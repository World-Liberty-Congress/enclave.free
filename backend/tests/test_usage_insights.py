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

    def test_provisioned_identity_stays_excluded_after_type_migration(self):
        import impersonation

        self.main.app.dependency_overrides[self.auth.require_admin] = lambda: self.actor
        original_type = self.database.create_user_type('Original persona')
        target_type = self.database.create_user_type('Target persona')
        provision = self.client.post(f'/admin/test-users/provision?user_type_id={original_type}')
        self.assertEqual(provision.status_code, 200, provision.text)
        user_id = provision.json()['user_id']
        pubkey = self.database.get_user(user_id)['pubkey']
        migrated = self.client.post(f'/admin/users/{user_id}/migrate-type',
                                    json={'target_user_type_id': target_type})
        self.assertEqual(migrated.status_code, 200, migrated.text)
        self.database.delete_user_type(original_type)
        self.assertEqual(self.database.get_user(user_id)['pubkey'], pubkey)
        self.assertEqual(self.request([user_id]).json(),
                         {'users': [{'id': user_id, 'is_test': True}]})
        # Analytics provenance does not grant impersonation of another persona.
        with self.assertRaises(impersonation.ImpersonationUnavailable):
            impersonation.issue_session_token(user_id=user_id, issued_by_pubkey=self.pubkey)

    def test_upgrade_backfills_migrated_identity_after_original_type_was_deleted(self):
        import impersonation

        original_type = self.database.create_user_type('Deleted original persona')
        target_type = self.database.create_user_type('Current persona')
        pubkey = impersonation.derive_test_user_pubkey(self.pubkey, original_type)
        user_id = self.database.create_user(pubkey=pubkey, user_type_id=original_type)
        self.database.update_user_type_id(user_id, target_type)
        self.database.delete_user_type(original_type)
        ordinary = self.database.create_user(user_type_id=target_type)
        with self.database.get_cursor() as cursor:
            cursor.execute('UPDATE users SET email=? WHERE id=?',
                           ('test-user+type1@enclave.test', ordinary))
        # Simulate upgrading an old schema, with no review logs or audit history.
        with self.database.get_cursor() as cursor:
            cursor.execute('DROP TABLE IF EXISTS admin_test_provenance')
            cursor.execute('SELECT * FROM users ORDER BY id')
            before = [dict(row) for row in cursor.fetchall()]
        self.database.init_schema()
        self.database.init_schema()
        with self.database.get_cursor() as cursor:
            cursor.execute('SELECT * FROM users ORDER BY id')
            self.assertEqual([dict(row) for row in cursor.fetchall()], before)
        self.assertEqual(self.request([user_id, ordinary]).json(), {'users': [
            {'id': user_id, 'is_test': True}, {'id': ordinary, 'is_test': False},
        ]})

    def test_provenance_is_bound_to_identity_and_removed_with_user(self):
        from coincurve import PrivateKey
        import impersonation
        import test_user_provenance

        user_id = self.database.create_user(
            pubkey=impersonation.derive_test_user_pubkey(self.pubkey, None))
        test_user_provenance.record_provisioning(user_id, self.pubkey, None)
        self.database.set_user_pubkey(user_id, PrivateKey().public_key.format()[1:].hex())
        self.assertEqual(self.request([user_id]).json(),
                         {'users': [{'id': user_id, 'is_test': False}]})
        with self.database.get_cursor() as cursor:
            cursor.execute('DELETE FROM users WHERE id=?', (user_id,))
            cursor.execute('SELECT COUNT(*) FROM admin_test_provenance WHERE user_id=?', (user_id,))
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_ordinary_identity_cannot_be_registered_as_test_provenance(self):
        import test_user_provenance

        ordinary = self.database.create_user()
        with self.assertRaises(ValueError):
            test_user_provenance.record_provisioning(ordinary, self.pubkey, None)


if __name__ == '__main__':
    unittest.main()
