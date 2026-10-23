"""The usage ownership boundary returns provenance, never profile content."""

import unittest

import test_admin_db_query_endpoint as harness


class UsageSubjectsTest(unittest.TestCase):
    def setUp(self) -> None:
        harness.AdminDbQueryEndpointTest.setUp(self)
        self.main.app.dependency_overrides.clear()
        self.pubkey = 'a' * 64
        admin_id = self.database.add_admin(self.pubkey)
        self.actor = {'id': admin_id, 'type': 'admin', 'approved': True, 'pubkey': self.pubkey}
        self.headers = {'X-Internal-Agent-Token': 'test-internal-token'}

    tearDown = harness.AdminDbQueryEndpointTest.tearDown
    _restore_env = staticmethod(harness.AdminDbQueryEndpointTest._restore_env)
    _restore_sentence_transformers = harness.AdminDbQueryEndpointTest._restore_sentence_transformers

    def request(self, user_ids, *, actor=None, headers=None):
        return self.client.post(
            '/internal/agent/admin/usage-users',
            json={'actor': self.actor if actor is None else actor, 'user_ids': user_ids},
            headers=self.headers if headers is None else headers,
        )

    def test_classifies_public_key_provenance_and_omits_missing_users(self):
        import impersonation

        ordinary = self.database.create_user()
        synthetic = self.database.create_user()
        pubkey = impersonation.derive_test_user_pubkey(self.pubkey, None)
        with self.database.get_cursor() as cursor:
            cursor.execute('UPDATE users SET email=?, name=? WHERE id=?',
                           ('test-user@enclave.test', 'PRIVATE NAME', ordinary))
            cursor.execute('UPDATE users SET pubkey=? WHERE id=?', (pubkey, synthetic))
        response = self.request([ordinary, synthetic, 99999])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'users': [
            {'id': ordinary, 'is_test': False}, {'id': synthetic, 'is_test': True},
        ]})
        self.assertNotIn('PRIVATE NAME', response.text)
        self.assertNotIn(pubkey, response.text)
        self.assertNotIn('enclave.test', response.text)

    def test_requires_internal_token_and_a_current_approved_admin(self):
        self.assertEqual(self.request([1], headers={}).status_code, 403)
        for change in ({'type': 'user'}, {'approved': False}, {'id': 999}, {'pubkey': 'b' * 64}):
            self.assertEqual(self.request([1], actor={**self.actor, **change}).status_code, 403)
        with self.database.get_cursor() as cursor:
            cursor.execute('DELETE FROM admins WHERE id=?', (self.actor['id'],))
        self.assertEqual(self.request([1]).status_code, 403)

    def test_rejects_unbounded_duplicate_and_non_integer_ids(self):
        for ids in ([0], [-1], [True], ['1'], [1, 1], list(range(1, 202))):
            with self.subTest(ids=ids[:3]):
                self.assertEqual(self.request(ids).status_code, 422)

    def test_empty_batch_validates_admin_even_without_retained_owners(self):
        self.assertEqual(self.request([]).json(), {"users": []})
        self.assertEqual(self.request([], actor={**self.actor, "type": "user"}).status_code, 403)

    def test_at_most_requested_ids_and_live_deletion(self):
        ids = [self.database.create_user() for _ in range(3)]
        self.assertEqual(self.request([ids[1]]).json(), {'users': [{'id': ids[1], 'is_test': False}]})
        with self.database.get_cursor() as cursor:
            cursor.execute('DELETE FROM users WHERE id=?', (ids[1],))
        self.assertEqual(self.request([ids[1]]).json(), {'users': []})


if __name__ == '__main__':
    unittest.main()
