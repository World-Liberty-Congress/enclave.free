"""
Session Logs — encrypted Conversation records for the Test & Feedback module.

Sensitive by default: a transcript is NIP-04 encrypted to the admin pubkey
*before* it touches disk, so the control plane persists only ciphertext (plus the
ephemeral pubkey the admin needs to decrypt client-side via NIP-07). The backend
can never read a transcript back in plaintext — see encryption.encrypt_for_admin.

General by design: `source` distinguishes an admin testing as a user persona
('admin_test') from a real user's logged session ('user'), so the same storage +
review pathway serves both without re-architecting.

- 'user' records mirror Sage. After each exchange Sage sends the complete
  retained user/assistant history with its own message IDs, and
  record_user_conversation keeps the encrypted copy in step.
- 'admin_test' records are saved once by the Admin browser at the end of a trial
  through save_transcript.

Every transcript message has a stable `message_id`. Usage (`turn_count`) is the
number of recorded messages, and review feedback belongs to a message ID, never
to a position in the transcript.

Layout:
- metadata rows live in the session_logs / session_log_feedback tables
- turn_metadata_json is the plaintext message order: turn_index, role, message_id
- transcript ciphertext lives in session_logs.transcript_ciphertext for Database
  Explorer visibility; transcript_path only points at encrypted files written by
  older releases
- the DB row carries the ephemeral pubkey for admin-side decryption
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
import zipfile
from io import BytesIO
from typing import Any, Optional

import database
from encryption import encrypt_for_admin_required, get_admin_pubkey
from models import SESSION_LOG_MESSAGE_ID_PATTERN

logger = logging.getLogger("enclave.session_logs")

VALID_SOURCES = ("admin_test", "user")
VALID_RATINGS = ("up", "down")
MESSAGE_ID_PATTERN = re.compile(SESSION_LOG_MESSAGE_ID_PATTERN)
# Concurrent writers to one record resolve on the next read, and archiving a
# record takes one extra pass; Sage retries the whole update if these run out.
_MAX_RECORD_ATTEMPTS = 4


def legacy_message_id(turn_index: int) -> str:
    """ID for a message saved before transcripts carried message IDs.

    database._migrate_session_log_feedback_message_ids gives existing feedback
    the same IDs, so it stays on the message it was saved for.
    """
    return f"legacy-{turn_index}"


def _new_log_id() -> str:
    return uuid.uuid4().hex


def _row_to_metadata(row: Any) -> dict[str, Any]:
    """Public metadata for a log row — never includes transcript content."""
    return {
        "log_id": row["log_id"],
        "source": row["source"],
        "title": row["title"],
        "subject_user_id": row["subject_user_id"],
        "user_type_id": row["user_type_id"],
        "sage_session_id": row["sage_session_id"],
        "turn_count": row["turn_count"],
        "status": row["status"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "has_transcript": bool(row["transcript_ciphertext"] or row["transcript_path"]),
    }


def _prepare_turns(turns: list[dict[str, Any]], *, require_ids: bool) -> list[dict[str, Any]]:
    """Give every turn a valid, unique message_id."""
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for turn in turns:
        item = dict(turn)
        message_id = item.get("message_id")
        if message_id is None:
            if require_ids:
                raise ValueError("Conversation messages require message IDs")
            message_id = uuid.uuid4().hex
        message_id = str(message_id)
        if not MESSAGE_ID_PATTERN.fullmatch(message_id):
            raise ValueError("Invalid transcript message ID")
        if message_id in seen:
            raise ValueError("Transcript message IDs must be unique")
        seen.add(message_id)
        item["message_id"] = message_id
        prepared.append(item)
    return prepared


def _turn_metadata_json(turns: list[dict[str, Any]]) -> str:
    return json.dumps(
        [
            {
                "turn_index": index,
                "role": str(turn.get("role", "")),
                "message_id": turn["message_id"],
            }
            for index, turn in enumerate(turns)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _transcript_messages(row: Any) -> list[dict[str, str]]:
    """The stored transcript's message order as [{message_id, role}]."""
    raw_metadata = row["turn_metadata_json"]
    if not raw_metadata:
        return []
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError as exc:
        raise ValueError("Transcript turn metadata is invalid") from exc
    if not isinstance(metadata, list):
        raise ValueError("Transcript turn metadata is invalid")

    messages: list[dict[str, str]] = []
    for index, item in enumerate(metadata):
        if not isinstance(item, dict):
            raise ValueError("Transcript turn metadata is invalid")
        messages.append(
            {
                "message_id": str(item.get("message_id") or legacy_message_id(index)),
                "role": str(item.get("role", "")),
            }
        )
    return messages


