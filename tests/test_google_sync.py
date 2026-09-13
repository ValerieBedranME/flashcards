from copy import deepcopy
from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import google_sync as sync
import workspace as w


class Sheet:
    def __init__(self):
        self.rows = [deepcopy(sync.HEADERS)]
        self.writes = 0
        self.lose_response = False
        self.receipts = {}
        self.before_write = None

    def read(self, file_id):
        return deepcopy(self.rows)

    def receipt(self, file_id, job_id):
        return deepcopy(self.receipts.get(job_id))

    def write(self, file_id, changes, before=None, job_id=None, previous_job=None):
        if job_id in self.receipts:
            raise w.Problem('Duplicate atomic batch', 503)
        if self.before_write:
            self.before_write(self.rows)
            self.before_write = None
        preimage = deepcopy(self.rows)
        self.writes += 1
        for index, values in changes:
            while len(self.rows) < index:
                self.rows.append([])
            row = self.rows[index-1]
            prior = before[index-1] if before and index <= len(before) else []
            for col, value in enumerate(values):
                old = str(prior[col]) if col < len(prior) else ''
                if before is None or old != value:
                    while len(row) <= col:
                        row.append('')
                    row[col] = value
        if job_id:
            self.receipts[job_id] = {'before': preimage, 'after': deepcopy(self.rows)}
        if self.lose_response:
            self.lose_response = False
            raise w.Problem("Response lost", 503)


