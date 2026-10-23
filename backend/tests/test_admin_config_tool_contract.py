import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient


APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class DummySentenceTransformer:
    def __init__(self, *_args, **_kwargs) -> None:
        pass


class AdminConfigToolContractTest(unittest.TestCase):
    """Verifies private Admin Config Tool contracts used by Sage."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_sqlite_path = os.environ.get("SQLITE_PATH")
        self._orig_secret_key = os.environ.get("SECRET_KEY")
        self._orig_internal_token = os.environ.get("INTERNAL_AGENT_TOKEN")
        self._orig_uploads_dir = os.environ.get("UPLOADS_DIR")
        self._orig_sentence_transformers = sys.modules.get("sentence_transformers")
        sys.modules["sentence_transformers"] = types.SimpleNamespace(
            SentenceTransformer=DummySentenceTransformer
        )
        os.environ["SQLITE_PATH"] = str(Path(self.tmp.name) / "enclave.db")
        os.environ["SECRET_KEY"] = "test-secret"
        os.environ["INTERNAL_AGENT_TOKEN"] = "test-internal-token"
        os.environ["UPLOADS_DIR"] = str(Path(self.tmp.name) / "uploads")

        import database
        import ingest_db
        import internal_agent

        self.database = importlib.reload(database)
        self.ingest_db = importlib.reload(ingest_db)
        self.internal_agent = importlib.reload(internal_agent)
        self.database.init_schema()
        self.admin_id = self.database.add_admin("abc123")

        app = FastAPI()
        app.include_router(self.internal_agent.router)
        self.client = TestClient(app)
        self.headers = {"X-Internal-Agent-Token": "test-internal-token"}
        self.admin_actor = {
            "id": self.admin_id,
            "type": "admin",
            "approved": True,
            "pubkey": "abc123",
        }

    def tearDown(self) -> None:
        if self.database._connection is not None:
            self.database._connection.close()
            self.database._connection = None
        self._restore_env("SQLITE_PATH", self._orig_sqlite_path)
        self._restore_env("SECRET_KEY", self._orig_secret_key)
        self._restore_env("INTERNAL_AGENT_TOKEN", self._orig_internal_token)
        self._restore_env("UPLOADS_DIR", self._orig_uploads_dir)
        self._restore_module("sentence_transformers", self._orig_sentence_transformers)
        self.tmp.cleanup()

    @staticmethod
    def _restore_env(name: str, value: Optional[str]) -> None:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    @staticmethod
    def _restore_module(name: str, value: object | None) -> None:
        if value is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = value

    def test_read_deployment_readiness_returns_structured_masked_tool_result(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/deployment-readiness",
            headers=self.headers,
            json={"actor": self.admin_actor},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["tool"], "read_deployment_readiness")
        self.assertEqual(payload["secret_policy"], {"mode": "masked"})
        self.assertIsInstance(payload["warnings"], list)
        self.assertIn("generated_at", payload)
        self.assertIn("items", payload["data"])
        self.assertIn("summary", payload["data"])
        self.assertNotIn("test-secret", response.text)

    def test_read_instance_settings_returns_structured_tool_result(self) -> None:
        self.database.update_settings({
            "instance_name": "FreeThem",
            "assistant_name": "Ally",
            "header_tagline": "Political Prisoner Support Team.",
            "description": "Support families and organizers.",
            "primary_color": "#8B5CF6",
            "default_theme": "dark",
            "auto_approve_users": "true",
        })
        self.database.mark_onboarding_configured_keys([
            "instance_name",
            "assistant_name",
            "header_tagline",
            "description",
            "primary_color",
            "default_theme",
            "auto_approve_users",
        ])

        response = self.client.post(
            "/internal/agent/admin-config/instance-settings",
            headers=self.headers,
            json={"actor": self.admin_actor},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["tool"], "read_instance_settings")
        self.assertEqual(payload["secret_policy"], {"mode": "masked"})
        self.assertEqual(payload["warnings"], [])
        data = payload["data"]
        self.assertEqual(data["settings"]["instance_name"], "FreeThem")
        self.assertEqual(data["settings"]["assistant_name"], "Ally")
        self.assertEqual(data["settings"]["default_theme"], "dark")
        self.assertIn("instance_name", data["explicitly_set_keys"])
        self.assertNotIn("onboarding_configured_keys", data["settings"])
        fields_by_key = {field["key"]: field for field in data["fields"]}
        self.assertEqual(fields_by_key["instance_name"]["label"], "Instance name")
        self.assertEqual(fields_by_key["instance_name"]["value"], "FreeThem")
        self.assertEqual(fields_by_key["instance_name"]["source"], "operator")
        self.assertTrue(fields_by_key["instance_name"]["editable"])
        self.assertEqual(
            fields_by_key["default_theme"]["supported_values"],
            ["light", "dark", "system"],
        )
        self.assertNotIn("test-secret", response.text)

    def test_update_instance_settings_applies_atomic_audited_direct_write(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/update-instance-settings",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-instance-write",
                "settings": {
                    "instance_name": "Freedom Network",
                    "default_theme": "DARK",
                    "auto_approve_users": False,
                },
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["tool"], "update_instance_settings")
        self.assertEqual(payload["secret_policy"], {"mode": "masked"})
        self.assertEqual(payload["data"]["outcome"], "succeeded")
        self.assertEqual(payload["data"]["validation"], {"status": "valid"})
        self.assertEqual(
            payload["data"]["saved_values"],
            {
                "auto_approve_users": "false",
                "default_theme": "dark",
                "instance_name": "Freedom Network",
            },
        )
        self.assertEqual(
            payload["data"]["changed_names"],
            ["auto_approve_users", "default_theme", "instance_name"],
        )
        self.assertEqual(payload["data"]["affected_areas"], ["instance_settings"])

        settings = self.database.get_all_settings()
        self.assertEqual(settings["instance_name"], "Freedom Network")
        self.assertEqual(settings["default_theme"], "dark")
        self.assertEqual(settings["auto_approve_users"], "false")
        self.assertTrue(
            {"instance_name", "default_theme", "auto_approve_users"}.issubset(
                self.database.get_onboarding_configured_keys()
            )
        )

        audit_entries = self.database.get_config_audit_log(
            limit=None,
            table_name="instance_settings",
        )
        direct_entries = [
            entry
            for entry in audit_entries
            if entry["conversation_id"] == "conversation-instance-write"
        ]
        self.assertEqual(
            {entry["config_key"] for entry in direct_entries},
            {"instance_name", "default_theme", "auto_approve_users"},
        )
        self.assertTrue(
            all(entry["action_source"] == "sage_conversation" for entry in direct_entries)
        )
        self.assertTrue(all(entry["changed_by"] == "abc123" for entry in direct_entries))
        self.assertNotIn("conversation-instance-write", " ".join(
            str(entry.get("old_value") or "") + str(entry.get("new_value") or "")
            for entry in direct_entries
        ))

    def test_update_instance_settings_rejects_invalid_batch_without_mutation(self) -> None:
        original_name = self.database.get_setting("instance_name")

        response = self.client.post(
            "/internal/agent/admin-config/update-instance-settings",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-invalid-instance-write",
                "settings": {
                    "instance_name": "Must not persist",
                    "default_theme": "neon",
                },
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.database.get_setting("instance_name"), original_name)
        audit_entries = self.database.get_config_audit_log(limit=None)
        self.assertFalse(any(
            entry.get("conversation_id") == "conversation-invalid-instance-write"
            for entry in audit_entries
        ))

    def test_read_deployment_settings_returns_masked_secret_status(self) -> None:
        self.database.upsert_deployment_config(
            "LLM_MODEL",
            "kimi-k2-6",
            category="llm",
            requires_restart=False,
            description="Model identifier",
        )
        self.database.upsert_deployment_config(
            "LLM_API_KEY",
            "configured-secret",
            category="llm",
            is_secret=True,
            description="Model provider API key",
        )
        self.database.upsert_deployment_config(
            "SMTP_HOST",
            "smtp.example.test",
            category="email",
            description="SMTP server hostname",
        )
        self.database.upsert_deployment_config(
            "SMTP_PASS",
            "smtp-secret",
            category="email",
            is_secret=True,
            description="SMTP password",
        )

        response = self.client.post(
            "/internal/agent/admin-config/deployment-settings",
            headers=self.headers,
            json={"actor": self.admin_actor},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["tool"], "read_deployment_settings")
        self.assertEqual(payload["secret_policy"], {"mode": "masked"})
        data = payload["data"]
        self.assertEqual(data["settings"]["LLM_MODEL"]["value"], "kimi-k2-6")
        self.assertFalse(data["settings"]["LLM_MODEL"]["secret"])
        self.assertTrue(data["settings"]["LLM_MODEL"]["configured"])
        self.assertEqual(data["settings"]["LLM_API_KEY"]["value"], "********")
        self.assertTrue(data["settings"]["LLM_API_KEY"]["secret"])
        self.assertTrue(data["settings"]["LLM_API_KEY"]["configured"])
        self.assertEqual(data["settings"]["SMTP_HOST"]["value"], "smtp.example.test")
        self.assertEqual(data["settings"]["SMTP_PASS"]["value"], "********")
        self.assertIn("LLM_MODEL", data["categories"]["llm"])
        self.assertIn("SMTP_HOST", data["categories"]["email"])
        self.assertNotIn("configured-secret", response.text)
        self.assertNotIn("smtp-secret", response.text)

    def test_update_deployment_settings_reports_restart_and_redacts_secret(self) -> None:
        secret = "deployment-secret-for-direct-tool"
        response = self.client.post(
            "/internal/agent/admin-config/update-deployment-settings",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-deployment-write",
                "settings": {
                    "LLM_API_URL": "https://inference.example.test/v1",
                    "LLM_API_KEY": secret,
                },
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["tool"], "update_deployment_settings")
        self.assertEqual(payload["data"]["outcome"], "succeeded")
        self.assertTrue(payload["data"]["restart_required"])
        self.assertEqual(payload["data"]["restart_required_keys"], ["LLM_API_URL"])
        self.assertEqual(
            payload["data"]["saved_values"],
            {
                "LLM_API_KEY": "********",
                "LLM_API_URL": "https://inference.example.test/v1",
            },
        )
        self.assertNotIn(secret, response.text)
        self.assertEqual(self.database.get_deployment_config_value("LLM_API_KEY"), secret)

        audit_entries = self.database.get_config_audit_log(
            limit=None,
            table_name="deployment_config",
        )
        direct_entries = [
            entry
            for entry in audit_entries
            if entry.get("conversation_id") == "conversation-deployment-write"
        ]
        self.assertEqual(
            {entry["config_key"] for entry in direct_entries},
            {"LLM_API_KEY", "LLM_API_URL"},
        )
        self.assertNotIn(secret, repr(direct_entries))

        reveal = self.client.post(
            "/internal/agent/admin-config/read-deployment-secret",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-secret-read",
                "key": "LLM_API_KEY",
            },
        )
        self.assertEqual(reveal.status_code, 200, reveal.text)
        self.assertEqual(reveal.json()["tool"], "read_deployment_secret")
        self.assertEqual(reveal.json()["secret_policy"], {"mode": "explicit_secret"})
        self.assertEqual(reveal.json()["data"], {"key": "LLM_API_KEY", "value": secret})
        reveal_audit = [
            entry
            for entry in self.database.get_config_audit_log(
                limit=None,
                table_name="deployment_config_secret_reads",
            )
            if entry.get("conversation_id") == "conversation-secret-read"
        ]
        self.assertEqual(len(reveal_audit), 1)
        self.assertEqual(reveal_audit[0]["config_key"], "LLM_API_KEY")
        self.assertEqual(reveal_audit[0]["action_source"], "sage_conversation")
        self.assertNotIn(secret, repr(reveal_audit))

        blank_conversation = self.client.post(
            "/internal/agent/admin-config/read-deployment-secret",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "   ",
                "key": "LLM_API_KEY",
            },
        )
        self.assertEqual(blank_conversation.status_code, 422)

    def test_update_deployment_settings_preserves_runtime_side_effects(self) -> None:
        self.database.upsert_deployment_config(
            key="SMTP_LAST_TEST_SUCCESS",
            value="true",
            category="email",
            description="Whether last SMTP test was successful",
        )
        invalidations: list[bool] = []
        config_loader = types.SimpleNamespace(
            invalidate_cache=lambda: invalidations.append(True)
        )

        with mock.patch.dict(sys.modules, {"config_loader": config_loader}):
            response = self.client.post(
                "/internal/agent/admin-config/update-deployment-settings",
                headers=self.headers,
                json={
                    "actor": self.admin_actor,
                    "conversation_id": "conversation-smtp-write",
                    "settings": {"SMTP_HOST": "smtp.example.test"},
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.database.get_deployment_config_value("SMTP_LAST_TEST_SUCCESS"),
            "false",
        )
        self.assertEqual(invalidations, [True])

        failing_loader = types.SimpleNamespace(
            invalidate_cache=lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable"))
        )
        with mock.patch.dict(sys.modules, {"config_loader": failing_loader}):
            committed = self.client.post(
                "/internal/agent/admin-config/update-deployment-settings",
                headers=self.headers,
                json={
                    "actor": self.admin_actor,
                    "conversation_id": "conversation-cache-failure",
                    "settings": {"LLM_MODEL": "cache-failure-still-commits"},
                },
            )
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertTrue(committed.json()["warnings"])
        self.assertIn("may remain stale", committed.json()["warnings"][0])
        self.assertEqual(
            self.database.get_deployment_config_value("LLM_MODEL"),
            "cache-failure-still-commits",
        )

    def test_update_deployment_settings_rejects_unsafe_url_schemes(self) -> None:
        cases = [
            ("LLM_API_URL", "javascript://example.test/payload"),
            ("EMBEDDING_API_URL", "file://example.test/tmp/model"),
            ("SEARXNG_URL", "ftp://example.test/search"),
            ("FRONTEND_URL", "data://example.test/value"),
            ("RATE_LIMIT_VALKEY_URL", "https://example.test/not-redis"),
            ("FRONTEND_URL", "https://[broken-host"),
            ("FRONTEND_URL", "https://example.test:notaport"),
            ("SMTP_HOST", "smtp.example.test:notaport"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                response = self.client.post(
                    "/internal/agent/admin-config/update-deployment-settings",
                    headers=self.headers,
                    json={
                        "actor": self.admin_actor,
                        "conversation_id": f"conversation-invalid-url-{key}",
                        "settings": {key: value},
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_update_deployment_settings_rejects_masked_secret_placeholder(self) -> None:
        self.database.upsert_deployment_config(
            "LLM_API_KEY",
            "real-secret",
            is_secret=True,
            category="llm",
        )
        response = self.client.post(
            "/internal/agent/admin-config/update-deployment-settings",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-masked-secret",
                "settings": {"LLM_API_KEY": "********"},
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            self.database.get_deployment_config_value("LLM_API_KEY"),
            "real-secret",
        )

    def test_update_deployment_settings_rejects_invalid_batch_without_mutation(self) -> None:
        original_model = self.database.get_deployment_config_value("LLM_MODEL")
        response = self.client.post(
            "/internal/agent/admin-config/update-deployment-settings",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-invalid-deployment-write",
                "settings": {
                    "LLM_MODEL": "must-not-persist",
                    "UNKNOWN_SETTING": "invalid",
                },
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.database.get_deployment_config_value("LLM_MODEL"), original_model)
        self.assertFalse(any(
            entry.get("conversation_id") == "conversation-invalid-deployment-write"
            for entry in self.database.get_config_audit_log(limit=None)
        ))

    def test_empty_secret_update_preserves_latest_value_inside_write_transaction(self) -> None:
        self.database.upsert_deployment_config(
            "LLM_API_KEY",
            "preserve-this-secret",
            is_secret=True,
            category="llm",
        )

        response = self.client.post(
            "/internal/agent/admin-config/update-deployment-settings",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-preserve-secret",
                "settings": {"LLM_API_KEY": ""},
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.database.get_deployment_config_value("LLM_API_KEY"),
            "preserve-this-secret",
        )
        self.assertNotIn("preserve-this-secret", response.text)

    def test_deployment_batch_detects_metadata_changes_and_masks_secret_transition(self) -> None:
        self.database.upsert_deployment_config(
            "LLM_API_KEY",
            "same-value",
            is_secret=False,
            requires_restart=False,
            category="general",
            description="Old metadata",
        )

        result = self.database.update_deployment_configs_with_audit(
            {
                "LLM_API_KEY": {
                    "value": "same-value",
                    "is_secret": True,
                    "requires_restart": True,
                    "category": "llm",
                    "description": "Model provider API key",
                }
            },
            changed_by="abc123",
            action_source="sage_conversation",
            conversation_id="conversation-secret-transition",
        )

        self.assertEqual(result["changed_names"], ["LLM_API_KEY"])
        entry = next(
            item
            for item in self.database.get_config_audit_log(
                limit=None,
                table_name="deployment_config",
            )
            if item.get("conversation_id") == "conversation-secret-transition"
        )
        self.assertEqual(entry["old_value"], "********")
        self.assertEqual(entry["new_value"], "********")
        self.assertNotIn("same-value", repr(entry))

    def test_read_agent_settings_returns_global_and_user_type_effective_values(self) -> None:
        user_type_id = self.database.create_user_type(
            "Advocates",
            "People doing direct support work",
            icon="Users",
            display_order=7,
        )
        self.database.update_ai_config("temperature", "0.4", changed_by="abc123")
        self.database.upsert_ai_config_override(
            "temperature",
            user_type_id,
            "0.2",
            changed_by="abc123",
        )

        response = self.client.post(
            "/internal/agent/admin-config/agent-settings",
            headers=self.headers,
            json={"actor": self.admin_actor},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["tool"], "read_agent_settings")
        self.assertEqual(payload["secret_policy"], {"mode": "masked"})
        data = payload["data"]
        self.assertEqual(
            data["global"]["parameters"]["temperature"]["value"],
            "0.4",
        )
        self.assertIn("prompt_system", data["global"]["prompt_sections"])
        self.assertIn("admin_trace_visibility", data["global"]["defaults"])
        advocate_settings = next(
            item for item in data["per_user_type"]
            if item["user_type_id"] == user_type_id
        )
        self.assertEqual(advocate_settings["user_type_name"], "Advocates")
        self.assertEqual(
            advocate_settings["overrides"]["temperature"]["value"],
            "0.2",
        )
        self.assertEqual(
            advocate_settings["effective_values"]["parameters"]["temperature"]["value"],
            "0.2",
        )
        self.assertTrue(
            advocate_settings["effective_values"]["parameters"]["temperature"]["is_override"]
        )
        self.assertEqual(data["limits"]["user_types_returned"], 1)
        self.assertNotIn("test-secret", response.text)

    def _agent_settings_audit(self, conversation_id: str) -> list[dict]:
        return [
            entry
            for entry in self.database.get_config_audit_log(limit=None)
            if entry.get("conversation_id") == conversation_id
        ]

    def test_record_agent_settings_change_audits_sage_writes_with_provenance(self) -> None:
        user_type_id = self.database.create_user_type("Case Workers")
        original_temperature = self.database.get_ai_config("temperature")["value"]

        response = self.client.post(
            "/internal/agent/admin-config/record-agent-settings-change",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-agent-override",
                "tool_name": "update_agent_settings",
                "user_type_id": user_type_id,
                "changes": [
                    {"key": "temperature", "old_value": None, "new_value": "0.4"},
                    {"key": "prompt_tone", "old_value": "Warm.", "new_value": None},
                ],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tool"], "record_agent_settings_change")
        self.assertEqual(response.json()["data"]["recorded"], 2)
        entries = self._agent_settings_audit("conversation-agent-override")
        by_key = {entry["config_key"]: entry for entry in entries}
        override = by_key[f"temperature:type_{user_type_id}"]
        self.assertEqual(override["table_name"], "ai_config_user_type_overrides")
        self.assertIsNone(override["old_value"])
        self.assertEqual(override["new_value"], "0.4")
        reverted = by_key[f"prompt_tone:type_{user_type_id}"]
        self.assertEqual(reverted["old_value"], "Warm.")
        self.assertEqual(reverted["new_value"], "(reverted to global)")
        tool_entry = by_key["update_agent_settings"]
        self.assertEqual(tool_entry["table_name"], "admin_config_tools")
        outcome = json.loads(tool_entry["new_value"])
        self.assertEqual(outcome["changed_names"], ["prompt_tone", "temperature"])
        self.assertEqual(outcome["affected_areas"], ["agent_settings"])
        self.assertEqual(outcome["store"], "sage")
        self.assertEqual(outcome["user_type_id"], user_type_id)
        self.assertEqual(outcome["reverted_keys"], ["prompt_tone"])
        self.assertTrue(all(entry["changed_by"] == "abc123" for entry in entries))
        self.assertTrue(all(entry["action_source"] == "sage_conversation" for entry in entries))
        # Agent Settings live in Sage; the control plane only records the change.
        self.assertEqual(self.database.get_ai_config("temperature")["value"], original_temperature)
        self.assertIsNone(self.database.get_ai_config_override("temperature", user_type_id))

    def test_record_agent_settings_change_names_guided_setup_changes(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/record-agent-settings-change",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-guided-agent",
                "tool_name": "configure_instance",
                "changes": [
                    {"key": "prompt_rules", "old_value": "[]", "new_value": "[\"Be brief.\"]"},
                ],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        by_key = {
            entry["config_key"]: entry
            for entry in self._agent_settings_audit("conversation-guided-agent")
        }
        self.assertEqual(by_key["prompt_rules"]["table_name"], "ai_config")
        self.assertEqual(by_key["prompt_rules"]["new_value"], "[\"Be brief.\"]")
        outcome = json.loads(by_key["configure_instance"]["new_value"])
        self.assertEqual(outcome["changed_names"], ["agent_settings.prompt_rules"])

    def test_record_agent_settings_change_rejects_invalid_requests_without_audit(self) -> None:
        cases = [
            (
                {"actor": self.admin_actor, "user_type_id": 9999, "tool_name": "update_agent_settings"},
                422,
            ),
            (
                {"actor": self.admin_actor, "tool_name": "update_instance_settings"},
                422,
            ),
            (
                {
                    "actor": {"id": 42, "type": "user", "approved": True, "email": "user@example.test"},
                    "tool_name": "update_agent_settings",
                },
                403,
            ),
            (
                {
                    "actor": self.admin_actor,
                    "tool_name": "update_agent_settings",
                    "changes": [{"key": "temperature", "old_value": "0.1", "new_value": None}],
                },
                422,
            ),
            (
                {
                    "actor": self.admin_actor,
                    "tool_name": "update_agent_settings",
                    "changes": [{"key": "prompt_tone", "old_value": "x" * 20_001, "new_value": "y"}],
                },
                422,
            ),
        ]
        for body, status in cases:
            response = self.client.post(
                "/internal/agent/admin-config/record-agent-settings-change",
                headers=self.headers,
                json={
                    "changes": [{"key": "temperature", "old_value": "0.1", "new_value": "0.2"}],
                    **body,
                    "conversation_id": "conversation-rejected-record",
                },
            )
            self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(self._agent_settings_audit("conversation-rejected-record"), [])

    def test_control_plane_no_longer_writes_agent_settings(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/update-agent-settings",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-removed-write",
                "updates": {"temperature": "0.25"},
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(hasattr(self.database, "update_agent_settings_with_audit"))

    def test_read_user_types_returns_onboarding_questions(self) -> None:
        user_type_id = self.database.create_user_type(
            "Family",
            "Family members seeking support",
            icon="Heart",
            display_order=3,
        )
        field_id = self.database.create_field_definition(
            "preferred_language",
            "select",
            required=True,
            display_order=2,
            user_type_id=user_type_id,
            placeholder="Choose a language",
            options=["English", "Spanish"],
            encryption_enabled=False,
            include_in_chat=True,
        )

        response = self.client.post(
            "/internal/agent/admin-config/user-types",
            headers=self.headers,
            json={"actor": self.admin_actor},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["tool"], "read_user_types")
        data = payload["data"]
        family = next(
            item for item in data["user_types"]
            if item["id"] == user_type_id
        )
        self.assertEqual(family["name"], "Family")
        self.assertEqual(family["icon"], "Heart")
        self.assertEqual(family["display_order"], 3)
        self.assertEqual(family["onboarding_fields"][0]["id"], field_id)
        question = next(
            item for item in data["onboarding_questions"]
            if item["id"] == field_id
        )
        self.assertEqual(question["user_type_id"], user_type_id)
        self.assertEqual(question["name"], "preferred_language")
        self.assertEqual(question["label"], "Preferred language")
        self.assertEqual(question["field_type"], "select")
        self.assertTrue(question["required"])
        self.assertEqual(question["placeholder"], "Choose a language")
        self.assertEqual(question["options"], ["English", "Spanish"])
        self.assertTrue(question["include_in_chat"])
        self.assertEqual(data["limits"]["user_types_returned"], 1)
        self.assertEqual(data["limits"]["onboarding_questions_returned"], 1)

    def test_manage_user_types_supports_audited_lifecycle(self) -> None:
        create = self.client.post(
            "/internal/agent/admin-config/manage-user-types",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-user-type-create",
                "operation": "create",
                "name": "  Legal Teams  ",
                "description": "Legal support organizations",
                "icon": "Scale",
                "display_order": 4,
            },
        )
        self.assertEqual(create.status_code, 200, create.text)
        created = create.json()["data"]["user_type"]
        self.assertEqual(created["name"], "Legal Teams")
        self.assertEqual(create.json()["data"]["affected_areas"], ["user_types"])

        update = self.client.post(
            "/internal/agent/admin-config/manage-user-types",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-user-type-update",
                "operation": "update",
                "user_type_id": created["id"],
                "name": "Legal Advocates",
                "display_order": 2,
            },
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(update.json()["data"]["user_type"]["name"], "Legal Advocates")

        delete = self.client.post(
            "/internal/agent/admin-config/manage-user-types",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-user-type-delete",
                "operation": "delete",
                "user_type_id": created["id"],
            },
        )
        self.assertEqual(delete.status_code, 200, delete.text)
        self.assertEqual(delete.json()["data"]["deleted_user_type_id"], created["id"])
        self.assertEqual(
            delete.json()["data"]["affected_areas"],
            [
                "user_types",
                "onboarding_questions",
                "agent_settings",
                "document_access",
            ],
        )
        self.assertIsNone(self.database.get_user_type(created["id"]))

        audit_entries = self.database.get_config_audit_log(limit=None, table_name="user_types")
        self.assertEqual(
            {
                entry["conversation_id"]
                for entry in audit_entries
                if entry["action_source"] == "sage_conversation"
            },
            {
                "conversation-user-type-create",
                "conversation-user-type-update",
                "conversation-user-type-delete",
            },
        )

    def test_manage_user_types_rejects_duplicate_without_partial_create(self) -> None:
        self.database.create_user_type("Families")
        response = self.client.post(
            "/internal/agent/admin-config/manage-user-types",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-duplicate-user-type",
                "operation": "create",
                "name": "Families",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            len([item for item in self.database.list_user_types() if item["name"] == "Families"]),
            1,
        )

    def test_manage_user_types_rejects_whitespace_only_name(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/manage-user-types",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-blank-user-type",
                "operation": "create",
                "name": "   ",
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.database.list_user_types(), [])

        existing_id = self.database.create_user_type("Existing Type")
        null_name = self.client.post(
            "/internal/agent/admin-config/manage-user-types",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-null-user-type",
                "operation": "update",
                "user_type_id": existing_id,
                "name": None,
            },
        )
        self.assertEqual(null_name.status_code, 422)

    def test_manage_user_types_skips_no_op_update_audit_and_refresh(self) -> None:
        user_type_id = self.database.create_user_type(
            "Families",
            "Family support",
            icon="Heart",
            display_order=2,
        )
        response = self.client.post(
            "/internal/agent/admin-config/manage-user-types",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-user-type-no-op",
                "operation": "update",
                "user_type_id": user_type_id,
                "name": "Families",
                "description": "Family support",
                "icon": "Heart",
                "display_order": 2,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["changed_names"], [])
        self.assertEqual(response.json()["data"]["affected_areas"], [])
        self.assertFalse(any(
            entry.get("conversation_id") == "conversation-user-type-no-op"
            for entry in self.database.get_config_audit_log(limit=None)
        ))

    def test_manage_user_types_preserves_question_answers_on_delete(self) -> None:
        user_type_id = self.database.create_user_type("Families")
        question_id = self.database.create_field_definition(
            "case_note",
            "text",
            user_type_id=user_type_id,
            encryption_enabled=False,
        )
        with self.database.get_write_cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (pubkey, approved, user_type_id) VALUES (?, ?, ?)",
                ("user-type-saved-answer", 1, user_type_id),
            )
            user_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO user_field_values (user_id, field_id, value) VALUES (?, ?, ?)",
                (user_id, question_id, "Keep this answer"),
            )

        response = self.client.post(
            "/internal/agent/admin-config/manage-user-types",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-user-type-delete-with-answer",
                "operation": "delete",
                "user_type_id": user_type_id,
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("saved user answers", response.json()["detail"])
        self.assertIsNotNone(self.database.get_user_type(user_type_id))
        self.assertIsNotNone(self.database.get_field_definition_by_id(question_id))
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "SELECT value FROM user_field_values WHERE user_id = ? AND field_id = ?",
                (user_id, question_id),
            )
            self.assertEqual(cursor.fetchone()["value"], "Keep this answer")

    def test_manage_onboarding_questions_supports_full_lifecycle(self) -> None:
        user_type_id = self.database.create_user_type("Families")
        create = self.client.post(
            "/internal/agent/admin-config/manage-onboarding-questions",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-question-create",
                "operation": "create",
                "field_name": "preferred_language",
                "field_type": "select",
                "required": True,
                "display_order": 3,
                "user_type_id": user_type_id,
                "placeholder": "Choose a language",
                "options": ["English", "Spanish"],
                "encryption_enabled": False,
                "include_in_chat": True,
            },
        )
        self.assertEqual(create.status_code, 200, create.text)
        question = create.json()["data"]["onboarding_question"]
        self.assertEqual(question["options"], ["English", "Spanish"])
        self.assertTrue(question["include_in_chat"])

        update = self.client.post(
            "/internal/agent/admin-config/manage-onboarding-questions",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-question-update",
                "operation": "update",
                "question_id": question["id"],
                "display_order": 1,
                "options": ["English", "Spanish", "French"],
            },
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertEqual(update.json()["data"]["onboarding_question"]["display_order"], 1)

        delete = self.client.post(
            "/internal/agent/admin-config/manage-onboarding-questions",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-question-delete",
                "operation": "delete",
                "question_id": question["id"],
            },
        )
        self.assertEqual(delete.status_code, 200, delete.text)
        self.assertIsNone(self.database.get_field_definition_by_id(question["id"]))

    def test_manage_onboarding_questions_rejects_encrypted_chat_field(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/manage-onboarding-questions",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-invalid-question",
                "operation": "create",
                "field_name": "private_case_note",
                "field_type": "text",
                "encryption_enabled": True,
                "include_in_chat": True,
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.database.get_field_definition_by_name("private_case_note"))

    def test_manage_onboarding_questions_skips_no_op_update(self) -> None:
        question_id = self.database.create_field_definition(
            "preferred_language",
            "select",
            required=True,
            display_order=3,
            options=["English", "Spanish"],
            encryption_enabled=False,
            include_in_chat=True,
        )
        response = self.client.post(
            "/internal/agent/admin-config/manage-onboarding-questions",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-question-no-op",
                "operation": "update",
                "question_id": question_id,
                "required": True,
                "display_order": 3,
                "options": ["English", "Spanish"],
                "encryption_enabled": False,
                "include_in_chat": True,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["changed_names"], [])
        self.assertEqual(response.json()["data"]["affected_areas"], [])
        self.assertFalse(any(
            entry.get("conversation_id") == "conversation-question-no-op"
            for entry in self.database.get_config_audit_log(limit=None)
        ))

    def test_manage_onboarding_questions_preserves_saved_answers_on_delete(self) -> None:
        question_id = self.database.create_field_definition(
            "case_note",
            "text",
            encryption_enabled=False,
        )
        with self.database.get_write_cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (pubkey, approved) VALUES (?, ?)",
                ("saved-answer-user", 1),
            )
            user_id = int(cursor.lastrowid)
            cursor.execute(
                "INSERT INTO user_field_values (user_id, field_id, value) VALUES (?, ?, ?)",
                (user_id, question_id, "Keep this answer"),
            )

        response = self.client.post(
            "/internal/agent/admin-config/manage-onboarding-questions",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-question-delete-with-answer",
                "operation": "delete",
                "question_id": question_id,
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("saved user answers", response.json()["detail"])
        self.assertIsNotNone(self.database.get_field_definition_by_id(question_id))
        with self.database.get_cursor() as cursor:
            cursor.execute(
                "SELECT value FROM user_field_values WHERE user_id = ? AND field_id = ?",
                (user_id, question_id),
            )
            self.assertEqual(cursor.fetchone()["value"], "Keep this answer")

    def test_read_document_access_returns_global_and_user_type_effective_state(self) -> None:
        user_type_id = self.database.create_user_type(
            "Advocates",
            "People doing direct support work",
            icon="Users",
            display_order=5,
        )
        self.ingest_db.create_job(
            "doc-global",
            "Global Handbook.pdf",
            "/uploads/global.pdf",
            "default",
        )
        self.ingest_db.update_job_status(
            "doc-global",
            "completed",
            total_chunks=3,
            processed_chunks=3,
        )
        self.ingest_db.create_job(
            "doc-optional",
            "Optional Briefing.pdf",
            "/uploads/optional.pdf",
            "default",
        )
        self.ingest_db.update_job_status(
            "doc-optional",
            "completed",
            total_chunks=2,
            processed_chunks=2,
        )
        self.database.upsert_document_defaults(
            "doc-global",
            is_available=True,
            is_default_active=True,
            display_order=1,
            changed_by="abc123",
        )
        self.database.upsert_document_defaults(
            "doc-optional",
            is_available=True,
            is_default_active=False,
            display_order=2,
            changed_by="abc123",
        )
        self.database.upsert_document_defaults_override(
            "doc-optional",
            user_type_id,
            is_available=True,
            is_default_active=True,
            changed_by="abc123",
        )

        response = self.client.post(
            "/internal/agent/admin-config/document-access",
            headers=self.headers,
            json={"actor": self.admin_actor},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["tool"], "read_document_access")
        self.assertEqual(payload["secret_policy"], {"mode": "masked"})
        data = payload["data"]
        self.assertEqual(data["global"]["available_document_ids"], ["doc-global", "doc-optional"])
        self.assertEqual(data["global"]["default_document_ids"], ["doc-global"])
        documents_by_id = {document["job_id"]: document for document in data["documents"]}
        self.assertEqual(documents_by_id["doc-global"]["filename"], "Global Handbook.pdf")
        self.assertEqual(documents_by_id["doc-global"]["status"], "completed")
        self.assertEqual(documents_by_id["doc-global"]["total_chunks"], 3)
        advocate_access = next(
            item for item in data["per_user_type"]
            if item["user_type_id"] == user_type_id
        )
        self.assertEqual(advocate_access["user_type_name"], "Advocates")
        self.assertEqual(
            advocate_access["available_document_ids"],
            ["doc-global", "doc-optional"],
        )
        self.assertEqual(
            advocate_access["default_document_ids"],
            ["doc-global", "doc-optional"],
        )
        effective_optional = next(
            document for document in advocate_access["documents"]
            if document["job_id"] == "doc-optional"
        )
        self.assertTrue(effective_optional["is_override"])
        self.assertTrue(effective_optional["is_default_active"])
        self.assertEqual(data["limits"]["documents_returned"], 2)
        self.assertEqual(data["limits"]["user_types_returned"], 1)

    def test_update_document_access_supports_batch_override_and_revert(self) -> None:
        user_type_id = self.database.create_user_type("Advocates")
        self.ingest_db.create_job("doc-direct", "Direct Guide.pdf", "/uploads/direct.pdf", "default")
        self.ingest_db.update_job_status(
            "doc-direct",
            "completed",
            total_chunks=1,
            processed_chunks=1,
        )

        global_update = self.client.post(
            "/internal/agent/admin-config/update-document-access",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-document-global",
                "updates": [{
                    "job_id": "doc-direct",
                    "is_available": True,
                    "is_default_active": False,
                    "display_order": 6,
                }],
            },
        )
        self.assertEqual(global_update.status_code, 200, global_update.text)
        self.assertFalse(self.database.get_document_defaults("doc-direct")["is_default_active"])

        no_op = self.client.post(
            "/internal/agent/admin-config/update-document-access",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-document-no-op",
                "updates": [{
                    "job_id": "doc-direct",
                    "is_available": True,
                    "is_default_active": False,
                    "display_order": 6,
                }],
            },
        )
        self.assertEqual(no_op.status_code, 200, no_op.text)
        self.assertEqual(no_op.json()["data"]["changed_names"], [])
        self.assertFalse(any(
            entry.get("conversation_id") == "conversation-document-no-op"
            and entry.get("table_name") == "document_defaults"
            for entry in self.database.get_config_audit_log(limit=None)
        ))

        override = self.client.post(
            "/internal/agent/admin-config/update-document-access",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-document-override",
                "user_type_id": user_type_id,
                "updates": [{
                    "job_id": "doc-direct",
                    "is_default_active": True,
                }],
            },
        )
        self.assertEqual(override.status_code, 200, override.text)
        self.assertTrue(
            self.database.get_document_defaults_override("doc-direct", user_type_id)["is_default_active"]
        )

        revert = self.client.post(
            "/internal/agent/admin-config/update-document-access",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-document-revert",
                "user_type_id": user_type_id,
                "revert_job_ids": ["doc-direct"],
            },
        )
        self.assertEqual(revert.status_code, 200, revert.text)
        self.assertIsNone(self.database.get_document_defaults_override("doc-direct", user_type_id))
        self.assertEqual(revert.json()["data"]["reverted_job_ids"], ["doc-direct"])

    def test_update_document_access_rejects_unknown_document_without_mutation(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/update-document-access",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-invalid-document-access",
                "updates": [{
                    "job_id": "missing-document",
                    "is_available": True,
                }],
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.database.get_document_defaults("missing-document"))

    def test_update_document_access_rejects_duplicate_jobs_without_mutation(self) -> None:
        self.ingest_db.create_job(
            "doc-duplicate",
            "Duplicate.pdf",
            "/uploads/duplicate.pdf",
            "default",
        )
        response = self.client.post(
            "/internal/agent/admin-config/update-document-access",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-duplicate-document",
                "updates": [
                    {"job_id": "doc-duplicate", "is_available": False},
                    {"job_id": "doc-duplicate", "is_available": True},
                ],
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIsNone(self.database.get_document_defaults("doc-duplicate"))

    def test_configure_instance_applies_guided_setup_atomically_with_audit(self) -> None:
        original_rules = self.database.get_ai_config("prompt_rules")["value"]
        original_forbidden = self.database.get_ai_config("prompt_forbidden")["value"]
        response = self.client.post(
            "/internal/agent/admin-config/configure-instance",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-guided-setup",
                "settings": {
                    "instance_name": "Freedom Network",
                    "assistant_name": "Sage Ally",
                    "header_tagline": "Support for political prisoners and their families",
                    "description": "Private coordination and trusted support resources.",
                    "primary_color": "#6d28d9",
                    "default_theme": "dark",
                    "default_language": "en",
                    "auto_approve_users": False,
                    "chat_bubble_style": "rounded",
                    "chat_bubble_shadow": True,
                    "surface_style": "soft",
                    "status_icon_set": "minimal",
                    "typography_preset": "humanist",
                },
                "user_types": [{
                    "reference": "families",
                    "name": "Families",
                    "description": "Family members seeking support",
                    "icon": "Heart",
                    "display_order": 1,
                }],
                "onboarding_questions": [{
                    "field_name": "Preferred language",
                    "field_type": "select",
                    "required": True,
                    "display_order": 1,
                    "user_type_reference": "families",
                    "placeholder": "Choose a language",
                    "options": ["English", "Spanish"],
                    "encryption_enabled": False,
                    "include_in_chat": True,
                }],
                "behavior_rules": [
                    "Prioritize practical, verified support steps.",
                    "State uncertainty plainly.",
                ],
                "forbidden_topics": ["Never invent organizations or contact details."],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["tool"], "configure_instance")
        self.assertEqual(payload["data"]["outcome"], "succeeded")
        self.assertEqual(payload["data"]["validation"], {"status": "valid"})
        # Sage applies and audits the guided Agent Settings in its own store.
        self.assertEqual(
            payload["data"]["affected_areas"],
            [
                "instance_settings",
                "user_types",
                "onboarding_questions",
            ],
        )
        self.assertEqual(
            payload["data"]["saved_setup"]["settings"]["primary_color"],
            "#6d28d9",
        )
        created_type = payload["data"]["saved_setup"]["user_types"][0]
        self.assertEqual(created_type["reference"], "families")
        self.assertEqual(created_type["name"], "Families")
        created_question = payload["data"]["saved_setup"]["onboarding_questions"][0]
        self.assertEqual(created_question["user_type_id"], created_type["id"])
        self.assertEqual(created_question["options"], ["English", "Spanish"])
        self.assertNotIn("behavior_rules", payload["data"]["saved_setup"])

        self.assertEqual(self.database.get_setting("instance_name"), "Freedom Network")
        self.assertEqual(self.database.get_user_type(created_type["id"])["name"], "Families")
        self.assertEqual(
            self.database.get_field_definition_by_id(created_question["id"])["field_name"],
            "Preferred language",
        )
        self.assertEqual(self.database.get_ai_config("prompt_rules")["value"], original_rules)
        self.assertEqual(self.database.get_ai_config("prompt_forbidden")["value"], original_forbidden)
        self.assertFalse(any(
            entry["table_name"] == "ai_config"
            for entry in self.database.get_config_audit_log(limit=None)
            if entry.get("conversation_id") == "conversation-guided-setup"
        ))

        audit_entries = [
            entry
            for entry in self.database.get_config_audit_log(limit=None)
            if entry.get("conversation_id") == "conversation-guided-setup"
        ]
        self.assertGreaterEqual(len(audit_entries), 5)
        self.assertTrue(all(entry["changed_by"] == "abc123" for entry in audit_entries))
        self.assertTrue(
            all(entry["action_source"] == "sage_conversation" for entry in audit_entries)
        )
        self.assertNotIn(
            "conversation-guided-setup",
            " ".join(
                str(entry.get("old_value") or "") + str(entry.get("new_value") or "")
                for entry in audit_entries
            ),
        )

    def test_configure_instance_rolls_back_every_area_on_late_relational_failure(self) -> None:
        original_name = self.database.get_setting("instance_name")
        original_rules = self.database.get_ai_config("prompt_rules")["value"]
        response = self.client.post(
            "/internal/agent/admin-config/configure-instance",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-guided-rollback",
                "settings": {
                    "instance_name": "Must Roll Back",
                    "assistant_name": "Rollback Ally",
                    "header_tagline": "This must not persist",
                    "description": "This entire setup should roll back.",
                    "primary_color": "#123456",
                    "default_theme": "light",
                    "default_language": "en",
                    "auto_approve_users": True,
                },
                "user_types": [{
                    "reference": "duplicate-scope",
                    "name": "Must Not Exist",
                }],
                "onboarding_questions": [
                    {
                        "field_name": "Duplicate question",
                        "field_type": "text",
                        "user_type_reference": "duplicate-scope",
                    },
                    {
                        "field_name": "Duplicate question",
                        "field_type": "textarea",
                        "user_type_reference": "duplicate-scope",
                    },
                ],
                "behavior_rules": ["Must not persist"],
            },
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.database.get_setting("instance_name"), original_name)
        self.assertEqual(self.database.get_ai_config("prompt_rules")["value"], original_rules)
        self.assertFalse(any(
            user_type["name"] == "Must Not Exist"
            for user_type in self.database.list_user_types()
        ))
        self.assertIsNone(
            self.database.get_field_definition_by_name("Duplicate question")
        )
        self.assertFalse(any(
            entry.get("conversation_id") == "conversation-guided-rollback"
            for entry in self.database.get_config_audit_log(limit=None)
        ))

    def test_configure_instance_rejects_whitespace_only_user_type_name(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/configure-instance",
            headers=self.headers,
            json={
                "actor": self.admin_actor,
                "conversation_id": "conversation-blank-guided-user-type",
                "settings": {
                    "instance_name": "Freedom Network",
                    "assistant_name": "Sage Ally",
                    "header_tagline": "Trusted support",
                    "description": "Private coordination and support resources.",
                    "primary_color": "#6d28d9",
                    "default_theme": "dark",
                    "default_language": "en",
                    "auto_approve_users": False,
                },
                "user_types": [{"reference": "blank", "name": "   "}],
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.database.list_user_types(), [])

    def test_configure_instance_rejects_non_admin_actor(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/configure-instance",
            headers=self.headers,
            json={
                "actor": {
                    "id": 99,
                    "type": "user",
                    "approved": True,
                    "pubkey": "user-pubkey",
                },
                "conversation_id": "conversation-unauthorized-setup",
                "settings": {
                    "instance_name": "Blocked",
                    "assistant_name": "Blocked",
                    "header_tagline": "Blocked",
                    "description": "Blocked",
                    "primary_color": "#123456",
                    "default_theme": "light",
                    "default_language": "en",
                    "auto_approve_users": False,
                },
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(self.database.get_setting("instance_name"), "Blocked")

    def test_read_onboarding_status_returns_setup_flags_and_guided_checklist(self) -> None:
        user_type_id = self.database.create_user_type(
            "Family",
            "Family members seeking support",
            icon="Heart",
            display_order=3,
        )
        self.database.create_field_definition(
            "preferred_language",
            "select",
            required=True,
            user_type_id=user_type_id,
            options=["English", "Spanish"],
            include_in_chat=True,
        )
        self.database.update_settings({
            "instance_name": "FreeThem",
            "assistant_name": "Ally",
            "header_tagline": "Political Prisoner Support Team.",
            "description": "Support families and organizers.",
            "primary_color": "#8B5CF6",
            "default_theme": "dark",
            "auto_approve_users": "true",
        })
        self.database.mark_onboarding_configured_keys([
            "instance_name",
            "assistant_name",
            "header_tagline",
            "description",
            "primary_color",
            "default_theme",
            "auto_approve_users",
        ])

        response = self.client.post(
            "/internal/agent/admin-config/onboarding-status",
            headers=self.headers,
            json={"actor": self.admin_actor},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["tool"], "read_onboarding_status")
        data = payload["data"]
        self.assertTrue(data["instance"]["admin_exists"])
        self.assertTrue(data["instance"]["admin_initialized"])
        self.assertFalse(data["instance"]["setup_complete"])
        self.assertFalse(data["instance"]["ready_for_users"])
        self.assertEqual(data["instance"]["admin_count"], 1)
        guided = data["guided_bootstrap"]
        self.assertFalse(guided["complete"])
        self.assertIn("default_language", guided["missing_required_keys"])
        self.assertIn("instance_name", guided["configured_keys"])
        self.assertEqual(guided["configured_required_count"], 7)
        self.assertEqual(guided["required_count"], 8)
        self.assertEqual(data["limits"]["user_types_returned"], 1)
        self.assertEqual(data["limits"]["onboarding_questions_returned"], 1)

    def test_read_deployment_readiness_rejects_non_admin_actor(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/deployment-readiness",
            headers=self.headers,
            json={
                "actor": {
                    "id": 42,
                    "type": "user",
                    "approved": True,
                    "email": "user@example.test",
                }
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("items", response.text)

    def test_read_deployment_readiness_rejects_unregistered_admin_actor(self) -> None:
        response = self.client.post(
            "/internal/agent/admin-config/deployment-readiness",
            headers=self.headers,
            json={
                "actor": {
                    "id": 999,
                    "type": "admin",
                    "approved": True,
                    "pubkey": "not-the-admin",
                }
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("items", response.text)