RecordVersion = tuple[Optional[int], int]


def _write_transcript(
    log_id: str,
    turns: list[dict[str, Any]],
    *,
    through_sequence_id: Optional[int] = None,
    expected_version: Optional[RecordVersion] = None,
) -> bool:
    """Encrypt turns to the admin pubkey and store them as the log's transcript.

    With expected_version, the write only lands if the record is still at that
    (sage_through_sequence_id, turn_count) version, and returns False otherwise.
    Fails closed: raises if no admin is configured, so a transcript is never
    written in plaintext.
    """
    admin_pubkey = get_admin_pubkey()
    if not admin_pubkey:
        raise ValueError("No admin configured for encryption")

    plaintext = json.dumps({"turns": turns}, ensure_ascii=False)
    ciphertext, ephemeral_pubkey = encrypt_for_admin_required(plaintext)
    now = database.utc_timestamp_z()
    sql = """UPDATE session_logs
             SET transcript_ciphertext = ?,
                 transcript_ephemeral_pubkey = ?,
                 encrypted_to_pubkey = ?,
                 turn_metadata_json = ?,
                 turn_count = ?,
                 sage_through_sequence_id = ?,
                 status = 'completed',
                 completed_at = ?,
                 updated_at = ?
             WHERE log_id = ?"""
    params: list[Any] = [
        ciphertext,
        ephemeral_pubkey,
        admin_pubkey,
        _turn_metadata_json(turns),
        len(turns),
        through_sequence_id,
        now,
        now,
        log_id,
    ]
    if expected_version is not None:
        sql += " AND sage_through_sequence_id IS ? AND turn_count = ?"
        params.extend(expected_version)

    with database.get_cursor() as cursor:
        cursor.execute(sql, params)
        written = cursor.rowcount > 0
    if not written and expected_version is None:
        raise KeyError(log_id)
    return written


