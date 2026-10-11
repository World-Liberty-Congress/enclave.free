#!/usr/bin/env python3
"""Export organization state and stage a clean replay; never clone user history."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from Crypto.Cipher import AES


TABLES = (
    "instance_settings", "user_types", "user_field_definitions", "ai_config",
    "ai_config_user_type_overrides", "country_regions", "resources", "ingest_jobs",
    "document_defaults", "document_defaults_user_type_overrides", "retrieval_chunks", "deployment_config",
)
EXCLUDED = {
    "admins", "instance_state", "users", "user_field_values", "user_memories",
    "session_logs", "session_log_feedback", "config_audit_log", "inference_verification_records",
    "deletion_tombstones", "retention_run_records", "sqlite_sequence",
}
EMPTY_DESTINATION = EXCLUDED - {"instance_state", "sqlite_sequence"}
DESTINATION_KEYS = {
    "ADMIN_BASE_URL", "API_BASE_URL", "BASE_DOMAIN", "CDN_DOMAINS", "CORS_ORIGINS",
    "CUSTOM_SEARXNG_URL", "DKIM_SELECTOR", "DMARC_POLICY", "EMAIL_DOMAIN", "EMBEDDING_API_URL",
    "FRONTEND_URL", "INSTANCE_URL", "LLM_API_URL", "MONITORING_URL", "QDRANT_HOST", "QDRANT_PORT",
    "SEARXNG_URL", "SMTP_FROM", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SPF_INCLUDE",
    "SQLITE_PATH", "SSL_CERT_PATH", "TRUSTED_PROXIES", "UPLOADS_DIR", "WEBHOOK_BASE_URL",
}
FORCED_VALUES = {"DOCUMENT_ARTIFACT_ENCRYPTION": "required", "FORCE_HTTPS": "true", "MOCK_EMAIL": "false"}
RESET_VALUES = {"SMTP_LAST_TEST_AT": "", "SMTP_LAST_TEST_SUCCESS": ""}
CREDENTIAL_KEYS = {"CONTENT_ENCRYPTION_KEY", "LLM_API_KEY", "EMBEDDING_API_KEY", "TINFOIL_API_KEY", "MAPLE_API_KEY", "SECRET_KEY", "INTERNAL_AGENT_TOKEN", "SMTP_USER", "SMTP_PASS", "RATE_LIMIT_VALKEY_URL", "SSL_KEY_PATH"}
REQUIRED_CONFIGURED_KEYS = {"CONTENT_ENCRYPTION_KEY", "LLM_API_KEY", "EMBEDDING_API_KEY", "SMTP_USER", "SMTP_PASS", "FRONTEND_URL"}
PAYLOAD_KEYS = {"type", "chunk_id", "job_id", "source_file", "source_label", "content_ref", "embedding_provider", "embedding_model"}
ARTIFACT_PREFIX = b"enclave-artifact::v1::"
BUNDLE_PREFIX = b"enclave-migration::v1::"
COLLECTION = "enclave_knowledge"


class MigrationError(RuntimeError):
    pass


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def artifact_key(secret: str) -> bytes:
    if not secret:
        raise MigrationError("An encrypted source artifact requires its source content key")
    info = b"enclave/artifact/v1"
    prk = hmac.new(info, secret.encode(), hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def decrypt_artifact(value: bytes, secret: str) -> bytes:
    if value.startswith(b"sanctum-artifact::v1::"):
        raise MigrationError("Legacy encrypted artifact requires the supported confidentiality repair first")
    if not value.startswith(ARTIFACT_PREFIX):
        return value
    raw = base64.b64decode(value[len(ARTIFACT_PREFIX):], validate=True)
    cipher = AES.new(artifact_key(secret), AES.MODE_GCM, nonce=raw[:12])
    return cipher.decrypt_and_verify(raw[28:], raw[12:28])


def encrypt_artifact(value: bytes, secret: str) -> bytes:
    nonce = os.urandom(12)
    cipher = AES.new(artifact_key(secret), AES.MODE_GCM, nonce=nonce)
    content, tag = cipher.encrypt_and_digest(value)
    return ARTIFACT_PREFIX + base64.b64encode(nonce + tag + content)


def bundle_key() -> bytes:
    path = Path(os.environ.get("MIGRATION_BUNDLE_KEY_FILE", ""))
    if not path.is_file() or path.stat().st_mode & 0o077:
        raise MigrationError("MIGRATION_BUNDLE_KEY_FILE must be a private file with a 64-character hex key")
    key = bytes.fromhex(path.read_text().strip())
    if len(key) != 32:
        raise MigrationError("Migration bundle key must contain 32 random bytes encoded as hex")
    return key


def seal(source: Path, output: Path) -> None:
    cipher = AES.new(bundle_key(), AES.MODE_GCM, nonce=os.urandom(12))
    cipher.update(BUNDLE_PREFIX)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".carryover-", delete=False) as dest:
        stage = Path(dest.name)
        try:
            with source.open("rb") as src:
                dest.write(BUNDLE_PREFIX + cipher.nonce)
                while block := src.read(1024 * 1024):
                    dest.write(cipher.encrypt(block))
                dest.write(cipher.digest())
            dest.flush()
            os.fsync(dest.fileno())
            # Atomic publication without replacing an existing export.
            os.link(stage, output)
        finally:
            stage.unlink(missing_ok=True)


@contextmanager
def open_bundle(source: Path):
    # Authenticate the entire encrypted bundle before parsing any ZIP content.
    with tempfile.TemporaryDirectory(prefix="enclave-carryover-") as temp:
        plain = Path(temp) / "bundle.zip"
        with source.open("rb") as src, plain.open("wb") as dest:
            if src.read(len(BUNDLE_PREFIX)) != BUNDLE_PREFIX:
                raise MigrationError("Unrecognized migration bundle format")
            cipher = AES.new(bundle_key(), AES.MODE_GCM, nonce=src.read(12))
            cipher.update(BUNDLE_PREFIX)
            remaining = source.stat().st_size - len(BUNDLE_PREFIX) - 12 - 16
            if remaining < 0:
                raise MigrationError("Truncated migration bundle")
            while remaining:
                block = src.read(min(1024 * 1024, remaining))
                if not block:
                    raise MigrationError("Truncated migration bundle")
                dest.write(cipher.decrypt(block))
                remaining -= len(block)
            cipher.verify(src.read(16))
        with zipfile.ZipFile(plain) as archive:
            yield archive


def connect(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def rows(c: sqlite3.Connection, table: str) -> list[dict]:
    if table not in set(TABLES) | EXCLUDED:
        raise MigrationError("Unclassified database table")
    return [dict(row) for row in c.execute(f'SELECT * FROM "{table}"')]


def counts(c: sqlite3.Connection) -> dict[str, int]:
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if names - set(TABLES) - EXCLUDED or set(TABLES) - names:
        raise MigrationError("Database schema differs from the reviewed migration boundary")
    return {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(names)}


def request(base: str, path: str, body=None, method=None):
    url = urllib.parse.urlsplit(base)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query:
        raise MigrationError("Use an internal Qdrant URL without embedded credentials")
    headers = {"Content-Type": "application/json"}
    if os.environ.get("MIGRATION_QDRANT_API_KEY"):
        headers["api-key"] = os.environ["MIGRATION_QDRANT_API_KEY"]
    req = urllib.request.Request(base.rstrip("/") + path, data=canonical(body) if body is not None else None, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as response:
        result = json.load(response)
    if result.get("status") != "ok":
        raise MigrationError("Qdrant did not complete the requested operation")
    return result["result"]


def collect_index(base: str, chunks: dict[str, dict], active_ids: set[str]) -> dict:
    info = request(base, f"/collections/{COLLECTION}")
    vectors = info["config"]["params"]["vectors"]
    if not isinstance(vectors, dict) or not isinstance(vectors.get("size"), int):
        raise MigrationError("Only the inspected dense knowledge-vector layout is supported")
    points, offset = [], None
    while True:
        body = {"limit": 256, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        page = request(base, f"/collections/{COLLECTION}/points/scroll", body)
        points.extend(page["points"])
        offset = page.get("next_page_offset")
        if offset is None:
            break
    indexed_chunks = set()
    for point in points:
        payload = point.get("payload") or {}
        chunk_id = payload.get("chunk_id")
        if set(payload) - PAYLOAD_KEYS or chunk_id not in chunks or payload.get("job_id") != chunks[chunk_id]["job_id"]:
            raise MigrationError("Knowledge index contains an unclassified or unmatched payload")
        if chunk_id in indexed_chunks:
            raise MigrationError("Knowledge index contains duplicate chunk references")
        indexed_chunks.add(chunk_id)
        point.pop("shard_key", None)
    if not active_ids <= indexed_chunks:
        raise MigrationError("An active retrieval chunk is missing its source vector")
    return {"vectors": vectors, "points": sorted(points, key=lambda p: str(p["id"]))}


def read_sage(container: str) -> dict:
    query = "SELECT json_build_object('ai_config', (SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json) FROM ai_config t), 'ai_config_user_type_overrides', (SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json) FROM ai_config_user_type_overrides t));"
    # Credentials are read in memory, never printed or placed in shell text.
    inspected = subprocess.run(["docker", "inspect", container], capture_output=True, text=True, check=True)
    env = dict(item.split("=", 1) for item in json.loads(inspected.stdout)[0]["Config"]["Env"] if "=" in item)
    result = subprocess.run(["docker", "exec", container, "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", env["POSTGRES_USER"], "-d", env["POSTGRES_DB"], "-c", query], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def apply_sage(output: Path, container: str) -> dict:
    if not (output / "REPLAY_COMPLETE.json").is_file():
        raise MigrationError("Only a verified replay may be applied")
    verify_replay(output)
    raw = subprocess.run(["docker", "inspect", container], capture_output=True, text=True, check=True)
    env = dict(v.split("=", 1) for v in json.loads(raw.stdout)[0]["Config"]["Env"] if "=" in v)
    sql = (output / "sage-config.sql").read_text()
    subprocess.run(["docker", "exec", "-i", container, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", env["POSTGRES_USER"], "-d", env["POSTGRES_DB"]], input=sql, capture_output=True, text=True, check=True)
    expected = json.loads((output / "sage.json").read_text())
    actual = read_sage(container)
    for data in (expected, actual):
        for table, values in data.items():
            for row in values:
                row["updated_at"] = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            values.sort(key=lambda r: str(r.get("key", r.get("id"))))
    if canonical(actual) != canonical(expected):
        raise MigrationError("Destination Sage Agent Settings differ after replay")
    return {"status": "Sage Agent Settings applied and compared", "config_count": len(actual["ai_config"]), "override_count": len(actual["ai_config_user_type_overrides"])}


def validate_sage(data: dict, type_ids: set[int]) -> None:
    if set(data) != {"ai_config", "ai_config_user_type_overrides"}:
        raise MigrationError("Only Sage Agent Settings and user-type overrides may be replayed")
    columns = {"ai_config": {"key", "value", "value_type", "category", "description", "updated_at"}, "ai_config_user_type_overrides": {"id", "ai_config_key", "user_type_id", "value", "updated_at"}}
    for table, values in data.items():
        if not isinstance(values, list) or any(set(v) != columns[table] for v in values):
            raise MigrationError("Sage configuration schema differs from the reviewed boundary")
    keys = {r["key"] for r in data["ai_config"]}
    if any(r["user_type_id"] not in type_ids or r["ai_config_key"] not in keys for r in data["ai_config_user_type_overrides"]):
        raise MigrationError("Sage user-type override has a missing configuration or User Type")


def export_bundle(sqlite: Path, uploads: Path, stored_prefix: str, sage: dict, qdrant: str, output: Path) -> dict:
    source_key = os.environ.get("SOURCE_CONTENT_ENCRYPTION_KEY", "")
    with connect(sqlite) as c, tempfile.TemporaryDirectory(prefix="enclave-carryover-") as temp:
        c.execute("BEGIN")
        inventory = counts(c)
        data = {t: rows(c, t) for t in TABLES}
        if any(j["status"] in {"pending", "processing"} for j in data["ingest_jobs"]):
            raise MigrationError("Complete or explicitly resolve in-flight ingests before exporting")
        validate_sage(sage, {t["id"] for t in data["user_types"]})
        required_overrides = []
        for row in data["deployment_config"]:
            if row["key"] in CREDENTIAL_KEYS:
                row["is_secret"] = 1
            if row["is_secret"] or row["key"] in DESTINATION_KEYS:
                required_overrides.append(row["key"])
                row["value"] = None
            if row["key"] in RESET_VALUES:
                row["value"] = RESET_VALUES[row["key"]]
        for row in data["instance_settings"]:
            if any(word in row["key"].lower() for word in ("password", "secret", "token", "api_key")):
                raise MigrationError("Instance Settings contains a credential-like key requiring explicit review")
        chunks = {r["chunk_id"]: r for r in data["retrieval_chunks"]}
        chunk_hashes = {}
        for row in chunks.values():
            text = decrypt_artifact(row["encrypted_text"].encode(), source_key)
            chunk_hashes[row["chunk_id"]] = digest(text)
            row["encrypted_text"] = text.decode()
        active_jobs = {j["job_id"] for j in data["ingest_jobs"] if j["is_current"] != 0 and not j["replaced_by_job_id"] and j["status"] in {"completed", "completed_with_errors"}}
        index = collect_index(qdrant, chunks, {r["chunk_id"] for r in chunks.values() if r["job_id"] in active_jobs})
        artifacts, absent = {}, []
        archive_path = Path(temp) / "bundle.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for job in data["ingest_jobs"]:
                relative = PurePosixPath(job["file_path"]).relative_to(PurePosixPath(stored_prefix))
                path = uploads.joinpath(*relative.parts)
                if ".." in relative.parts or not path.resolve().is_relative_to(uploads.resolve()) or path.is_symlink():
                    raise MigrationError("Document artifact path escapes the reviewed uploads boundary")
                artifact_name = "artifacts/" + digest(job["job_id"].encode()) + PurePosixPath(job["file_path"]).suffix
                if not path.is_file():
                    if job["job_id"] in active_jobs:
                        raise MigrationError("Active document artifact is missing")
                    absent.append(job["job_id"])
                else:
                    content = decrypt_artifact(path.read_bytes(), source_key)
                    artifacts[job["job_id"]] = {"archive_path": artifact_name, "sha256": digest(content)}
                    archive.writestr(artifact_name, content)
                job["file_path"] = "/uploads/migrated/" + PurePosixPath(artifact_name).name
            manifest = {
                "format_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "source_counts": inventory, "retained_counts": {t: len(r) for t, r in data.items()},
                "excluded_tables": sorted(EXCLUDED), "required_destination_keys": sorted(required_overrides),
                "artifacts": artifacts, "absent_inactive_artifact_job_ids": sorted(absent),
                "chunk_plaintext_sha256": chunk_hashes, "index_sha256": digest(canonical(index)),
                "sage_sha256": digest(canonical(sage)), "tables_sha256": digest(canonical(data)),
            }
            archive.writestr("tables.json", canonical(data))
            archive.writestr("sage.json", canonical(sage))
            archive.writestr("index.json", canonical(index))
            archive.writestr("manifest.json", canonical(manifest))
        seal(archive_path, output)
        return {"status": "encrypted carryover exported", "retained_counts": manifest["retained_counts"], "active_artifact_count": len(artifacts), "absent_inactive_artifact_count": len(absent), "knowledge_vector_count": len(index["points"])}


def secret_value(value: str, auth_secret: str) -> str:
    key = hashlib.sha256(f"enclave-deployment-config:{auth_secret}".encode()).digest()
    nonce = os.urandom(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    content, tag = cipher.encrypt_and_digest(value.encode())
    return "enc::v1::" + ":".join(base64.b64encode(v).decode() for v in (nonce, tag, content))


def sage_sql(data: dict) -> str:
    clean_guard = """DO $$ DECLARE t TEXT; populated BOOLEAN; BEGIN
    FOREACH t IN ARRAY ARRAY['web_sessions','external_identities','agents','messages','blocks','passages','summaries','chat_contexts','user_preferences','scheduled_tasks'] LOOP
      IF to_regclass('public.' || t) IS NOT NULL THEN
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM %I)', t) INTO populated;
        IF populated THEN RAISE EXCEPTION 'Destination Sage is not clean'; END IF;
      END IF;
    END LOOP; END $$;"""
    sql = ["BEGIN;", "SET standard_conforming_strings = on;", clean_guard]
    for table in ("ai_config_user_type_overrides", "ai_config"):
        sql.append(f"DELETE FROM {table};")
    for table, values in data.items():
        literal = canonical(values).decode().replace("'", "''")
        sql.append(f"INSERT INTO {table} SELECT * FROM json_populate_recordset(NULL::{table}, '{literal}'::json);")
    sql.append("COMMIT;")
    return "\n".join(sql) + "\n"


def replay(bundle: Path, schema: Path, output: Path, destination: dict[str, str]) -> dict:
    if output.exists():
        raise MigrationError("Replay output must be a new private directory")
    content_key, auth_secret = os.environ.get("CONTENT_ENCRYPTION_KEY", ""), os.environ.get("SECRET_KEY", "")
    if len(content_key) < 32 or len(auth_secret) < 32:
        raise MigrationError("Fresh production CONTENT_ENCRYPTION_KEY and SECRET_KEY must be configured")
    with open_bundle(bundle) as archive, connect(schema) as seed:
        seed_counts = counts(seed)
        if any(seed_counts.get(t, 0) for t in EMPTY_DESTINATION):
            raise MigrationError("Schema database already contains identities, history, or verification state")
        manifest = json.loads(archive.read("manifest.json"))
        data, sage, index = (json.loads(archive.read(name)) for name in ("tables.json", "sage.json", "index.json"))
        if manifest.get("format_version") != 1 or set(data) != set(TABLES):
            raise MigrationError("Unexpected migration format or retained tables")
        for item, key in ((data, "tables_sha256"), (sage, "sage_sha256"), (index, "index_sha256")):
            if digest(canonical(item)) != manifest[key]:
                raise MigrationError("Migration manifest integrity mismatch")
        validate_sage(sage, {t["id"] for t in data["user_types"]})
        required = set(manifest["required_destination_keys"])
        if required - set(destination) or any(not isinstance(v, str) for v in destination.values()):
            raise MigrationError("Explicit destination values are required for every secret and deployment-specific setting")
        if any(not destination.get(k, "").strip() for k in REQUIRED_CONFIGURED_KEYS & required):
            raise MigrationError("Organization provider/email credentials and production content key must be nonempty")
        if destination.get("CONTENT_ENCRYPTION_KEY") != content_key:
            raise MigrationError("Destination configuration and runtime content keys must match")
        if not destination.get("FRONTEND_URL", "").startswith("https://"):
            raise MigrationError("Production frontend must use HTTPS")
        for row in data["deployment_config"]:
            key = row["key"]
            value = FORCED_VALUES.get(key, RESET_VALUES.get(key, destination.get(key, row["value"])))
            row["value"] = secret_value(value, auth_secret) if row["is_secret"] and value else value
        output.mkdir(mode=0o700, parents=True)
        uploads = output / "uploads" / "migrated"
        uploads.mkdir(mode=0o700, parents=True)
        for job_id, artifact in manifest["artifacts"].items():
            name = PurePosixPath(artifact["archive_path"]).name
            content = archive.read(artifact["archive_path"])
            if digest(content) != artifact["sha256"]:
                raise MigrationError("Uploaded document plaintext integrity mismatch")
            (uploads / name).write_bytes(encrypt_artifact(content, content_key))
        for row in data["retrieval_chunks"]:
            content = row["encrypted_text"].encode()
            if digest(content) != manifest["chunk_plaintext_sha256"][row["chunk_id"]]:
                raise MigrationError("Retrieval chunk plaintext integrity mismatch")
            row["encrypted_text"] = encrypt_artifact(content, content_key).decode()
        db = sqlite3.connect(output / "enclave.db")
        try:
            seed.backup(db)
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                for table in reversed(TABLES):
                    db.execute(f'DELETE FROM "{table}"')
                for table in TABLES:
                    columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
                    for row in data[table]:
                        if set(row) != set(columns):
                            raise MigrationError("Destination schema does not match retained source columns")
                        names = ",".join(f'"{c}"' for c in columns)
                        db.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})', [row[c] for c in columns])
                if db.execute("PRAGMA foreign_key_check").fetchall():
                    raise MigrationError("Replay has unresolved configuration or document references")
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise MigrationError("Replay SQLite integrity check failed")
        finally:
            db.close()
        (output / "sage-config.sql").write_text(sage_sql(sage))
        (output / "sage.json").write_bytes(canonical(sage))
        (output / "index.json").write_bytes(canonical(index))
        manifest["replayed_tables_sha256"] = digest(canonical(data))
        (output / "manifest.json").write_bytes(canonical(manifest))
        result = verify_replay(output)
        (output / "REPLAY_COMPLETE.json").write_bytes(canonical(result))
        return result


def verify_replay(output: Path) -> dict:
    key = os.environ.get("CONTENT_ENCRYPTION_KEY", "")
    manifest = json.loads((output / "manifest.json").read_text())
    index = json.loads((output / "index.json").read_text())
    with connect(output / "enclave.db") as c:
        actual = counts(c)
        if any(actual.get(t, 0) for t in EMPTY_DESTINATION):
            raise MigrationError("Replay contains excluded identities or history")
        if any(actual[t] != count for t, count in manifest["retained_counts"].items()):
            raise MigrationError("Replay retained row counts differ from manifest")
        if digest(canonical({t: rows(c, t) for t in TABLES})) != manifest["replayed_tables_sha256"]:
            raise MigrationError("Replay configuration or metadata differs from the staged manifest")
        for row in rows(c, "retrieval_chunks"):
            raw = row["encrypted_text"].encode()
            if not raw.startswith(ARTIFACT_PREFIX) or digest(decrypt_artifact(raw, key)) != manifest["chunk_plaintext_sha256"][row["chunk_id"]]:
                raise MigrationError("Replay chunk encryption or plaintext hash differs")
    for artifact in manifest["artifacts"].values():
        raw = (output / "uploads" / "migrated" / PurePosixPath(artifact["archive_path"]).name).read_bytes()
        if not raw.startswith(ARTIFACT_PREFIX) or digest(decrypt_artifact(raw, key)) != artifact["sha256"]:
            raise MigrationError("Replay upload encryption or plaintext hash differs")
    if digest(canonical(index)) != manifest["index_sha256"]:
        raise MigrationError("Replay knowledge vector integrity differs")
    sage = json.loads((output / "sage.json").read_text())
    if digest(canonical(sage)) != manifest["sage_sha256"] or (output / "sage-config.sql").read_text() != sage_sql(sage):
        raise MigrationError("Replay Sage configuration integrity differs")
    return {"status": "offline replay verified", "retained_counts": manifest["retained_counts"], "encrypted_artifact_count": len(manifest["artifacts"]), "knowledge_vector_count": len(index["points"]), "absent_inactive_artifact_count": len(manifest["absent_inactive_artifact_job_ids"]), "excluded_identity_and_history_rows": 0, "remaining": "Apply Sage settings and vectors to an isolated clean stack, then verify full product behavior"}


def apply_index(output: Path, base: str) -> dict:
    if not (output / "REPLAY_COMPLETE.json").is_file():
        raise MigrationError("Only a verified replay may be applied")
    verify_replay(output)
    index = json.loads((output / "index.json").read_text())
    collections = request(base, "/collections")["collections"]
    if any(c["name"] == COLLECTION for c in collections):
        info = request(base, f"/collections/{COLLECTION}")
        if info.get("points_count", 0) or info["config"]["params"]["vectors"] != index["vectors"]:
            raise MigrationError("Destination knowledge collection must be empty and use matching vectors")
    else:
        request(base, f"/collections/{COLLECTION}", {"vectors": index["vectors"]}, "PUT")
    for start in range(0, len(index["points"]), 128):
        request(base, f"/collections/{COLLECTION}/points?wait=true", {"points": index["points"][start:start + 128]}, "PUT")
    chunks = {r["chunk_id"]: r for r in rows_from_replay(output)}
    actual = collect_index(base, chunks, {p["payload"]["chunk_id"] for p in index["points"]})
    expected_points = {str(p["id"]): p for p in index["points"]}
    actual_points = {str(p["id"]): p for p in actual["points"]}
    if actual["vectors"] != index["vectors"] or set(expected_points) != set(actual_points):
        raise MigrationError("Destination knowledge vector identities/configuration differ")
    maximum_difference = 0.0
    for point_id, expected in expected_points.items():
        found = actual_points[point_id]
        if found["payload"] != expected["payload"] or len(found["vector"]) != len(expected["vector"]):
            raise MigrationError("Destination knowledge payloads/vector dimensions differ")
        for left, right in zip(found["vector"], expected["vector"]):
            if not math.isclose(left, right, rel_tol=1e-5, abs_tol=1e-6):
                raise MigrationError("Destination knowledge vectors differ beyond float32 normalization tolerance")
            maximum_difference = max(maximum_difference, abs(left - right))
    return {"status": "knowledge index applied and compared", "point_count": len(index["points"]), "maximum_vector_difference": maximum_difference}


def rows_from_replay(output: Path) -> list[dict]:
    with connect(output / "enclave.db") as c:
        return rows(c, "retrieval_chunks")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export")
    e.add_argument("--sqlite", type=Path, required=True)
    e.add_argument("--uploads", type=Path, required=True)
    e.add_argument("--stored-uploads-prefix", default="/uploads")
    sage = e.add_mutually_exclusive_group(required=True)
    sage.add_argument("--sage-container")
    sage.add_argument("--sage-config-json", type=Path)
    e.add_argument("--qdrant-url", required=True)
    e.add_argument("--output", type=Path, required=True)
    e.add_argument("--source-is-frozen", action="store_true", required=True, help="Source writers are stopped, or reads use an isolated restored copy")
    r = sub.add_parser("replay")
    r.add_argument("--bundle", type=Path, required=True)
    r.add_argument("--schema-db", type=Path, required=True, help="Freshly initialized Enclave database with no identities/history")
    r.add_argument("--destination-values", type=Path, required=True, help="Private JSON containing replacement deployment settings")
    r.add_argument("--output-dir", type=Path, required=True)
    for name in ("verify", "apply-index", "apply-sage"):
        p = sub.add_parser(name)
        p.add_argument("--output-dir", type=Path, required=True)
        if name == "apply-index":
            p.add_argument("--qdrant-url", required=True)
        if name == "apply-sage":
            p.add_argument("--postgres-container", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "export":
            if args.output.exists():
                raise MigrationError("Export output already exists")
            sage = read_sage(args.sage_container) if args.sage_container else json.loads(args.sage_config_json.read_text())
            result = export_bundle(args.sqlite, args.uploads, args.stored_uploads_prefix, sage, args.qdrant_url, args.output)
        elif args.command == "replay":
            if args.destination_values.stat().st_mode & 0o077:
                raise MigrationError("Destination values file must have private permissions")
            result = replay(args.bundle, args.schema_db, args.output_dir, json.loads(args.destination_values.read_text()))
        elif args.command == "verify":
            result = verify_replay(args.output_dir)
        elif args.command == "apply-index":
            result = apply_index(args.output_dir, args.qdrant_url)
        else:
            result = apply_sage(args.output_dir, args.postgres_container)
        print(json.dumps(result))
    except MigrationError as exc:
        print(f"Carryover failed: {exc}. No production acceptance reported.", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError, zipfile.BadZipFile):
        # Never print database values, raw provider errors, or SQL containing prompts.
        print("Carryover failed; source/destination state did not pass a required check. Inspect privately; no production acceptance reported.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
