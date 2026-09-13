"""Opt-in tests against an explicitly named, isolated PostgreSQL test database."""
import io
import json
import os
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from flask import Flask, g

import app as host
import google_sync
import storage


@unittest.skipUnless(os.environ.get('FLASHCARDS_TEST_DATABASE_URL'), 'Requires isolated PostgreSQL test database')
class PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ['FLASHCARDS_TEST_DATABASE_URL']
        self.schema = 'qa_' + uuid.uuid4().hex
        with psycopg.connect(self.url) as db:
            if not db.execute('SELECT current_database()').fetchone()[0].startswith('flashcards_qa_'):
                raise RuntimeError('Refusing to test outside flashcards_qa_* database')
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.scoped = make_conninfo(self.url, options='-csearch_path=' + self.schema)
        config = dict(host.app.config)
        self.addCleanup(lambda: host.app.config.update(config))
        self.addCleanup(self.remove_test_schema)
        host.app.config.update(DATABASE_URL=self.scoped, SECRET_KEY='postgres-test-only',
                               PUBLIC_BASE_URL=None, TESTING=True, SESSION_COOKIE_SECURE=False)

    def remove_test_schema(self):
        with psycopg.connect(self.url) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def profile(self, name):
        client = host.app.test_client()
        self.assertEqual(client.post('/api/register', json={
            'name': name, 'email': name + '@example.invalid', 'password': 'test-only-password'}).status_code, 200)
        client.post('/api/login', json={'name': name, 'password': 'test-only-password'})
        csrf = client.get('/api/v2/bootstrap').get_json()['csrf']
        return client, {'X-CSRF-Token': csrf}

    def test_two_simultaneous_edits_preserve_conflicting_versions(self):
        client, headers = self.profile('Alice')
        deck = client.post('/api/v2/decks', headers=headers, json={'operation_id': str(uuid.uuid4()), 'name': 'Deck', 'topic': 'Topic'}).get_json()
        card = client.post('/api/v2/cards', headers=headers, json={'operation_id': str(uuid.uuid4()), 'deck_id': deck['id'], 'q': 'Before', 'a': 'Answer'}).get_json()['card']
        clients = []
        for _ in range(2):
            other = host.app.test_client()
            other.post('/api/login', json={'name': 'Alice', 'password': 'test-only-password'})
            clients.append((other, {'X-CSRF-Token': other.get('/api/v2/bootstrap').get_json()['csrf']}))
        barrier, results = threading.Barrier(2), []
        def edit(index):
            other, csrf = clients[index]
            barrier.wait(5)
            results.append(other.patch('/api/v2/cards/' + card['id'], headers=csrf, json={
                'operation_id': str(uuid.uuid4()), 'revision': 1, 'q': 'Version ' + str(index), 'a': 'Answer'}).status_code)
        threads = [threading.Thread(target=edit, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(results), [200, 409])
        conflict = client.get('/api/v2/bootstrap').get_json()['conflicts'][0]
        self.assertEqual({conflict['current']['q'], conflict['proposed']['q']}, {'Version 0', 'Version 1'})

    def test_other_profile_can_commit_while_google_waits(self):
        first, first_headers = self.profile('Alice')
        second, second_headers = self.profile('Masha')
        with psycopg.connect(self.scoped) as db:
            users = db.execute("SELECT data FROM flashcards_documents WHERE name='users'").fetchone()[0]
            token = google_sync.cipher(host.app).encrypt(json.dumps({
                'access_token': 'test-only', 'expires_at': time.time() + 3600}).encode()).decode()
            users['Alice']['google'] = {'token': token, 'links': {'anatomy': {
                'file_id': 'test-file', 'base': {}, 'pending': True, 'initialized': True}}}
            db.execute("UPDATE flashcards_documents SET data=%s WHERE name='users'", (Jsonb(users),))
        completed, results = threading.Event(), []
        def other_profile():
            try:
                results.append(second.post('/api/v2/decks', headers=second_headers,
                    json={'operation_id': str(uuid.uuid4()), 'name': 'Parallel deck', 'topic': 'Topic', 'subject_id': 'latin'}).status_code)
            finally:
                completed.set()
        worker = threading.Thread(target=other_profile)
        def google_response(*args, **kwargs):
            worker.start()
            self.assertTrue(completed.wait(8), 'Google IO held the PostgreSQL advisory lock')
            return io.BytesIO(json.dumps({'values': [google_sync.HEADERS]}).encode())
        try:
            with patch.object(google_sync.urllib.request, 'urlopen', side_effect=google_response):
                response = first.post('/api/v2/google/subjects/anatomy/sync', headers=first_headers, json={})
                self.assertEqual(response.status_code, 200)
        finally:
            if worker.ident:
                worker.join(10)
        self.assertEqual(results, [200])
        self.assertEqual(second.get('/api/v2/bootstrap').get_json()['decks'][0]['name'], 'Parallel deck')

    def test_retry_state_commits_on_503_and_unexpected_500_rolls_back(self):
        test_app = Flask('postgres-transaction-test')
        test_app.config.update(DATABASE_URL=self.scoped, TESTING=False)
        test_app.logger.disabled = True
        storage.init_storage(test_app)
        @test_app.post('/api/write/<kind>')
        def write(kind):
            storage.save_document('probe', '/unused', {'value': kind})
            if kind == 'retry':
                g.commit_storage_on_error = True
                return {'retry': True}, 503
            raise RuntimeError('Deliberate rollback test')
        client = test_app.test_client()
        self.assertEqual(client.post('/api/write/retry').status_code, 503)
        self.assertEqual(client.post('/api/write/crash').status_code, 500)
        with psycopg.connect(self.scoped) as db:
            self.assertEqual(db.execute("SELECT data FROM flashcards_documents WHERE name='probe'").fetchone()[0], {'value': 'retry'})

    def test_terminated_connection_during_write_returns_error_and_can_retry(self):
        test_app = Flask('postgres-disconnection-test')
        test_app.config.update(DATABASE_URL=self.scoped, TESTING=True)
        storage.init_storage(test_app)
        @test_app.post('/api/write/<kind>')
        def write(kind):
            storage.save_document('probe', '/unused', {'value': kind})
            if kind == 'disconnect':
                pid = g.db.execute('SELECT pg_backend_pid()').fetchone()[0]
                with psycopg.connect(self.url, autocommit=True) as controller:
                    self.assertTrue(controller.execute('SELECT pg_terminate_backend(%s)', (pid,)).fetchone()[0])
            return {'ok': True}
        client = test_app.test_client()
        self.assertEqual(client.post('/api/write/before').status_code, 200)
        failed = client.post('/api/write/disconnect')
        self.assertEqual(failed.status_code, 503)
        self.assertIn('Нет связи с базой данных', failed.get_json()['error'])
        self.assertEqual(failed.headers['Cache-Control'], 'no-store')
        with psycopg.connect(self.scoped) as db:
            self.assertEqual(db.execute("SELECT data FROM flashcards_documents WHERE name='probe'").fetchone()[0], {'value':'before'})
        self.assertEqual(client.post('/api/write/retry').status_code, 200)
        with psycopg.connect(self.scoped) as db:
            self.assertEqual(db.execute("SELECT data FROM flashcards_documents WHERE name='probe'").fetchone()[0], {'value':'retry'})
