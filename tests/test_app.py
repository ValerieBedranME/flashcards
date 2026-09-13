import os
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as module


class FlashcardsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for attribute, filename in [('USERS_PATH', 'users.json'), ('RESET_PATH', 'reset.json')]:
            patcher = patch.object(module, attribute, str(Path(self.directory.name) / filename))
            patcher.start()
            self.addCleanup(patcher.stop)
        module.app.config.update(TESTING=True, DATABASE_URL=None, SECRET_KEY='test-only-key', SESSION_COOKIE_SECURE=False)
        self.client = module.app.test_client()

    def register_login(self, client, name='alice'):
        self.assertEqual(client.post('/api/register', json={
            'name': name, 'email': name + '@example.com', 'password': 'test-password'
        }).status_code, 200)
        self.assertEqual(client.post('/api/login', json={
            'name': name, 'password': 'test-password'
        }).status_code, 200)

    def test_progress_and_personal_cards_survive_new_client(self):
        self.register_login(self.client)
        card = self.client.post('/api/cards?user=alice', json={'q': 'Question', 'a': 'Answer'}).get_json()
        cid = card['id']
        response = self.client.post('/api/srs/answer?user=alice', json={'id': cid, 'rating': 'know'})
        self.assertEqual(response.get_json()['interval'], 1)
        self.client.post('/api/logout')
        fresh = module.app.test_client()
        fresh.post('/api/login', json={'name': 'alice', 'password': 'test-password'})
        self.assertIn(cid, [c['id'] for c in fresh.get('/api/cards?user=alice').get_json()])
        self.assertEqual(fresh.get('/api/srs?user=alice').get_json()['stats']['know'], 1)
        self.assertEqual(fresh.get('/api/session').get_json()['name'], 'alice')

    def test_private_data_requires_authenticated_owner(self):
        for endpoint in ('cards', 'topics', 'srs'):
            self.assertEqual(self.client.get('/api/' + endpoint + '?user=alice').status_code, 401)
        self.register_login(self.client)
        self.assertEqual(self.client.get('/api/cards?user=bob').status_code, 403)
        self.assertEqual(self.client.post('/api/srs/answer?user=bob', json={'id': 1, 'rating': 'know'}).status_code, 403)
        self.client.post('/api/logout')
        self.assertEqual(self.client.get('/api/cards?user=alice').status_code, 401)

    def test_progress_counts_last_rating_by_current_topic_and_excludes_deleted(self):
        self.register_login(self.client)
        ids = []
        for topic in ('Topic A', 'Topic A', 'Topic B', 'Not started'):
            card = self.client.post('/api/cards?user=alice', json={
                'q': 'Question', 'a': 'Answer', 'topic': topic}).get_json()
            ids.append(card['id'])
        for cid, rating in zip(ids, ('know', 'dontknow', 'unsure')):
            self.client.post('/api/srs/answer?user=alice', json={'id': cid, 'rating': rating})
        self.client.post('/api/srs/answer?user=alice', json={'id': ids[0], 'rating': 'unsure'})
        result = self.client.get('/api/srs?user=alice').get_json()
        self.assertEqual(result['stats'], {'know': 0, 'dontknow': 1, 'unsure': 2})
        self.assertEqual(result['topics']['Topic A'], {'know': 0, 'dontknow': 1, 'unsure': 1})
        self.assertEqual(sum(result['topics']['Not started'].values()), 0)
        self.assertIn(ids[3], result['new'])
        self.client.put('/api/cards/' + str(ids[0]) + '?user=alice', json={
            'q': 'Question', 'a': 'Answer', 'topic': 'Topic B'})
        self.client.delete('/api/cards/' + str(ids[1]) + '?user=alice')
        after_two_days = module.time.time() + 2 * module.DAY
        with patch.object(module.time, 'time', return_value=after_two_days):
            result = self.client.get('/api/srs?user=alice').get_json()
        self.assertEqual(result['stats'], {'know': 0, 'dontknow': 0, 'unsure': 2})
        self.assertEqual(result['topics']['Topic B']['unsure'], 2)
        for kind in ('know', 'dontknow', 'unsure'):
            self.assertEqual(sum(t[kind] for t in result['topics'].values()), result['stats'][kind])
        self.assertEqual(len(result['due']), 2)

    def test_duplicate_and_incorrect_password(self):
        self.register_login(self.client)
        self.assertEqual(self.client.post('/api/register', json={
            'name': 'alice', 'email': 'alice@example.com', 'password': 'another-password'
        }).status_code, 409)
        other = module.app.test_client()
        self.assertEqual(other.post('/api/login', json={'name': 'alice', 'password': 'wrong'}).status_code, 401)
        self.assertEqual(other.get('/api/session').status_code, 401)

    def test_vercel_fails_closed_without_database(self):
        with patch.dict(os.environ, {'VERCEL': '1'}):
            response = self.client.post('/api/register', json={
                'name': 'alice', 'email': 'alice@example.com', 'password': 'test-password'
            })
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Path(module.USERS_PATH).exists())

    def test_disabled_email_and_private_file_routes(self):
        with patch.object(module, 'email_configured', return_value=False):
            self.assertEqual(self.client.post('/api/forgot', json={'email': 'alice@example.com'}).status_code, 503)
        with self.client.get('/') as response:
            self.assertEqual(response.status_code, 200)
        for path in ('users.json', 'reset.json', 'app.py', '.env'):
            self.assertEqual(self.client.get('/' + path).status_code, 404)

    def request_code(self, sent=True):
        with patch.object(module, 'email_configured', return_value=True), patch.object(module, 'send_email', return_value=sent) as sender:
            response = self.client.post('/api/forgot', json={'email': 'alice@example.com'})
        code = re.search(r'\b\d{6}\b', sender.call_args.args[2]).group() if sender.called else None
        return response, code

    def change_password(self, code):
        return self.client.post('/api/reset', json={'email': 'alice@example.com', 'code': code, 'password': 'new-password'})

    def test_login_by_email_and_canonical_name(self):
        self.register_login(self.client)
        self.client.post('/api/logout')
        for identity in ('ALICE@example.com', 'ALICE'):
            result = self.client.post('/api/login', json={'name': identity, 'password': 'test-password'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()['name'], 'alice')
            self.assertEqual(self.client.get('/api/cards?user=alice').status_code, 200)

    def test_reset_is_one_time_and_invalidates_existing_sessions(self):
        self.register_login(self.client)
        old_session = module.app.test_client()
        old_session.post('/api/login', json={'name': 'alice', 'password': 'test-password'})
        own_card = self.client.post('/api/cards?user=alice', json={'q': 'Question', 'a': 'Answer'}).get_json()['id']
        self.client.post('/api/srs/answer?user=alice', json={'id': own_card, 'rating': 'know'})
        response, code = self.request_code()
        self.assertEqual(response.status_code, 200)
        saved = json.loads(Path(module.RESET_PATH).read_text())['alice@example.com']
        self.assertNotIn('code', saved)
        self.assertNotEqual(saved['code_hash'], code)
        self.assertEqual(self.change_password(code).status_code, 200)
        self.assertEqual(self.change_password(code).status_code, 400)
        self.assertEqual(old_session.get('/api/session').status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'name': 'alice', 'password': 'test-password'}).status_code, 401)
        self.assertEqual(self.client.post('/api/login', json={'name': 'alice@example.com', 'password': 'new-password'}).status_code, 200)
        self.assertEqual(self.client.get('/api/srs?user=alice').get_json()['stats']['know'], 1)

    def test_wrong_attempt_limit_and_resend_cooldown(self):
        self.register_login(self.client)
        response, code = self.request_code()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.request_code()[0].status_code, 429)
        wrong = '999999' if code != '999999' else '000000'
        for _ in range(5):
            self.assertEqual(self.change_password(wrong).status_code, 400)
        self.assertEqual(self.change_password(code).status_code, 400)
        saved = json.loads(Path(module.RESET_PATH).read_text())['alice@example.com']
        self.assertEqual(saved['attempts'], 5)

    def test_expired_code_and_unknown_email(self):
        self.register_login(self.client)
        with patch.object(module.time, 'time', return_value=10000):
            response, code = self.request_code()
        with patch.object(module.time, 'time', return_value=10901):
            self.assertEqual(self.change_password(code).status_code, 400)
        with patch.object(module, 'email_configured', return_value=True), patch.object(module, 'send_email') as sender:
            result = self.client.post('/api/forgot', json={'email': 'nobody@example.com'})
            self.assertEqual(result.status_code, 200)
            sender.assert_not_called()

    def test_mail_failure_does_not_create_reset_code(self):
        self.register_login(self.client)
        self.assertEqual(self.request_code(sent=False)[0].status_code, 503)
        self.assertFalse(Path(module.RESET_PATH).exists())

    def test_email_cannot_be_reused_for_new_account(self):
        self.register_login(self.client)
        result = self.client.post('/api/register', json={'name': 'another', 'email': 'ALICE@example.com', 'password': 'password'})
        self.assertEqual(result.status_code, 409)


if __name__ == '__main__':
    unittest.main()
