"""Authenticated retained-review projection boundary; isolated synthetic database."""
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))


class ConversationProjectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'SQLITE_PATH': str(Path(self.tmp.name) / 'test.db'), 'SECRET_KEY': 'synthetic-test-secret'})
        self.env.start()
        import database, auth, conversation_lookup
        self.database = importlib.reload(database)
        self.auth = importlib.reload(auth)
        self.module = importlib.reload(conversation_lookup)
        self.database.init_schema()
        self.pubkey = 'a' * 64
        admin_id = self.database.add_admin(self.pubkey)
        self.token = self.auth.create_admin_session_token(admin_id, self.pubkey)
        app = FastAPI()
        app.include_router(self.module.router)
        self.client = TestClient(app)
        self.client.cookies.set(self.auth.ADMIN_SESSION_COOKIE_NAME, self.token)
        self.client.cookies.set(self.auth.CSRF_COOKIE_NAME, 'synthetic-csrf')
        self.client.headers['X-CSRF-Token'] = 'synthetic-csrf'
        with self.database.get_cursor() as c:
            c.execute('INSERT INTO users(id) VALUES (1)')
            c.execute("INSERT INTO session_logs(log_id,source,subject_user_id,transcript_ciphertext,transcript_ephemeral_pubkey,encrypted_to_pubkey,turn_count,created_at) VALUES (?,?,?,?,?,?,?,?)", ('log-one', 'user', 1, 'ciphertext-only', 'b'*64, self.pubkey, 2, '2026-10-20 10:00:00'))

    def tearDown(self):
        self.client.close()
        if self.database._connection is not None:
            self.database._connection.close()
            self.database._connection = None
        self.env.stop()
        self.tmp.cleanup()

    def test_admin_selects_retained_ciphertext_by_user_and_time(self):
        response = self.client.post('/admin/conversation-lookup/projection', json={'user_ids': [1], 'start': '2026-10-20T00:00:00Z', 'end': '2026-10-21T00:00:00Z'})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['eligible_logs'], 1)
        self.assertEqual(body['logs'][0]['conversation_id'], 'log-one')
        self.assertEqual(body['logs'][0]['transcript']['ciphertext'], 'ciphertext-only')
        self.assertEqual(body['time_basis'], 'message_ts')
        self.assertNotIn('title', body['logs'][0])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        session = self.client.get('/admin/conversation-lookup/session').json()
        self.assertEqual(session['session_fingerprint'], hashlib.sha256(self.token.encode()).hexdigest())

    def test_time_selection_keeps_old_logs_as_candidates_for_recent_message_activity(self):
        body = self.client.post('/admin/conversation-lookup/projection', json={'start':'2026-10-22T00:00:00Z','end':'2026-10-23T00:00:00Z'}).json()
        self.assertEqual(body['eligible_logs'], 1)
        self.assertEqual(body['time_basis'], 'message_ts')
        self.assertEqual(body['logs'][0]['conversation_id'], 'log-one')

    def test_feedback_selection_and_stable_window_do_not_reconstruct_deleted_logs(self):
        with self.database.get_cursor() as c:
            c.execute("INSERT INTO session_log_feedback(log_id,message_id,rating,comment_ciphertext,comment_ephemeral_pubkey) VALUES (?,?,?,?,?)", ('log-one','message-1','down','encrypted-comment','c'*64))
            for i in range(2, 5):
                c.execute("INSERT INTO session_logs(log_id,source,subject_user_id,created_at) VALUES (?,?,?,?)", (f'log-{i}', 'admin_test' if i == 4 else 'user', 1, '2026-10-20 10:00:00'))
        body = self.client.post('/admin/conversation-lookup/projection', json={'feedback':'down'}).json()
        self.assertEqual(body['eligible_logs'], 1)
        self.assertEqual(body['logs'][0]['feedback'][0]['message_id'], 'message-1')
        self.assertEqual(body['logs'][0]['feedback'][0]['comment_ciphertext'], 'encrypted-comment')
        first = self.client.post('/admin/conversation-lookup/projection', json={}).json()
        self.assertEqual(first['eligible_logs'],3)
        second = self.client.post('/admin/conversation-lookup/projection', json={'after_id':first['next_after_id'],'through_id':first['through_id']}).json()
        self.assertEqual(second['logs'][0]['conversation_id'],'log-2')
        import session_logs
        self.assertTrue(session_logs.delete_session_log('log-one'))
        deleted = self.client.post('/admin/conversation-lookup/projection', json={'log_id':'log-one','through_id':first['through_id']}).json()
        self.assertEqual(deleted['logs'],[])
        self.assertEqual(deleted['eligible_logs'],2)

    def test_requires_current_admin_cookie_and_csrf_and_rejects_bearer_impersonation(self):
        self.client.headers['Authorization'] = 'Bearer ' + self.token
        self.assertEqual(self.client.post('/admin/conversation-lookup/projection',json={}).status_code,403)
        del self.client.headers['Authorization']
        del self.client.headers['X-CSRF-Token']
        self.assertEqual(self.client.post('/admin/conversation-lookup/projection',json={}).status_code,403)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/admin/conversation-lookup/session').status_code,401)
        self.assertEqual(self.client.post('/admin/conversation-lookup/projection',json={}).status_code,401)

    def test_projection_rejects_unsupported_fields_inverted_intervals_and_unbounded_selectors(self):
        for body in [{'select':['email']},{'user_ids':[1,1]},{'after_id':-1},{'through_id':0,'after_id':1},{'start':'2026-10-20'}, {'start':'2026-10-22T00:00:00Z','end':'2026-10-20T00:00:00Z'}, {'feedback':'bad'}, {'source':'sage_messages'}]:
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/admin/conversation-lookup/projection',json=body).status_code,422)

    def test_projects_real_production_record_envelope_and_retains_message_timestamps(self):
        from coincurve import PrivateKey
        import encryption
        import session_logs
        private_key = bytes([1]) * 32  # Synthetic test-only Admin key.
        pubkey = PrivateKey(private_key).public_key.format(compressed=True)[1:].hex()
        with self.database.get_cursor() as c:
            c.execute('DELETE FROM admins')
        admin_id = self.database.add_admin(pubkey)
        token = self.auth.create_admin_session_token(admin_id, pubkey)
        self.client.cookies.set(self.auth.ADMIN_SESSION_COOKIE_NAME, token)
        messages = [{'message_id':'sage:message.42','role':'user','content':'Production envelope fixture','ts':'2026-10-20T10:00:00.000000+00:00'}]
        saved = session_logs.record_user_conversation(sage_session_id='synthetic-session',subject_user_id=1,messages=messages,through_sequence_id=1)
        response = self.client.post('/admin/conversation-lookup/projection',json={'log_id':saved['log_id']})
        self.assertEqual(response.status_code,200,response.text)
        log = response.json()['logs'][0]
        plaintext = encryption.nip04_decrypt(log['transcript']['ciphertext'],log['transcript']['ephemeral_pubkey'],private_key)
        self.assertEqual(json.loads(plaintext),{'turns':messages})
        self.assertNotIn('Production envelope fixture',response.text)
