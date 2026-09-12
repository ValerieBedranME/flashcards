from copy import deepcopy
import unittest

import google_sync as sync
import workspace as w


class Sheet:
    def __init__(self):
        self.rows = [deepcopy(sync.HEADERS)]
        self.writes = 0
        self.lose_response = False

    def read(self, file_id):
        return deepcopy(self.rows)

    def write(self, file_id, changes, before=None):
        self.writes += 1
        for index, values in changes:
            while len(self.rows) < index:
                self.rows.append([])
            self.rows[index-1] = deepcopy(values)
        if self.lose_response:
            self.lose_response = False
            raise w.Problem("Response lost", 503)


class GoogleSyncTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