def create_session_log(
    *,
    source: str = "admin_test",
    title: Optional[str] = None,
    subject_user_id: Optional[int] = None,
    user_type_id: Optional[int] = None,
    sage_session_id: Optional[str] = None,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    """Open a new (active) session log. Transcript is saved later via save_transcript."""
    if source not in VALID_SOURCES:
        raise ValueError(f"Invalid session log source: {source}")

    log_id = _new_log_id()
    with database.get_cursor() as cursor:
        cursor.execute(
            """INSERT INTO session_logs (
                log_id, source, title, subject_user_id, user_type_id,
                sage_session_id, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                log_id,
                source,
                title,
                subject_user_id,
                user_type_id,
                sage_session_id,
                created_by,
            ),
        )
    return get_session_log_metadata(log_id)  # type: ignore[return-value]


def save_transcript(
    log_id: str,
    turns: list[dict[str, Any]],
    *,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    """Encrypt and store a complete transcript, and mark the log completed.

    Turns without a message_id get a new one. Fails closed: raises if no admin
    is configured, so a transcript is never written in plaintext."""
    if _get_row(log_id) is None:
        raise KeyError(log_id)
    prepared = _prepare_turns(turns, require_ids=False)
    _write_transcript(log_id, prepared)
    logger.info("Saved encrypted transcript for session log %s (%d turns)", log_id, len(prepared))
    return get_session_log_metadata(log_id)  # type: ignore[return-value]


def record_user_conversation(
    *,
    sage_session_id: str,
    subject_user_id: int,
    messages: list[dict[str, Any]],
    through_sequence_id: int,
    user_type_id: Optional[int] = None,
    title: Optional[str] = None,
) -> dict[str, Any]:
    """Keep the encrypted record of one User Conversation in step with Sage.

    `messages` is Sage's complete retained user/assistant history, oldest
    first, each with Sage's message_id; `through_sequence_id` is Sage's sequence
    number for the newest of them. Sage numbers messages in the order it stores
    them, so a later read of a Conversation has a higher (through_sequence_id,
    message count) version than an earlier read. The record only moves forward:
    an update at or below the stored version (a retry, or an older update
    arriving late) leaves it unchanged.

    The record has its own lifecycle, separate from Sage Session Memory. When a
    newer history no longer contains the recorded messages (the Conversation
    continued after its Session Memory was deleted), the existing record is
    archived intact and the continuation starts a new record.
    """
    turns = _prepare_turns(messages, require_ids=True)
    if not turns:
        raise ValueError("A Conversation record needs at least one message")
    if not get_admin_pubkey():
        raise ValueError("No admin configured for encryption")
    incoming: RecordVersion = (through_sequence_id, len(turns))
    incoming_ids = {turn["message_id"] for turn in turns}

    for _ in range(_MAX_RECORD_ATTEMPTS):
        log, created = _find_or_create_user_log(
            sage_session_id=sage_session_id,
            subject_user_id=subject_user_id,
            user_type_id=user_type_id,
            title=title,
        )
        try:
            row = _get_row(log["log_id"])
            if row is None:
                continue
            stored: RecordVersion = (row["sage_through_sequence_id"], row["turn_count"])
            if stored[0] is not None and incoming <= stored:
                return _row_to_metadata(row)
            if stored[0] is not None and not _recorded_message_ids(row) <= incoming_ids:
                _archive_record(log["log_id"], expected_version=stored)
                continue
            if _write_transcript(
                log["log_id"],
                turns,
                through_sequence_id=through_sequence_id,
                expected_version=stored,
            ):
                logger.info(
                    "Recorded User Conversation %s (%d messages)",
                    log["log_id"],
                    len(turns),
                )
                return get_session_log_metadata(log["log_id"])  # type: ignore[return-value]
        except ValueError:
            if created:
                _discard_unwritten_log(log["log_id"])
            raise
    raise RuntimeError("Conversation record changed concurrently; retry the update")


def _recorded_message_ids(row: Any) -> set[str]:
    try:
        return {message["message_id"] for message in _transcript_messages(row)}
    except ValueError:
        return set()


def _archive_record(log_id: str, *, expected_version: RecordVersion) -> bool:
    """Set a record aside, unchanged, so its Conversation can start a new one.

    Clearing sage_session_id releases the one-record-per-session slot; the
    archived record keeps its subject, transcript, usage and feedback.
    """
    with database.get_cursor() as cursor:
        cursor.execute(
            """UPDATE session_logs
               SET status = 'archived', sage_session_id = NULL, updated_at = ?
               WHERE log_id = ? AND sage_through_sequence_id IS ? AND turn_count = ?""",
            (database.utc_timestamp_z(), log_id, *expected_version),
        )
        archived = cursor.rowcount > 0
    if archived:
        logger.info("Archived User Conversation record %s; its Conversation continues in a new record", log_id)
    return archived


def _find_or_create_user_log(
    *,
    sage_session_id: str,
    subject_user_id: int,
    user_type_id: Optional[int],
    title: Optional[str],
) -> tuple[dict[str, Any], bool]:
    log = get_session_log_metadata_by_sage_session_id(
        source="user",
        sage_session_id=sage_session_id,
        subject_user_id=subject_user_id,
    )
    if log is not None:
        return log, False
    try:
        return (
            create_session_log(
                source="user",
                title=title,
                subject_user_id=subject_user_id,
                user_type_id=user_type_id,
                sage_session_id=sage_session_id,
                created_by=f"user:{subject_user_id}",
            ),
            True,
        )
    except sqlite3.IntegrityError:
        # A concurrent update for the same Conversation created it first.
        log = get_session_log_metadata_by_sage_session_id(
            source="user",
            sage_session_id=sage_session_id,
            subject_user_id=subject_user_id,
        )
        if log is None:
            raise
        return log, False


def _discard_unwritten_log(log_id: str) -> None:
    try:
        delete_session_log(log_id)
    except OSError:
        logger.warning("Could not clean up unwritten session log %s", log_id, exc_info=True)


def list_session_logs(
    *,
    source: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List session-log metadata (no transcript content), newest first."""
    clauses = []
    params: list[Any] = []
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 500)))
    with database.get_cursor() as cursor:
        cursor.execute(
            f"""SELECT * FROM session_logs
                {where}
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?""",
            params,
        )
        return [_row_to_metadata(row) for row in cursor.fetchall()]


def get_session_log_metadata_by_sage_session_id(
    *,
    source: str,
    sage_session_id: str,
    subject_user_id: int,
) -> Optional[dict[str, Any]]:
    if source not in VALID_SOURCES:
        raise ValueError(f"Invalid session log source: {source}")
    with database.get_cursor() as cursor:
        cursor.execute(
            """SELECT * FROM session_logs
               WHERE source = ? AND sage_session_id = ? AND subject_user_id = ?
               ORDER BY datetime(updated_at) DESC, id DESC
               LIMIT 1""",
            (source, sage_session_id, subject_user_id),
        )
        row = cursor.fetchone()
    return _row_to_metadata(row) if row else None


