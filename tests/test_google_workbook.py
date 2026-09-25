from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import google_sync as sync
import workspace as w
import app as host
from test_google_sync import Sheet
import test_workspace_api as api_tests


class Workbook:
    title = 'Карточки'

    def __init__(self):
        self.files = {}
        self.tab_ids = {}
        self.identities = {}
        self.fail_seed = False

    def create(self, name, identity):
        fid = self.identities.setdefault(identity, 'book-' + str(len(self.identities)))
        return {'spreadsheetId': fid, 'spreadsheetUrl': 'https://docs.google.com/spreadsheets/d/' + fid + '/edit'}

    def seed_tab(self, fid, title, rows):
        key = (fid, title)
        self.tab_ids.setdefault(key, sync.snapshot_id('subject/' + title, 'live'))
        if key not in self.files:
            sheet = self.files[key] = Sheet()
            sheet.rows = deepcopy(rows)
        if self.fail_seed:
            self.fail_seed = False
            raise w.Problem('Lost response', 503)
        return self.tab_ids[key]

    def tabs(self, fid):
        return [{'sheetId': self.tab_ids[key], 'title': key[1], 'hidden': False}
                for key in self.files if key[0] == fid]

    def prepare_tab(self, fid, tab_id):
        key = next(key for key, value in self.tab_ids.items() if value == tab_id)
        if self.files[key].rows == []:
            self.files[key].rows = [deepcopy(sync.HEADERS)]

    def read(self, fid, title=None):
        return self.files[(fid, title or self.title)].read(fid)

    def write(self, fid, *args):
        return self.files[(fid, self.title)].write(fid, *args)

    def receipt(self, fid, job):
        return self.files[(fid, self.title)].receipt(fid, job)


