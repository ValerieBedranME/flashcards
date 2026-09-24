from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import io
import threading
import time
from unittest.mock import patch
from unittest.mock import Mock
from urllib.parse import urlsplit, parse_qs

import app as host
import google_sync
import workspace
from storage import external_io
from test_google_sync import Sheet


class WorkspaceApiTests(unittest.TestCase):
    def test_create_with_one_name_and_study_one_topic_only(self):
        first=self.call(self.a,'/decks',{'name':'Bones','subject_id':'anatomy'})
        second=self.call(self.a,'/decks',{'name':'Bones','subject_id':'anatomy'})
        card=self.call(self.a,'/cards',{'deck_id':first['id'],'q':'Q','a':'A'})['card']
        self.call(self.a,'/cards',{'deck_id':second['id'],'q':'Other','a':'Other'})
        result=self.a.get('/api/v2/study?mode=all&subject_id=anatomy&deck_id='+first['id'])
        self.assertEqual(result.get_json()['ids'],[card['id']])
        self.assertEqual(self.b.get('/api/v2/study?mode=all&deck_id='+first['id']).status_code,404)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for attr, file in (("USERS_PATH", "users.json"), ("RESET_PATH", "reset.json")):
            p = patch.object(host, attr, str(Path(self.temp.name) / file))
            p.start(); self.addCleanup(p.stop)
        host.app.config.update(TESTING=True, DATABASE_URL=None, SECRET_KEY="test-only", SESSION_COOKIE_SECURE=False,
                               GOOGLE_CLIENT_ID=None, GOOGLE_CLIENT_SECRET=None)
        self.a, self.b = host.app.test_client(), host.app.test_client()
        for client, name in ((self.a, "Alice"), (self.b, "Masha")):
            self.assertEqual(client.post('/api/register', json={"name": name, "email": name+'@example.com', "password": "test-password"}).status_code, 200)
            client.post('/api/login', json={"name": name, "password": "test-password"})

    def boot(self, client):
        response = client.get('/api/v2/bootstrap')
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def call(self, client, path, body=None, method="POST", expected=200):
        if path.startswith('/conflicts/') and body and 'version' not in body:
            conflict = next(c for c in self.boot(client)['conflicts'] if c['id'] == path.split('/')[-1])
            body = dict(body, version=conflict['version'])
        response = client.open('/api/v2'+path, method=method,
                               headers={"X-CSRF-Token": self.boot(client)["csrf"]},
                               json={"operation_id": str(uuid.uuid4()), **(body or {})})
        self.assertEqual(response.status_code, expected, response.get_json())
        return response.get_json()

    def card(self, client, subject="anatomy"):
        deck = self.call(client, '/decks', {"name": "Лекция", "topic": "Тема", "subject_id": subject})
        card = self.call(client, '/cards', {"deck_id": deck["id"], "q": "Вопрос", "a": "Ответ"})["card"]
        return deck, card

    def test_new_accounts_empty_existing_accounts_migrate_once(self):
        self.assertEqual(self.boot(self.a)["cards"], [])
        path = Path(host.USERS_PATH); users = json.loads(path.read_text())
        users["Alice"].pop("workspace")
        users["Alice"]["srs"] = {"1": {"last": "know", "interval": 8, "due": 12345}}
        path.write_text(json.dumps(users))
        first, second = self.boot(self.a), self.boot(self.a)
        self.assertEqual(len(first["cards"]), len(host.load_cards()))
        self.assertEqual(first["cards"], second["cards"])
        self.assertEqual(first["srs"]["1"]["interval"], 8)
        self.assertNotIn("legacy_backup_v1", first)
        self.assertEqual(self.boot(self.b)["cards"], [])

    def test_publication_copy_author_progress_and_original_independence(self):
        deck, source = self.card(self.b)
        self.call(self.b, '/review', {"id": source["id"], "rating": "know"})
        self.assertEqual(self.a.get('/api/v2/library').get_json(), [])
        deck = self.boot(self.b)["decks"][0]
        pub = self.call(self.b, '/decks/'+deck["id"]+'/publish', {"revision": deck["revision"]})["id"]
        copied = self.call(self.a, '/library/'+pub+'/copy')["deck"]
        boot = self.boot(self.a); copy = boot["cards"][0]
        self.assertNotEqual(copy["id"], source["id"])
        self.assertEqual(copied["author"], "Masha")
        self.assertEqual(boot["srs"], {})
        changed = self.call(self.a, '/cards/'+copy["id"], {"q": "Моя правка", "a": "Мой ответ", "revision": copy["revision"]}, "PATCH")["card"]
        self.assertEqual(self.boot(self.b)["cards"][0]["q"], "Вопрос")
        self.assertEqual(self.a.get('/api/v2/library').get_json()[0]["cards"][0]["q"], "Вопрос")
        self.call(self.b, '/cards/'+source["id"], {"q": "Новый оригинал", "a": "Ответ", "revision": source["revision"]}, "PATCH")
        new_deck = self.boot(self.b)["decks"][0]
        new_pub = self.call(self.b, '/decks/'+deck["id"]+'/publish', {"revision": new_deck["revision"]})["id"]
        self.assertNotEqual(pub, new_pub)
        self.call(self.b, '/decks/'+deck["id"], {"revision": new_deck["revision"]}, "DELETE")
        self.assertEqual(self.boot(self.a)["cards"][0]["q"], changed["q"])
        self.call(self.a, '/library/'+pub+'/copy')
        self.assertEqual(len(self.boot(self.a)["cards"]), 1)
        self.call(self.a, '/library/'+new_pub+'/copy')
        self.assertEqual(len(self.boot(self.a)["cards"]), 2)

    def test_access_checks_csrf_and_subject_restriction(self):
        deck, card = self.card(self.a)
        self.call(self.b, '/cards/'+card["id"], {"q": "Hack", "a": "Hack", "revision": 1}, "PATCH", 404)
        self.call(self.b, '/cards', {"deck_id": deck["id"], "q": "Q", "a": "A"}, expected=404)
        self.call(self.b, '/topics', {"name": "New", "subject_id": "unauthorized"}, expected=400)
        self.assertEqual(self.a.post('/api/v2/decks', json={"name": "No token"}).status_code, 403)
        self.assertEqual(self.a.post('/api/register', json={}, headers={"Origin": "https://foreign.example"}).status_code, 403)
        self.assertEqual(self.a.get('/api/v2/bootstrap?user=Masha').status_code, 403)
        self.a.post('/api/logout')
        self.assertEqual(self.a.get('/api/v2/bootstrap').status_code, 401)

    def test_archive_restore_preserves_rating_and_duplicate_review_is_once(self):
        deck, card = self.card(self.a, "microbiology")
        operation = str(uuid.uuid4())
        first = self.call(self.a, '/review', {"id": card["id"], "rating": "know", "operation_id": operation})
        again = self.call(self.a, '/review', {"id": card["id"], "rating": "know", "operation_id": operation})
        self.assertEqual(first, again)
        removed = self.call(self.a, '/cards/'+card["id"], {"revision": 1}, "DELETE")["card"]
        self.assertEqual(self.boot(self.a)["cards"], [])
        self.call(self.a, '/cards/'+card["id"]+'/restore', {"revision": removed["revision"]})
        self.assertEqual(self.boot(self.a)["srs"][card["id"]], first)
        self.assertEqual(len(self.boot(self.a)["cards"]), 1)

    def test_stale_edit_preserves_both_texts_and_can_choose(self):
        deck, card = self.card(self.a)
        self.call(self.a, '/cards/'+card["id"], {"q": "Первый", "a": "Ответ", "revision": 1}, "PATCH")
        conflict = self.call(self.a, '/cards/'+card["id"], {"q": "Второй", "a": "Ответ", "revision": 1}, "PATCH", 409)["conflict"]
        self.assertEqual(conflict["current"]["q"], "Первый")
        self.assertEqual(conflict["proposed"]["q"], "Второй")
        self.assertEqual(len(self.boot(self.a)["conflicts"]), 1)
        again = self.call(self.a, '/cards/'+card["id"], {"q": "Второй", "a": "Ответ", "revision": 1}, "PATCH", 409)["conflict"]
        self.assertEqual(again["id"], conflict["id"])
        self.assertEqual(len(self.boot(self.a)["conflicts"]), 1)
        self.call(self.a, '/conflicts/'+conflict["id"], {"choice": "proposed"})
        self.assertEqual(self.boot(self.a)["cards"][0]["q"], "Второй")
        self.assertEqual(self.boot(self.a)["conflicts"], [])

    def test_separate_subjects_empty_state_and_unrated_cards(self):
        _, a = self.card(self.a, "anatomy")
        _, l = self.card(self.a, "latin")
        self.call(self.a, '/review', {"id": a["id"], "rating": "dontknow"})
        all_due = self.a.get('/api/v2/study').get_json()
        self.assertEqual(all_due["ids"], [a["id"]])
        self.assertEqual(self.a.get('/api/v2/study?subject_id=latin').get_json()["cards"], [])
        self.assertEqual(self.a.get('/api/v2/study?subject_id=latin&mode=all').get_json()["ids"], [l["id"]])
        self.assertEqual(self.a.get('/api/v2/study?subject_id=microbiology').get_json()["total"], 0)

    def test_edit_after_concurrent_deletion_can_restore_the_proposed_version(self):
        _, card = self.card(self.a)
        self.call(self.a, '/cards/'+card['id'], {'revision': 1}, 'DELETE')
        conflict = self.call(self.a, '/cards/'+card['id'], {'q': 'Preserve this edit', 'a': 'Answer', 'revision': 1}, 'PATCH', 409)['conflict']
        self.assertTrue(conflict['current']['deleted'])
        self.assertFalse(conflict['proposed']['deleted'])
        self.call(self.a, '/conflicts/'+conflict['id'], {'choice': 'proposed'})
        self.assertEqual(self.boot(self.a)['cards'][0]['q'], 'Preserve this edit')

    def test_conflict_choice_cannot_accept_a_version_the_user_has_not_seen(self):
        _, card = self.card(self.a)
        self.call(self.a, '/cards/'+card['id'], {'q': 'First', 'a': 'Answer', 'revision': 1}, 'PATCH')
        conflict = self.call(self.a, '/cards/'+card['id'], {'q': 'Second', 'a': 'Answer', 'revision': 1}, 'PATCH', 409)['conflict']
        self.call(self.a, '/cards/'+card['id'], {'q': 'Third', 'a': 'Answer', 'revision': 2}, 'PATCH')
        updated = self.call(self.a, '/conflicts/'+conflict['id'], {'choice': 'proposed', 'version': conflict['version']}, expected=409)['conflict']
        self.assertEqual(updated['current']['q'], 'Third')
        self.assertEqual(updated['proposed']['q'], 'Second')
        self.assertEqual(self.boot(self.a)['cards'][0]['q'], 'Third')
        self.call(self.a, '/conflicts/'+conflict['id'], {'choice': 'proposed', 'version': updated['version']})
        self.assertEqual(self.boot(self.a)['cards'][0]['q'], 'Second')

    def test_google_not_configured_is_explicit_and_secrets_not_in_bootstrap(self):
        self.call(self.a, '/google/connect', expected=503)
        result = self.boot(self.a)
        self.assertFalse(result["google"]["connected"])
        self.assertNotIn('password_hash', result)
        self.assertNotIn('salt', result)

    def test_google_oauth_is_bound_to_profile_one_time_and_encrypted(self):
        host.app.config.update(GOOGLE_CLIENT_ID='test-client', GOOGLE_CLIENT_SECRET='test-secret')
        path = Path(host.USERS_PATH)
        users = json.loads(path.read_text())
        users['Alice']['google'] = {'links': {}, 'reconnect_required': True}
        path.write_text(json.dumps(users))
        url = self.call(self.a, '/google/connect')['url']
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertEqual(params['scope'], [google_sync.SCOPE])
        callback = '/api/v2/google/callback?state='+params['state'][0]+'&code=test-code'
        token = {'access_token': 'test-access-token', 'refresh_token': 'test-refresh-token',
                 'expires_in': 3600, 'scope': google_sync.SCOPE}
        with patch.object(google_sync, 'http_json', return_value=token) as exchange:
            self.assertIn('error', self.b.get(callback).location)
            exchange.assert_not_called()
            self.assertIn('connected', self.a.get(callback).location)
            self.assertIn('error', self.a.get(callback).location)
            exchange.assert_called_once()
        boot = self.boot(self.a)
        self.assertTrue(boot['google']['connected'])
        self.assertFalse(boot['google']['reconnect_required'])
        self.assertNotIn('token', json.dumps(boot))
        saved = Path(host.USERS_PATH).read_text()
        self.assertNotIn('test-access-token', saved)
        self.assertNotIn('test-refresh-token', saved)

    def test_google_reconnect_button_state_tracks_access_failure(self):
        path = Path(host.USERS_PATH)
        users = json.loads(path.read_text())
        token = google_sync.cipher(host.app).encrypt(json.dumps({
            'access_token': 'expired', 'refresh_token': 'test-refresh-token', 'expires_at': 0
        }).encode()).decode()
        users['Alice']['google'] = {'token': token, 'links': {}}
        users['Masha']['google'] = {'token': token, 'links': {}}
        path.write_text(json.dumps(users))
        self.assertFalse(self.boot(self.a)['google']['reconnect_required'])
        with patch.object(google_sync, 'http_json', side_effect=workspace.Problem(
                'Доступ Google истёк. Переподключите Google.', 503, reconnect_required=True)):
            self.call(self.a, '/google/workbook', expected=503)
        self.assertTrue(self.boot(self.a)['google']['reconnect_required'])
        self.assertFalse(self.boot(self.b)['google']['reconnect_required'])
        with patch.object(google_sync, 'http_json', side_effect=workspace.Problem(
                'Google временно недоступен', 503, reconnect_required=False)):
            self.call(self.b, '/google/workbook', expected=503)
        self.assertFalse(self.boot(self.b)['google']['reconnect_required'])

    def test_connected_copy_waits_for_own_table_then_exports_original_author(self):
        deck, original = self.card(self.b)
        deck = self.boot(self.b)['decks'][0]
        publication = self.call(self.b, '/decks/'+deck['id']+'/publish', {'revision': deck['revision']})['id']
        path = Path(host.USERS_PATH)
        users = json.loads(path.read_text())
        users['Alice']['google'] = {'token': 'test-only', 'links': {}}
        path.write_text(json.dumps(users))
        copied = self.call(self.a, '/library/'+publication+'/copy')
        self.assertTrue(copied['pending_sync'])
        self.assertEqual(self.boot(self.a)['google']['pending_subjects'], ['anatomy'])
        sheet = Sheet()
        sheet.create = Mock(return_value={'spreadsheetId': 'test-file', 'spreadsheetUrl': 'https://docs.google.com/spreadsheets/d/test-file/edit'})
        sheet.initialize = Mock()
        sheet.seed_tab = Mock(return_value=42)
        with patch.object(google_sync, 'GoogleSheet', return_value=sheet):
            for _ in range(5):
                self.call(self.a, '/google/workbook')
            prepared = self.call(self.a, '/google/subjects/anatomy/sync')
            self.assertEqual(len(sheet.rows), 1)
            self.call(self.a, '/google/subjects/anatomy/flush', {'job_id': prepared['job_id']})
            self.call(self.a, '/google/subjects/anatomy')
        sheet.create.assert_called_once()
        self.assertEqual(sheet.rows[1][8], 'Masha')
        self.assertNotEqual(sheet.rows[1][0], original['id'])
        self.assertFalse(self.boot(self.a)['google']['links']['anatomy']['pending'])
        self.assertEqual(self.boot(self.a)['srs'], {})
        blocked = self.a.put('/api/cards/'+sheet.rows[1][0]+'?user=Alice', json={'q': 'Bypass', 'a': 'Bypass'})
        self.assertEqual(blocked.status_code, 409)

    def test_waiting_for_google_does_not_block_or_overwrite_another_profile(self):
        self.card(self.a)
        path = Path(host.USERS_PATH)
        users = json.loads(path.read_text())
        token = google_sync.cipher(host.app).encrypt(json.dumps({'access_token': 'test-only', 'expires_at': time.time()+3600}).encode()).decode()
        users['Alice']['google'] = {'token': token, 'links': {'anatomy': {
            'file_id': 'test-file', 'base': {}, 'pending': True, 'initialized': True, 'single_topic_layout': True, 'image_layout': True}}}
        path.write_text(json.dumps(users))
        completed, errors = threading.Event(), []
        def other_profile():
            try:
                self.card(self.b, 'latin')
            except Exception as error:
                errors.append(error)
            finally:
                completed.set()
        worker = threading.Thread(target=other_profile)
        def response(*args, **kwargs):
            worker.start()
            self.assertTrue(completed.wait(2), 'Google wait held the shared storage lock')
            return io.BytesIO(json.dumps({'values': [google_sync.HEADERS]}).encode())
        try:
            with patch.object(google_sync.urllib.request, 'urlopen', side_effect=response), \
                    patch.object(google_sync.GoogleSheet, 'read_images', return_value={}), \
                    patch.object(google_sync.GoogleSheet, 'version', return_value='1'):
                self.call(self.a, '/google/subjects/anatomy/sync')
        finally:
            if worker.ident:
                worker.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(self.boot(self.b)['cards'][0]['subject_id'], 'latin')
        self.assertEqual(len(self.boot(self.a)['cards']), 1)

    def test_creation_lease_blocks_duplicate_request_and_retry_reuses_file(self):
        self.card(self.a)
        path = Path(host.USERS_PATH)
        users = json.loads(path.read_text())
        users['Alice']['google'] = {'token': 'test-only', 'links': {}}
        path.write_text(json.dumps(users))
        second = host.app.test_client()
        with second.session_transaction() as session:
            session['user'], session['auth_version'] = 'Alice', users['Alice']['salt']
        csrf = self.boot(second)['csrf']
        sheet = Sheet()
        def create(name, identity):
            with external_io():
                duplicate = second.post('/api/v2/google/subjects/anatomy', json={}, headers={'X-CSRF-Token': csrf})
                self.assertEqual(duplicate.status_code, 409)
            return {'spreadsheetId': 'created-once', 'spreadsheetUrl': 'https://docs.google.com/spreadsheets/d/created-once/edit'}
        sheet.create = Mock(side_effect=create)
        sheet.seed_tab = Mock(side_effect=[workspace.Problem('Temporary Google error', 503), 42])
        with patch.object(google_sync, 'GoogleSheet', return_value=sheet):
            self.call(self.a, '/google/workbook')
            self.call(self.a, '/google/workbook', expected=503)
            self.assertTrue(self.boot(self.a)['google']['setting_up'])
            self.call(self.a, '/google/workbook')
        sheet.create.assert_called_once()

    def test_sheet_recovery_is_private_retryable_and_keeps_old_file_reference(self):
        deck, card = self.card(self.a)
        self.call(self.a, '/cards', {'deck_id': deck['id'], 'q': 'Second', 'a': 'Second answer'})
        path = Path(host.USERS_PATH)
        users = json.loads(path.read_text())
        users['Alice']['google'] = {'token': 'test-only', 'links': {'anatomy': {
            'file_id': 'test-file', 'url': 'https://docs.google.com/spreadsheets/d/test-file/edit',
            'base': {}, 'pending': True, 'initialized': True}}}
        path.write_text(json.dumps(users))
        sheet = Sheet()
        with patch.object(google_sync, 'GoogleSheet', return_value=sheet):
            prepared = self.call(self.a, '/google/subjects/anatomy/sync')
            self.call(self.a, '/google/subjects/anatomy/flush', {'job_id': prepared['job_id']})
            self.call(self.a, '/cards/'+card['id'], {'revision': 1, 'q': 'App edit', 'a': card['a']}, 'PATCH')
            prepared = self.call(self.a, '/google/subjects/anatomy/sync')
            def move(rows):
                rows[1][5] = 'Sheet edit'
                rows[1:] = list(reversed(rows[1:]))
            sheet.before_write = move
            self.call(self.a, '/google/subjects/anatomy/flush', {'job_id': prepared['job_id']}, expected=409)
        boot = self.boot(self.a)
        job = boot['google']['links']['anatomy']['recovery_job']
        self.assertEqual(boot['google']['links']['anatomy']['file_id'], 'test-file')
        self.assertNotIn('before', json.dumps(boot['google']))
        self.call(self.b, '/google/subjects/anatomy/recover', {'job_id': job}, expected=409)
        self.call(self.a, '/google/subjects/anatomy/recover', {'job_id': job})
        self.call(self.a, '/google/subjects/anatomy/recover', {'job_id': job})
        restored = self.boot(self.a)
        self.assertEqual(len(restored['cards']), 2)
        self.assertEqual(len(restored['conflicts']), 1)
        self.assertEqual(restored['google']['pending_subjects'], ['anatomy'])
        self.assertEqual(len(restored['google']['archived_links']), 1)
        self.assertEqual(restored['google']['archived_links'][0]['url'], 'https://docs.google.com/spreadsheets/d/test-file/edit')


if __name__ == '__main__':
    unittest.main()