def _get_row(log_id: str) -> Any:
    with database.get_cursor() as cursor:
        cursor.execute("SELECT * FROM session_logs WHERE log_id = ?", (log_id,))
        return cursor.fetchone()


def get_session_log_metadata(log_id: str) -> Optional[dict[str, Any]]:
    row = _get_row(log_id)
    return _row_to_metadata(row) if row else None


def get_session_log(log_id: str) -> Optional[dict[str, Any]]:
    """Full log for admin review: metadata + the encrypted transcript ciphertext
    (for client-side NIP-07 decryption) + the plaintext message order + feedback.
    The backend does NOT decrypt — it hands the admin the ciphertext and the
    ephemeral pubkey."""
    row = _get_row(log_id)
    if not row:
        return None

    transcript_ciphertext: Optional[str] = row["transcript_ciphertext"]
    if not transcript_ciphertext and row["transcript_path"]:
        try:
            with open(row["transcript_path"], "r", encoding="utf-8") as handle:
                transcript_ciphertext = handle.read()
        except OSError:
            logger.warning("Transcript file missing for session log %s", log_id, exc_info=True)

    try:
        transcript_messages = _transcript_messages(row)
    except ValueError:
        logger.warning("Transcript metadata is invalid for session log %s", log_id)
        transcript_messages = []

    data = _row_to_metadata(row)
    data["transcript_ciphertext"] = transcript_ciphertext
    data["transcript_ephemeral_pubkey"] = row["transcript_ephemeral_pubkey"]
    data["encrypted_to_pubkey"] = row["encrypted_to_pubkey"]
    data["transcript_messages"] = transcript_messages
    data["feedback"] = _feedback_for_messages(log_id, transcript_messages)
    return data


def delete_session_log(log_id: str) -> bool:
    """Delete a session log, its feedback (cascade), and any legacy transcript file."""
    row = _get_row(log_id)
    if not row:
        return False
    if row["transcript_path"]:
        try:
            os.remove(row["transcript_path"])
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not remove transcript file for %s", log_id, exc_info=True)
            raise
    with database.get_cursor() as cursor:
        cursor.execute("DELETE FROM session_logs WHERE log_id = ?", (log_id,))
        return cursor.rowcount > 0


