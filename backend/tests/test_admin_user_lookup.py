"""Public projection boundary; synthetic rows only."""

import hashlib
import unittest
import test_admin_db_query_endpoint as harness


class AdminUserLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        harness.AdminDbQueryEndpointTest.setUp(self)
        self.main.app.dependency_overrides.clear()
        self.pubkey = "a" * 64
        admin_id = self.database.add_admin(self.pubkey)
        self.token = self.auth.create_admin_session_token(admin_id, self.pubkey)
        self.client.headers["Authorization"] = "Bearer " + self.token

    tearDown = harness.AdminDbQueryEndpointTest.tearDown
    _restore_env = staticmethod(harness.AdminDbQueryEndpointTest._restore_env)
    _restore_sentence_transformers = (
        harness.AdminDbQueryEndpointTest._restore_sentence_transformers
    )

    def test_exact_projection_reads_only_selected_names_beyond_first_25(self) -> None:
        with self.database.get_cursor() as cursor:
            cursor.executemany(
                "INSERT INTO users(id,encrypted_name,ephemeral_pubkey_name,encrypted_email) VALUES (?,?,?,?)",
                [(i, f"name-{i}", "a" * 64, "UNREQUESTED") for i in range(1, 1001)],
            )
        response = self.client.post(
            "/admin/user-lookup/projection",
            json={
                "user_ids": [991, 71, 409, 999, 601],
                "select": ["name"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            {u["id"] for u in response.json()["users"]}, {991, 71, 409, 999, 601}
        )
        self.assertNotIn("UNREQUESTED", response.text)
        self.assertEqual(
            response.json()["fields"], [{"selector": "name", "type": "text"}]
        )

    def test_definition_ids_disambiguate_duplicate_labels_and_batched_reads(
        self,
    ) -> None:
        with self.database.get_cursor() as c:
            c.execute("INSERT INTO user_types(id,name) VALUES (42,'Scoped')")
            c.executemany(
                "INSERT INTO user_field_definitions(id,field_name,field_type,user_type_id) VALUES (?,?,?,?)",
                [
                    (11, "Country", "text", None),
                    (12, "Country", "text", 42),
                    (13, "Score", "number", None),
                ],
            )
            c.executemany(
                "INSERT INTO users(id) VALUES (?)", [(i,) for i in range(1, 21)]
            )
            c.executemany(
                "INSERT INTO user_field_values(user_id,field_id,value) VALUES (?,?,?)",
                [
                    (i, f, v)
                    for i in range(1, 21)
                    for f, v in [(11, "Nicaragua"), (12, "Costa Rica"), (13, "7")]
                ],
            )
        statements = []
        self.database.get_connection().set_trace_callback(statements.append)
        r = self.client.post(
            "/admin/user-lookup/projection",
            json={
                "user_ids": list(range(1, 21)),
                "select": ["field:11", "field:12", "field:13"],
            },
        )
        self.database.get_connection().set_trace_callback(None)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["users"][0]["values"],
            {
                "field:11": {"value": "Nicaragua"},
                "field:12": {"value": "Costa Rica"},
                "field:13": {"value": "7"},
            },
        )
        reads = [
            s
            for s in statements
            if s.lstrip().upper().startswith("SELECT")
            and any(
                t in s
                for t in ["user_field_values", "user_field_definitions", "FROM users"]
            )
        ]
        self.assertEqual(len(reads), 3)
        self.assertIn({"selector": "field:13", "type": "number"}, r.json()["fields"])

    def test_ordered_pages_use_a_stable_id_window_and_typed_metadata(self) -> None:
        with self.database.get_cursor() as c:
            c.executemany(
                "INSERT INTO users(id,approved,pubkey) VALUES (?,?,?)",
                [(1, 1, None), (5, 0, None), (9, 1, "key"), (30, 1, None)],
            )
        first = self.client.post(
            "/admin/user-lookup/projection",
            json={
                "select": ["name"],
                "metadata": {"approved": True, "pubkey_present": False},
                "limit": 1,
            },
        ).json()
        self.assertEqual([u["id"] for u in first["users"]], [1])
        self.assertEqual(first["eligible"], 2)
        self.assertEqual(first["through_id"], 30)
        with self.database.get_cursor() as c:
            c.execute("INSERT INTO users(id) VALUES (40)")
        second = self.client.post(
            "/admin/user-lookup/projection",
            json={
                "select": ["name"],
                "metadata": {"approved": True, "pubkey_present": False},
                "after_id": first["next_after_id"],
                "through_id": first["through_id"],
                "limit": 1,
            },
        ).json()
        self.assertEqual([u["id"] for u in second["users"]], [30])
        self.assertIsNone(second["next_after_id"])

    def test_rejects_unknown_fields_invalid_predicate_types_and_unbounded_projection(
        self,
    ) -> None:
        with self.database.get_cursor() as c:
            c.execute(
                "INSERT INTO user_field_definitions(id,field_name,field_type) VALUES (41,'Score','number')"
            )
        for body in [
            {"select": ["field:999"]},
            {"select": ["name"], "limit": 101},
            {"select": ["name"], "user_ids": list(range(1, 22))},
            {"select": ["name"], "user_ids": [1, 1]},
            {"select": ["name"], "user_ids": [1], "after_id": 1},
            {"select": ["name"], "metadata": {"approved": "true"}},
            {
                "select": ["name"],
                "where": [{"field": "name", "operator": "gt", "value": 3}],
            },
            {
                "select": ["field:41"],
                "where": [{"field": "field:41", "operator": "equals", "value": "7"}],
            },
            {
                "select": ["name"],
                "where": [{"field": "email", "operator": "equals", "value": "hidden"}],
            },
            {"select": ["name"], "sql": "SELECT * FROM users"},
        ]:
            r = self.client.post("/admin/user-lookup/projection", json=body)
            self.assertIn(r.status_code, [400, 422], r.text)

    def test_public_auth_csrf_and_current_session_fingerprint(self) -> None:
        r = self.client.get("/admin/user-lookup/session")
        self.assertEqual(
            r.json(),
            {
                "pubkey": self.pubkey,
                "session_fingerprint": hashlib.sha256(self.token.encode()).hexdigest(),
            },
        )
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.client.headers.pop("Authorization")
        self.assertEqual(
            self.client.post(
                "/admin/user-lookup/projection", json={"select": ["name"]}
            ).status_code,
            401,
        )
        self.client.cookies.set(self.auth.ADMIN_SESSION_COOKIE_NAME, self.token)
        self.assertEqual(
            self.client.post(
                "/admin/user-lookup/projection", json={"select": ["name"]}
            ).status_code,
            403,
        )
        self.client.cookies.set(self.auth.CSRF_COOKIE_NAME, "test-csrf")
        headers = {
            "Origin": next(iter(self.main._csrf_allowed_origins())),
            "X-CSRF-Token": "test-csrf",
        }
        self.assertEqual(
            self.client.post(
                "/admin/user-lookup/projection",
                json={"select": ["name"]},
                headers=headers,
            ).status_code,
            200,
        )
        self.database.increment_admin_session_nonce(self.pubkey)
        self.assertEqual(self.client.get("/admin/user-lookup/session").status_code, 401)

    def test_predicate_unicode_scalar_boundaries(self) -> None:
        for character in ("a", "😀"):
            for count in (1000, 1001):
                with self.subTest(character=character, count=count):
                    response = self.client.post(
                        "/admin/user-lookup/projection",
                        json={
                            "select": ["name"],
                            "where": [
                                {
                                    "field": "name",
                                    "operator": "equals",
                                    "value": character * count,
                                }
                            ],
                        },
                    )
                    self.assertEqual(
                        response.status_code,
                        200 if count == 1000 else 400,
                        response.text,
                    )