# Reuse helpers, without inheriting and rerunning the existing API test suite.
class WorkbookTests(__import__('unittest').TestCase):
    setUp = api_tests.WorkspaceApiTests.setUp
    boot = api_tests.WorkspaceApiTests.boot
    call = api_tests.WorkspaceApiTests.call
    card = api_tests.WorkspaceApiTests.card

    def connect(self, name, links=None):
        path = Path(host.USERS_PATH)
        users = json.loads(path.read_text())
        users[name]['google'] = {'token': 'test-only', 'links': links or {}}
        path.write_text(json.dumps(users))

    def prepare(self, client):
        for _ in range(6):
            result = self.call(client, '/google/workbook')
            if result.get('ok'):
                return self.boot(client)
        self.fail('Setup did not finish')

    def update(self, client, subject):
        result = self.call(client, '/google/subjects/' + subject + '/sync')
        if result.get('job_id'):
            self.call(client, '/google/subjects/' + subject + '/flush', {'job_id': result['job_id']})

    def test_one_file_default_tabs_two_owners_and_identical_rows_do_not_cross_subjects(self):
        self.connect('Alice'); self.connect('Masha')
        fake = Workbook()
        with patch.object(sync, 'GoogleSheet', return_value=fake):
            a, b = self.prepare(self.a), self.prepare(self.b)
            ids_a = {link['file_id'] for link in a['google']['links'].values()}
            ids_b = {link['file_id'] for link in b['google']['links'].values()}
            self.assertEqual(len(ids_a), 1)
            self.assertFalse(ids_a & ids_b)
            self.assertEqual(len(a['google']['links']), 2)
            fid = ids_a.pop()
            for subject in w.SUBJECTS:
                fake.files[(fid, subject['name'])].rows.append(['', '', '', 'Same', '', 'Same Q', 'Same A'])
                self.update(self.a, subject['id'])
            cards = self.boot(self.a)['cards']
            self.assertEqual(len({card['id'] for card in cards}), 2)
            self.assertEqual({card['subject_id'] for card in cards}, w.SUBJECT_IDS)
            self.assertEqual(self.boot(self.b)['cards'], [])
            self.prepare(self.a)
            self.assertEqual(len(fake.identities), 2)

    def test_new_and_removed_tabs_change_subjects_after_discovery(self):
        self.connect('Alice')
        fake = Workbook()
        with patch.object(sync, 'GoogleSheet', return_value=fake):
            before = self.prepare(self.a)
            fid = before['google']['workbook']['url'].split('/d/')[1].split('/')[0]
            key = (fid, 'Физика')
            fake.files[key] = Sheet()
            fake.files[key].rows = []
            fake.tab_ids[key] = 901
            discovered = self.call(self.a, '/google/discover')
            sid = next(s['id'] for s in discovered['subjects'] if s['name'] == 'Физика')
            self.assertTrue(sid.startswith('sheet_'))
            self.update(self.a, sid)
            self.assertEqual(fake.files[key].rows, [sync.HEADERS])
            fake.files[key].rows.append(['', '', '', 'Механика', '', 'Сила?', 'Ньютон'])
            self.update(self.a, sid)
            self.assertEqual(self.boot(self.a)['cards'][0]['subject_id'], sid)
            renamed = (fid, 'Новая физика')
            fake.files[renamed] = fake.files.pop(key)
            fake.tab_ids[renamed] = fake.tab_ids.pop(key)
            self.call(self.a, '/google/discover')
            self.assertIn({'id': sid, 'name': 'Новая физика'}, self.boot(self.a)['subjects'])
            self.assertEqual(len(self.boot(self.a)['cards']), 1)
            key = renamed
            del fake.files[key], fake.tab_ids[key]
            self.call(self.a, '/google/discover')
            after = self.boot(self.a)
            self.assertNotIn(sid, [s['id'] for s in after['subjects']])
            self.assertEqual(after['cards'], [])

    def test_migration_keeps_pending_sheet_edits_authorship_progress_and_old_files(self):
        _, card = self.card(self.a)
        self.call(self.a, '/review', {'id': card['id'], 'rating': 'know'})
        before = self.boot(self.a)
        users = json.loads(Path(host.USERS_PATH).read_text())
        stored = users['Alice']['workspace']['cards'][card['id']]
        row = sync.serialize(users['Alice']['workspace'], stored)
        baseline = {card['id']: w.content(stored)}
        self.connect('Alice', {'anatomy': {'file_id': 'old', 'url': 'https://example.invalid/old',
            'base': baseline, 'initialized': True, 'single_topic_layout': True}})
        fake = Workbook()
        fake.seed_tab('old', 'Карточки', [sync.HEADERS, row])
        fake.files[('old', 'Карточки')].rows[1][5] = 'Unsynced Google edit'
        old_rows = deepcopy(fake.files[('old', 'Карточки')].rows)
        with patch.object(sync, 'GoogleSheet', return_value=fake):
            self.prepare(self.a)
            self.update(self.a, 'anatomy')
        after = self.boot(self.a)
        self.assertEqual(after['srs'], before['srs'])
        self.assertEqual(after['cards'][0]['id'], card['id'])
        self.assertEqual(after['cards'][0]['author'], card['author'])
        self.assertEqual(after['cards'][0]['q'], 'Unsynced Google edit')
        self.assertEqual(fake.files[('old', 'Карточки')].rows, old_rows)
        self.assertEqual(after['google']['archived_links'], [{'url': 'https://example.invalid/old'}])

    def test_partial_setup_and_lost_response_keep_later_target_edit(self):
        self.connect('Alice')
        fake = Workbook()
        with patch.object(sync, 'GoogleSheet', return_value=fake):
            self.call(self.a, '/google/workbook')
            fake.fail_seed = True
            self.call(self.a, '/google/workbook', expected=503)
            self.assertEqual(self.boot(self.a)['google']['links'], {})
            first = next(iter(fake.files.values()))
            first.rows.append(['', '', '', 'After timeout', '', 'Keep me', 'Answer'])
            self.prepare(self.a)
            self.update(self.a, w.SUBJECTS[0]['id'])
        self.assertEqual(len(fake.identities), 1)
        self.assertEqual(self.boot(self.a)['cards'][0]['q'], 'Keep me')

    def test_pending_job_blocks_migration_before_creating_file(self):
        self.connect('Alice', {'anatomy': {'sync_job': {'id': 'pending'}}})
        fake = Workbook()
        with patch.object(sync, 'GoogleSheet', return_value=fake):
            self.call(self.a, '/google/workbook', expected=409)
        self.assertFalse(fake.identities)

    def test_atomic_seed_retry_never_overwrites_existing_tab(self):
        sheet = object.__new__(sync.GoogleSheet)
        calls = []
        metadata = [{'properties': {'sheetId': 0, 'title': 'Sheet1'}}]
        def call(path, method='GET', payload=None):
            calls.append((path, method, payload))
            if method == 'GET':
                return {'sheets': deepcopy(metadata)}
            added = payload['requests'][0]['addSheet']['properties']
            metadata.append({'properties': deepcopy(added)})
            raise w.Problem('Response lost after commit', 503)
        sheet.call = call
        with self.assertRaises(w.Problem):
            sheet.seed_tab('file', 'Латынь', [sync.HEADERS, ['', '', '', 'Topic', '', '=literal', 'Answer']])
        sheet.seed_tab('file', 'Латынь', [sync.HEADERS])
        writes = [c for c in calls if c[1] == 'POST']
        self.assertEqual(len(writes), 1)
        rows = writes[0][2]['requests'][1]['updateCells']['rows']
        self.assertEqual(rows[1]['values'][5]['userEnteredValue'], {'stringValue': '=literal'})