def export_session_log_zip(
    log_id: str,
    *,
    changed_by: Optional[str] = None,
) -> tuple[str, bytes]:
    """Build a copied export bundle for one completed session log.

    The bundle intentionally carries encrypted transcript/comment material and
    the metadata needed for client-side decryption, never plaintext content.
    """
    detail = get_session_log(log_id)
    if detail is None:
        raise KeyError(log_id)
    if not detail["transcript_ciphertext"]:
        raise ValueError("Session log has no transcript to export")

    exported_at = database.utc_timestamp_z()
    metadata = {
        "log_id": detail["log_id"],
        "source": detail["source"],
        "title": detail["title"],
        "subject_user_id": detail["subject_user_id"],
        "user_type_id": detail["user_type_id"],
        "sage_session_id": detail["sage_session_id"],
        "turn_count": detail["turn_count"],
        "status": detail["status"],
        "created_by": detail["created_by"],
        "created_at": detail["created_at"],
        "updated_at": detail["updated_at"],
        "completed_at": detail["completed_at"],
        "has_transcript": detail["has_transcript"],
        "transcript_messages": detail["transcript_messages"],
        "transcript_ephemeral_pubkey": detail["transcript_ephemeral_pubkey"],
        "encrypted_to_pubkey": detail["encrypted_to_pubkey"],
        "copied_export": True,
        "lifecycle_posture": "outside_active_storage_lifecycle",
        "exported_at": exported_at,
    }

    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr(
            "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
        )
        zip_file.writestr("transcript.nip04.txt", detail["transcript_ciphertext"])
        zip_file.writestr(
            "feedback.json",
            json.dumps(detail["feedback"], ensure_ascii=False, indent=2, sort_keys=True),
        )

    database.log_config_audit_event(
        table_name="data_deletion",
        config_key="copied_export:test_feedback_session",
        old_value=None,
        new_value=json.dumps(
            {
                "workflow": "copied_export",
                "target": "test_feedback_session",
                "lifecycle_posture": "outside_active_storage_lifecycle",
                "log_id": log_id,
                "filename": f"test_feedback_{log_id}.zip",
                "exported_at": exported_at,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        changed_by=changed_by or "unknown",
    )
    return f"test_feedback_{log_id}.zip", archive.getvalue()


def record_plaintext_export(
    log_id: str,
    *,
    filename: str,
    changed_by: Optional[str] = None,
) -> None:
    """Audit a browser-generated plaintext export without receiving plaintext."""
    if get_session_log_metadata(log_id) is None:
        raise KeyError(log_id)

    exported_at = database.utc_timestamp_z()
    database.log_config_audit_event(
        table_name="data_deletion",
        config_key="copied_export:test_feedback_session",
        old_value=None,
        new_value=json.dumps(
            {
                "workflow": "copied_export",
                "target": "test_feedback_session",
                "lifecycle_posture": "outside_active_storage_lifecycle",
                "log_id": log_id,
                "filename": filename,
                "includes_decrypted_browser_values": True,
                "plaintext_contents_received_by_backend": False,
                "exported_at": exported_at,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        changed_by=changed_by or "unknown",
    )


def _feedback_to_dict(row: Any, positions: dict[str, int]) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        # The message's current position, or None when the record no longer
        # contains it (for example, after Sage memory for it was deleted).
        "turn_index": positions.get(row["message_id"]),
        "rating": row["rating"],
        "comment_ciphertext": row["comment_ciphertext"],
        "comment_ephemeral_pubkey": row["comment_ephemeral_pubkey"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _feedback_for_messages(
    log_id: str,
    transcript_messages: list[dict[str, str]],
) -> list[dict[str, Any]]:
    positions = {
        message["message_id"]: index for index, message in enumerate(transcript_messages)
    }
    with database.get_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM session_log_feedback WHERE log_id = ? ORDER BY id",
            (log_id,),
        )
        feedback = [_feedback_to_dict(row, positions) for row in cursor.fetchall()]
    return sorted(
        feedback,
        key=lambda item: (item["turn_index"] is None, item["turn_index"] or 0),
    )


def list_feedback(log_id: str) -> list[dict[str, Any]]:
    row = _get_row(log_id)
    if row is None:
        return []
    try:
        transcript_messages = _transcript_messages(row)
    except ValueError:
        transcript_messages = []
    return _feedback_for_messages(log_id, transcript_messages)


def set_message_feedback(
    log_id: str,
    message_id: str,
    rating: str,
    *,
    comment: Optional[str] = None,
    created_by: Optional[str] = None,
) -> dict[str, Any]:
    """Upsert feedback for one assistant message. Rating is stored plaintext (for
    querying); the optional comment is NIP-04 encrypted to the admin pubkey (it
    may quote transcript content)."""
    if rating not in VALID_RATINGS:
        raise ValueError(f"Invalid rating: {rating}")
    row = _get_row(log_id)
    if row is None:
        raise KeyError(log_id)
    if not row["turn_metadata_json"]:
        raise ValueError("Transcript turn metadata is unavailable")
    transcript_messages = _transcript_messages(row)
    if not any(
        message["message_id"] == message_id and message["role"] == "assistant"
        for message in transcript_messages
    ):
        raise ValueError("Feedback can only be saved for assistant messages in this transcript")

    comment_ciphertext: Optional[str] = None
    comment_ephemeral_pubkey: Optional[str] = None
    trimmed = comment.strip() if comment else None
    if trimmed:
        comment_ciphertext, comment_ephemeral_pubkey = encrypt_for_admin_required(trimmed)

    now = database.utc_timestamp_z()
    with database.get_cursor() as cursor:
        cursor.execute(
            """INSERT INTO session_log_feedback (
                log_id, message_id, rating, comment_ciphertext,
                comment_ephemeral_pubkey, created_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(log_id, message_id) DO UPDATE SET
                rating = excluded.rating,
                comment_ciphertext = excluded.comment_ciphertext,
                comment_ephemeral_pubkey = excluded.comment_ephemeral_pubkey,
                created_by = excluded.created_by,
                updated_at = excluded.updated_at""",
            (
                log_id,
                message_id,
                rating,
                comment_ciphertext,
                comment_ephemeral_pubkey,
                created_by,
                now,
            ),
        )
        cursor.execute(
            "SELECT * FROM session_log_feedback WHERE log_id = ? AND message_id = ?",
            (log_id, message_id),
        )
        saved = cursor.fetchone()
    positions = {
        message["message_id"]: index for index, message in enumerate(transcript_messages)
    }
    return _feedback_to_dict(saved, positions)
