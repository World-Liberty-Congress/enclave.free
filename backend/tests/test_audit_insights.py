"""Public private-API contract for retained, masked audit reads."""
import importlib
import base64
import json
import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_admin_config_tool_contract as contract_fixture


class AuditInsightsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = contract_fixture.AdminConfigToolContractTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.database = self.fixture.database
        self.actor = self.fixture.admin_actor
        self.headers = self.fixture.headers
        module = importlib.import_module("audit_insights")
        app = FastAPI()
        app.include_router(module.router)
        app.include_router(self.fixture.internal_agent.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def read(self, **filters):
        return self.client.post("/internal/agent/admin-config/read-audit-log",
                                json={"actor": self.actor, **filters}, headers=self.headers)

    def event(self, key, old="0.4", new="0.8", source="sage_conversation", actor="abc123", table="ai_config"):
        return self.database.log_config_audit_event(table, key, old, new, actor, action_source=source)

    def test_read_masks_historical_secrets_and_unknown_values(self):
        self.event("temperature")
        self.event("retired_provider_password", "old-private-value", "new-private-value", table="deployment_config")
        self.event("prompt_rules", "embedded-old-credential", "embedded-new-credential")
        rows = []
        cursor = None
        while True:
            response = self.read(**({"cursor": cursor} if cursor else {}))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertLessEqual(len(response.content), 1200)
            self.assertNotIn("private-value", response.text)
            self.assertNotIn("credential", response.text)
            data = response.json()
            rows.extend(data["records"])
            cursor = data["next_cursor"]
            if not cursor:
                break
        values = {row["key"]: row for row in rows}
        self.assertEqual(values["temperature"]["before"], "0.4")
        self.assertEqual(values["temperature"]["after"], "0.8")
        self.assertEqual(values["retired_provider_password"]["before"], {"redacted": True})
        self.assertEqual(values["prompt_rules"]["after"], {"redacted": True})
        self.assertEqual(data["coverage"]["matched"], 3)

    def test_filters_tied_timestamp_pages_and_cursor_scope(self):
        for _ in range(4):
            self.event("temperature", source="admin_ui")
        self.event("top_k", source="admin_ui")
        self.event("temperature", source="sage_conversation")
        self.event("temperature", source="admin_ui", actor="different-admin")
        with self.database.get_write_cursor() as cursor:
            cursor.execute("UPDATE config_audit_log SET changed_at = '2026-10-23 12:00:00'")
        filters = dict(start_time="2026-10-23T07:00:00-05:00", end_time="2026-10-23T12:00:01Z",
                       actor_id="abc123", action_source="admin_ui", config_key="temperature", limit=2)
        first = self.read(**filters)
        self.assertEqual(first.status_code, 200, first.text)
        first = first.json()
        self.assertEqual([r["id"] for r in first["records"]], [4, 3])
        self.assertEqual(first["coverage"]["matched"], 4)
        self.event("temperature", source="admin_ui")
        second = self.read(**filters, cursor=first["next_cursor"]).json()
        self.assertEqual([r["id"] for r in second["records"]], [2, 1])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["coverage"]["matched"], 4)
        changed_filters = {**filters, "actor_id": "different-admin"}
        self.assertEqual(self.read(**changed_filters, cursor=first["next_cursor"]).status_code, 422)
        self.assertEqual(self.read(**{**filters, "start_time": "2026-10-23T11:59:59Z", "end_time": "2026-10-23T12:00:00Z"}).json()["records"], [])

    def test_public_branding_excerpts_and_large_metadata_fit_whole_records(self):
        self.event("instance_name", "Old cooperative", "New cooperative", table="instance_settings")
        self.event("header_tagline", "界" * 400, "welcome " * 500, table="instance_settings")
        self.event("instance_name", "retired-secret", "still-secret", table="deployment_config")
        self.event("\\" * 255, actor="\\" * 128, source="\\" * 128, table="\\" * 128)
        ids = []
        page = self.read().json()
        while True:
            self.assertTrue(page["records"])
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()), 1200)
            self.assertNotIn("retired-secret", json.dumps(page))
            for row in page["records"]:
                ids.append(row["id"])
                if row["id"] == 1:
                    self.assertEqual(row["before"], {"text": "Old cooperative", "excerpt": False})
                if row["id"] == 2:
                    self.assertTrue(row["before"]["excerpt"])
                    self.assertTrue(row["after"]["excerpt"])
                if row["id"] == 4:
                    self.assertIn("key", row["excerpted_fields"])
            if not page["next_cursor"]:
                break
            page = self.read(cursor=page["next_cursor"]).json()
        self.assertEqual(sorted(ids), [1, 2, 3, 4])

    def test_read_enforces_internal_token_and_current_admin_actor(self):
        url = "/internal/agent/admin-config/read-audit-log"
        self.assertEqual(self.client.post(url, json={"actor": self.actor}).status_code, 403)
        for override in ({"type": "user"}, {"approved": False}, {"pubkey": None}, {"id": 999}, {"pubkey": "revoked-admin"}):
            response = self.client.post(url, json={"actor": {**self.actor, **override}}, headers=self.headers)
            self.assertEqual(response.status_code, 403)
        for filters in ({"cursor": "not-a-cursor"}, {"limit": 0}, {"limit": 21}, {"limit": True},
                        {"start_time": "2026-10-23T12:00:00"},
                        {"start_time": "2026-10-24T00:00:00Z", "end_time": "2026-10-23T00:00:00Z"}):
            self.assertEqual(self.read(**filters).status_code, 422)
        self.assertEqual(self.read(config_key="' OR 1=1 --").json()["records"], [])

    def test_ui_and_agent_audits_have_distinct_validated_sources(self):
        url = "/internal/agent/admin-config/record-agent-settings-change"
        base = {"actor": self.actor, "changes": [{"key": "temperature", "old_value": "0.1", "new_value": "0.3"}]}
        ui = self.client.post(url, json={**base, "tool_name": "admin_ui_update_agent_settings", "conversation_id": ""}, headers=self.headers)
        self.assertEqual(ui.status_code, 200, ui.text)
        agent = self.client.post(url, json={**base, "tool_name": "update_agent_settings", "conversation_id": "conversation-1"}, headers=self.headers)
        self.assertEqual(agent.status_code, 200, agent.text)
        rows = self.read(config_key="temperature").json()["records"]
        self.assertEqual([r["source"] for r in rows], ["sage_conversation", "admin_ui"])
        missing = self.client.post(url, json={**base, "tool_name": "update_agent_settings"}, headers=self.headers)
        self.assertEqual(missing.status_code, 422)
        spoof = self.client.post(url, json={**base, "tool_name": "admin_ui_update_agent_settings", "conversation_id": "spoofed"}, headers=self.headers)
        self.assertEqual(spoof.status_code, 422)
        denied = self.client.post(url, json={**base, "actor": {**self.actor, "type": "user"}, "tool_name": "admin_ui_update_agent_settings"}, headers=self.headers)
        self.assertEqual(denied.status_code, 403)

    def test_control_characters_unicode_and_invalid_legacy_timestamps_make_progress(self):
        for _ in range(3):
            self.event("\u0001" * 255, actor="\u0002" * 128, source="界" * 128, table="\u0003" * 128)
        with self.database.get_write_cursor() as cursor:
            cursor.execute("UPDATE config_audit_log SET changed_at = ?", ("\u0004界" * 1000,))
        page = self.read(limit=1)
        seen = []
        for _ in range(4):
            self.assertEqual(page.status_code, 200, page.text)
            self.assertLessEqual(len(page.content), 1200)
            data = page.json()
            self.assertEqual(len(data["records"]), 1)
            row = data["records"][0]
            self.assertIn("at", row["excerpted_fields"])
            seen.append(row["id"])
            if not data["next_cursor"]:
                break
            page = self.read(limit=1, cursor=data["next_cursor"])
        self.assertEqual(seen, [3, 2, 1])

    def test_malformed_numeric_cursor_is_rejected_without_server_error(self):
        self.event("temperature")
        self.event("top_k")
        cursor = self.read(limit=1).json()["next_cursor"]
        original = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for index, value in ((0, 10**320), (0, float("inf")), (1, True), (2, 2**64)):
            modified = original.copy()
            modified[index] = value
            encoded = base64.urlsafe_b64encode(json.dumps(modified, separators=(",", ":")).encode()).decode().rstrip("=")
            self.assertEqual(self.read(cursor=encoded).status_code, 422)

    def test_hex_admin_actor_identifier_is_preserved_in_full(self):
        actor_key = "a1" * 32
        self.event("temperature", actor=actor_key)
        response = self.read(actor_id=actor_key)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.content), 1200)
        row = response.json()["records"][0]
        self.assertEqual(row["actor"], actor_key)
        self.assertNotIn("actor", row.get("excerpted_fields", []))

    def test_microsecond_filters_and_cursor_order_preserve_recorded_precision(self):
        for _ in range(3):
            self.event("temperature")
        with self.database.get_write_cursor() as cursor:
            for row_id, timestamp in (
                (1, "2026-10-23T10:00:00.123457Z"),
                (2, "2026-10-23T05:00:00.123456-05:00"),
                (3, "2026-10-23 10:00:00.123455"),
            ):
                cursor.execute("UPDATE config_audit_log SET changed_at=? WHERE id=?", (timestamp, row_id))
        narrow = self.read(start_time="2026-10-23T10:00:00.123455Z",
                           end_time="2026-10-23T10:00:00.123457Z").json()
        self.assertEqual([r["id"] for r in narrow["records"]], [2, 3])
        self.assertEqual(narrow["coverage"]["matched"], 2)
        later = self.read(start_time="2026-10-23T10:00:00.123457Z").json()
        self.assertEqual([r["id"] for r in later["records"]], [1])
        page = self.read(limit=1).json()
        seen = []
        while True:
            seen.extend(r["id"] for r in page["records"])
            if page["next_cursor"] is None:
                break
            page = self.read(limit=1, cursor=page["next_cursor"]).json()
        self.assertEqual(seen, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