class GoogleSyncTests(unittest.TestCase):
    def test_revoked_google_access_has_reconnection_action(self):
        error = HTTPError('https://oauth2.googleapis.com/token', 400, 'Bad Request', {},
                          BytesIO(b'{"error":"invalid_grant"}'))
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(w.Problem) as result:
                sync.http_json('https://oauth2.googleapis.com/token', 'POST',
                               {'grant_type': 'refresh_token'}, form=True)
        self.assertEqual(result.exception.status, 503)
        self.assertIn('Переподключите Google', result.exception.message)

    def setUp(self):
        self.user = {"workspace": w.empty_workspace(), "srs": {}, "google": {"links": {
            "anatomy": {"file_id": "test-file", "base": {}, "pending": True}}}}
        ws = self.user["workspace"]
        deck = w.create_deck(ws, "Alice", {"name": "Лекция", "topic": "Тема"})
        self.card = w.create_card(ws, deck, {"q": "Question", "a": "Answer"})
        self.cid = self.card["id"]
        self.sheet = Sheet()

    def run_sync(self):
        result = sync.synchronize(self.user, "Alice", "anatomy", self.sheet)
        if result.get("job_id"):
            return sync.flush_sync(self.user, "anatomy", self.sheet, result["job_id"])
        return result

    def test_export_is_staged_then_written_and_formula_text_is_literal(self):
        self.card["q"] = '=IMPORTXML("https://example.invalid","//x")'
        result = sync.synchronize(self.user, "Alice", "anatomy", self.sheet)
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertTrue(result["job_id"])
        sync.flush_sync(self.user, "anatomy", self.sheet, result["job_id"])
        self.assertEqual(self.sheet.rows[1][5], self.card["q"])
        self.assertEqual(self.sheet.rows[1][8], "Alice")
        self.assertFalse(self.user["google"]["links"]["anatomy"]["pending"])

    def test_reorder_and_bulk_import_keep_ids_and_progress(self):
        self.run_sync()
        self.user["srs"][self.cid] = {"last": "know", "interval": 8, "due": 12345}
        for i in range(20):
            self.sheet.rows.append(['', '', '', 'Новая тема', 'Новая лекция', f'Q{i}', f'A{i}'])
        self.run_sync()
        ids = set(self.user["workspace"]["cards"])
        self.assertEqual(len(ids), 21)
        self.sheet.rows[1:] = reversed(self.sheet.rows[1:])
        self.run_sync()
        self.assertEqual(set(self.user["workspace"]["cards"]), ids)
        self.assertEqual(self.user["srs"][self.cid]["interval"], 8)

    def test_identical_new_row_added_later_is_a_new_card(self):
        self.run_sync()
        row = ['', '', '', 'Тема', 'Лекция', 'Same Q', 'Same A']
        self.sheet.rows.append(row[:])
        self.run_sync()
        ids = set(self.user['workspace']['cards'])
        self.sheet.rows.append(row[:])
        self.run_sync()
        self.assertEqual(len(set(self.user['workspace']['cards']) - ids), 1)
        self.assertEqual(len(w.active_cards(self.user['workspace'])), 3)

    def test_new_rows_can_extend_an_independent_copy_and_ambiguous_names_fail(self):
        deck = self.user['workspace']['decks'][self.card['deck_id']]
        deck.update(author='Masha', origin={'publication_id': 'test-publication'})
        self.card['author'] = 'Masha'
        self.run_sync()
        self.sheet.rows.append(['', '', '', 'Тема', 'Лекция', 'Added to copy', 'Answer'])
        self.run_sync()
        self.assertEqual(len(self.user['workspace']['decks']), 1)
        self.assertEqual(self.sheet.rows[2][8], 'Masha')
        w.create_deck(self.user['workspace'], 'Alice', {'name': 'Лекция', 'topic': 'Тема'})
        self.sheet.rows.append(['', '', '', 'Тема', 'Лекция', 'Ambiguous', 'Answer'])
        before = deepcopy(self.user)
        with self.assertRaises(w.Problem):
            self.run_sync()
        self.assertEqual(self.user, before)

    def test_new_row_edited_before_metadata_write_does_not_leave_a_duplicate(self):
        self.run_sync()
        self.sheet.rows.append(['', '', '', 'Тема', 'Лекция', 'Before', 'Answer'])
        job = sync.synchronize(self.user, 'Alice', 'anatomy', self.sheet)
        self.assertEqual(len(w.active_cards(self.user['workspace'])), 1)
        self.sheet.rows[2][5] = 'After'
        result = sync.flush_sync(self.user, 'anatomy', self.sheet, job['job_id'])
        self.assertTrue(result['retry'])
        self.run_sync()
        self.assertEqual(len(w.active_cards(self.user['workspace'])), 2)
        self.assertEqual(len(self.sheet.rows), 3)
        self.assertIn('After', [c['q'] for c in w.active_cards(self.user['workspace'])])

    def test_concurrent_edits_keep_both_variants(self):
        self.run_sync()
        card = self.user["workspace"]["cards"][self.cid]
        w.change_card(self.user["workspace"], card, {"q": "App version", "a": "Answer"})
        self.sheet.rows[1][5] = 'Sheet version'
        self.run_sync()
        conflict = next(iter(self.user["workspace"]["conflicts"].values()))
        self.assertEqual(conflict["current"]["q"], 'App version')
        self.assertEqual(conflict["proposed"]["q"], 'Sheet version')
        self.assertEqual(self.sheet.rows[1][5], 'Sheet version')
        self.run_sync()
        self.assertEqual(len(self.user["workspace"]["conflicts"]), 1)

    def test_lost_write_response_does_not_duplicate_imported_rows(self):
        self.run_sync()
        self.sheet.rows.append(['', '', '', 'Тема', 'Лекция', 'New Q', 'New A'])
        result = sync.synchronize(self.user, 'Alice', 'anatomy', self.sheet)
        before_ids = set(self.user["workspace"]["cards"])
        self.assertEqual(len(before_ids), 2)
        # This workspace and job are committed before the flush HTTP request starts.
        self.sheet.lose_response = True
        with self.assertRaises(w.Problem):
            sync.flush_sync(self.user, 'anatomy', self.sheet, result['job_id'])
        retry = self.run_sync()
        self.assertTrue(retry['ok'])
        self.assertEqual(set(self.user["workspace"]["cards"]), before_ids)
        self.assertEqual(len(self.sheet.rows), 3)

    def test_edit_between_prepare_and_write_cancels_stale_write(self):
        self.run_sync()
        card = self.user["workspace"]["cards"][self.cid]
        w.change_card(self.user["workspace"], card, {"q": 'App change', "a": 'Answer'})
        prepared = sync.synchronize(self.user, 'Alice', 'anatomy', self.sheet)
        self.sheet.rows[1][5] = 'Concurrent sheet change'
        result = sync.flush_sync(self.user, 'anatomy', self.sheet, prepared['job_id'])
        self.assertTrue(result['retry'])
        self.assertEqual(self.sheet.rows[1][5], 'Concurrent sheet change')
        self.run_sync()
        self.assertEqual(len(self.user["workspace"]["conflicts"]), 1)

    def test_confirmed_row_deletion_goes_to_trash_and_restore_keeps_id(self):
        self.run_sync();self.sheet.rows.pop()
        self.run_sync()
        self.assertTrue(self.user["workspace"]["cards"][self.cid]['deleted'])
        w.change_card(self.user["workspace"], self.user["workspace"]["cards"][self.cid], deleted=False)
        self.run_sync()
        self.assertEqual(self.sheet.rows[1][0], self.cid)
        self.assertEqual(len(self.user["workspace"]["cards"]), 1)

    def test_bad_header_duplicate_id_and_invalid_row_leave_workspace_unchanged(self):
        self.run_sync()
        valid = deepcopy(self.sheet.rows)
        for damaged in ([['Broken']], valid + [valid[1]], valid + [['', '', '', 'Тема', 'Лекция', 'No answer']]):
            before = deepcopy(self.user)
            self.sheet.rows = damaged
            with self.assertRaises(w.Problem):
                self.run_sync()
            self.assertEqual(self.user, before)

    def test_same_cell_edit_after_last_read_is_recovered_for_choice(self):
        self.run_sync()
        card = self.user['workspace']['cards'][self.cid]
        w.change_card(self.user['workspace'], card, {'q': 'App version', 'a': 'Answer'})
        self.sheet.before_write = lambda rows: rows[1].__setitem__(5, 'Last-moment Sheet version')
        self.run_sync()
        conflict = next(iter(self.user['workspace']['conflicts'].values()))
        self.assertEqual(conflict['proposed']['q'], 'Last-moment Sheet version')
        self.assertEqual(conflict['current']['q'], 'App version')
        self.assertTrue(conflict['recovered'])
        self.run_sync()
        self.assertEqual(len(self.user['workspace']['conflicts']), 1)
        self.assertEqual(conflict['proposed']['q'], 'Last-moment Sheet version')
        # The same baseline used by the resolution endpoint exports the chosen text.
        w.change_card(self.user['workspace'], self.user['workspace']['cards'][self.cid], conflict['proposed'])
        self.user['google']['links']['anatomy']['base'][self.cid] = conflict['remote_baseline']
        del self.user['workspace']['conflicts'][conflict['id']]
        self.run_sync()
        self.assertEqual(self.sheet.rows[1][5], 'Last-moment Sheet version')
        self.assertFalse(self.user['workspace']['conflicts'])

    def test_receipt_prevents_rewrite_after_lost_response_and_later_sheet_edit(self):
        self.run_sync()
        w.change_card(self.user['workspace'], self.user['workspace']['cards'][self.cid], {'q': 'App edit', 'a': 'Answer'})
        prepared = sync.synchronize(self.user, 'Alice', 'anatomy', self.sheet)
        self.sheet.before_write = lambda rows: rows[1].__setitem__(5, 'Racing edit')
        self.sheet.lose_response = True
        with self.assertRaises(w.Problem):
            sync.flush_sync(self.user, 'anatomy', self.sheet, prepared['job_id'])
        writes = self.sheet.writes
        self.sheet.rows[1][5] = 'Still later edit'
        self.run_sync()
        self.assertEqual(self.sheet.writes, writes)
        self.assertEqual(self.sheet.rows[1][5], 'Still later edit')
        self.assertEqual(next(iter(self.user['workspace']['conflicts'].values()))['proposed']['q'], 'Racing edit')

    def test_new_import_edited_during_id_write_keeps_one_card_and_both_versions(self):
        self.run_sync()
        self.sheet.rows.append(['', '', '', 'Тема', 'Лекция', 'Initial import', 'Answer'])
        self.sheet.before_write = lambda rows: rows[2].__setitem__(5, 'Edited during import')
        self.run_sync()
        self.assertEqual(len(w.active_cards(self.user['workspace'])), 2)
        self.assertEqual(len(self.sheet.rows), 3)
        conflict = next(iter(self.user['workspace']['conflicts'].values()))
        self.assertEqual(conflict['proposed']['q'], 'Edited during import')
        self.assertEqual(conflict['current']['q'], 'Initial import')

    def test_row_move_during_write_stops_sync_and_preserves_complete_snapshots(self):
        self.run_sync()
        ws = self.user['workspace']
        w.create_card(ws, ws['decks'][self.card['deck_id']], {'q': 'Second', 'a': 'Second answer'})
        self.run_sync()
        w.change_card(self.user['workspace'], self.user['workspace']['cards'][self.cid], {'q': 'App edit', 'a': 'Answer'})
        self.sheet.before_write = lambda rows: rows.__setitem__(slice(1, None), list(reversed(rows[1:])))
        with self.assertRaises(w.Problem):
            self.run_sync()
        recovery = self.user['google']['links']['anatomy']['recovery']
        self.assertEqual(recovery['before'][2][5], 'Question')
        self.assertEqual(recovery['before'][1][5], 'Second')
        writes = self.sheet.writes
        with self.assertRaises(w.Problem):
            self.run_sync()
        self.assertEqual(self.sheet.writes, writes)

    def test_recovery_creates_fresh_link_without_losing_progress_or_old_file(self):
        self.run_sync()
        ws = self.user['workspace']
        second = w.create_card(ws, ws['decks'][self.card['deck_id']], {'q': 'Second', 'a': 'Second answer'})
        self.run_sync()
        self.user['srs'][self.cid] = {'last': 'know', 'interval': 8, 'due': 12345}
        link = self.user['google']['links']['anatomy']
        link['url'] = 'https://docs.google.com/spreadsheets/d/test-file/edit'
        card = self.user['workspace']['cards'][self.cid]
        w.change_card(self.user['workspace'], card, {'q': 'App edit', 'a': 'Answer'})
        def race(rows):
            rows[1][5] = 'Sheet edit during move'
            rows[1:] = list(reversed(rows[1:]))
        self.sheet.before_write = race
        with self.assertRaises(w.Problem):
            self.run_sync()
        old_rows = deepcopy(self.sheet.rows)
        job_id = link['recovery_job']
        sync.prepare_recovery(self.user, 'Alice', 'anatomy', job_id)
        self.assertEqual(self.sheet.rows, old_rows)
        self.assertNotIn('anatomy', self.user['google']['links'])
        self.assertEqual(self.user['google']['archives'][job_id]['file_id'], 'test-file')
        conflict = next(iter(self.user['workspace']['conflicts'].values()))
        self.assertEqual(conflict['proposed']['q'], 'Sheet edit during move')
        self.assertEqual(conflict['current']['q'], 'App edit')
        self.assertEqual(self.user['srs'][self.cid]['interval'], 8)
        self.assertEqual(self.user['workspace']['cards'][second['id']]['q'], 'Second')
        before_retry = deepcopy(self.user)
        sync.prepare_recovery(self.user, 'Alice', 'anatomy', job_id)
        self.assertEqual(self.user, before_retry)
        self.user['google']['links']['anatomy'] = {'file_id': 'new-file', 'base': {}, 'pending': True}
        self.sheet = Sheet()
        self.run_sync()
        self.assertEqual({row[0] for row in self.sheet.rows[1:]}, set(self.user['workspace']['cards']))

    def test_google_adapter_snapshots_and_cell_writes_share_one_atomic_batch(self):
        calls = []
        api = object.__new__(sync.GoogleSheet)
        def call(path, method='GET', payload=None):
            calls.append((path, method, payload))
            return {'sheets': [{'properties': {'sheetId': 42, 'title': 'Карточки'}}]}
        api.call = call
        api.write('test-file', [(2, ['id', '', '', '', '', '=literal'])], [sync.HEADERS, []], 'sync_test')
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1][0].endswith(':batchUpdate'))
        requests = calls[1][2]['requests']
        copies = [i for i, item in enumerate(requests) if 'duplicateSheet' in item]
        writes = [i for i, item in enumerate(requests) if 'updateCells' in item]
        self.assertLess(copies[0], min(writes))
        self.assertGreater(copies[1], max(writes))
        self.assertEqual(requests[writes[-1]]['updateCells']['rows'][0]['values'][0]['userEnteredValue'], {'stringValue': '=literal'})


if __name__ == '__main__':
    unittest.main()
