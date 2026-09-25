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


class CompactSheet(Sheet):
    compact = True

    def write(self, file_id, changes, before=None, job_id=None, previous_job=None):
        preimage = deepcopy(self.rows)
        for row, values in changes:
            if values[9] != 'в корзине':
                while len(self.rows) < row:
                    self.rows.append([])
                self.rows[row-1] = deepcopy(values)
        for row, values in sorted(changes, reverse=True):
            if values[9] == 'в корзине':
                del self.rows[row-1]
        self.receipts[job_id] = {'before': preimage, 'after': deepcopy(self.rows)}


class GoogleSyncTests(unittest.TestCase):
    def test_new_blank_tab_gets_headers_without_replacing_existing_rows(self):
        sheet = object.__new__(sync.GoogleSheet)
        sheet.title = 'Физика'
        calls = []
        sheet.call = lambda path, method='GET', payload=None: (calls.append((path, method, payload)) or {})
        sheet.prepare_tab('file', 42)
        self.assertEqual(calls[1][1], 'POST')
        self.assertEqual(calls[1][2]['data'][0]['values'], [[sync.HEADERS[0]]])
        self.assertEqual(calls[2][2]['requests'][0]['repeatCell']['range']['sheetId'], 42)

    def test_existing_tab_headers_are_not_overwritten(self):
        sheet = object.__new__(sync.GoogleSheet)
        sheet.title = 'Физика'
        calls = []
        sheet.call = lambda path, method='GET', payload=None: (calls.append(path) or {'values': [sync.HEADERS]})
        sheet.prepare_tab('file', 42)
        self.assertEqual(len(calls), 1)

    def test_compact_sheet_maps_questions_without_removed_columns(self):
        api = object.__new__(sync.GoogleSheet)
        api.title = 'Анатомия'
        api.call = lambda *args: {'values': [sync.COMPACT_HEADERS + sync.IMAGE_HEADERS,
                                            ['', '', '', 'Кости', 'Вопрос', 'Ответ', '', '', '1']]}
        rows = api.read('test-file')
        self.assertTrue(api.compact)
        self.assertEqual(rows[0], sync.HEADERS)
        self.assertEqual(rows[1][3:7], ['Кости', 'Кости', 'Вопрос', 'Ответ'])
        self.assertEqual(rows[1][9], 'активна')

    def test_compact_atomic_write_deletes_card_row(self):
        calls = []
        api = object.__new__(sync.GoogleSheet)
        api.title = 'Анатомия'
        api.compact = True
        def call(path, method='GET', payload=None):
            calls.append((path, method, payload))
            return {'sheets': [{'properties': {'sheetId': 42, 'title': 'Анатомия'}}]}
        api.call = call
        removed = ['card_1', 'deck_1', 'topic_1', 'Кости', 'Кости',
                   'Вопрос', 'Ответ', '', 'Валерия', 'в корзине', '2']
        api.write('test-file', [(2, removed)], [sync.HEADERS, removed], 'compact-delete')
        requests = calls[-1][2]['requests']
        deletion = next(item['deleteDimension'] for item in requests if 'deleteDimension' in item)
        self.assertEqual(deletion['range'],
                         {'sheetId': 42, 'dimension': 'ROWS', 'startIndex': 1, 'endIndex': 2})
        self.assertFalse(any('updateCells' in item for item in requests))

    def test_compact_atomic_write_uses_shifted_question_column(self):
        calls = []
        api = object.__new__(sync.GoogleSheet)
        api.title = 'Анатомия'
        api.compact = True
        def call(path, method='GET', payload=None):
            calls.append((path, method, payload))
            return {'sheets': [{'properties': {'sheetId': 42, 'title': 'Анатомия'}}]}
        api.call = call
        before = ['card_1', 'deck_1', 'topic_1', 'Кости', 'Кости',
                  'Старый вопрос', 'Ответ', '', 'Валерия', 'активна', '1']
        after = before.copy()
        after[5] = 'Новый вопрос'
        after[10] = '2'
        api.write('test-file', [(2, after)], [sync.HEADERS, before], 'compact-edit')
        positions = {(item['updateCells']['start']['rowIndex'],
                      item['updateCells']['start']['columnIndex'])
                     for item in calls[-1][2]['requests'] if 'updateCells' in item}
        self.assertEqual(positions, {(1, 4), (1, 8)})

    def test_compact_row_deletion_keeps_card_in_app_trash(self):
        self.sheet = CompactSheet()
        self.run_sync()
        self.assertEqual(len(self.sheet.rows), 2)
        card = self.user['workspace']['cards'][self.cid]
        w.change_card(self.user['workspace'], card, deleted=True)
        self.run_sync()
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertTrue(self.user['workspace']['cards'][self.cid]['deleted'])
        self.assertFalse(self.user['google']['links']['anatomy']['pending'])

    def test_image_columns_are_added_without_touching_card_text(self):
        sheet = object.__new__(sync.GoogleSheet)
        sheet.title = "Анатомия"
        calls = []
        def call(path, method="GET", payload=None):
            calls.append((path, method, payload))
            if "values/" in path:
                return {"values": []}
            if path.endswith("?fields=sheets(properties(sheetId,title))"):
                return {"sheets": [{"properties": {"sheetId": 17, "title": "Анатомия"}}]}
            return {}
        sheet.call = call
        sheet.image_layout("file")
        batch = calls[-1][2]["requests"]
        self.assertEqual(batch[0]["updateCells"]["start"],
                         {"sheetId": 17, "rowIndex": 0, "columnIndex": 11})
        self.assertEqual([v["userEnteredValue"]["stringValue"]
                          for v in batch[0]["updateCells"]["rows"][0]["values"]],
                         sync.IMAGE_HEADERS)
        self.assertTrue(all(item["startIndex"] >= 11 for item in
                            [req["updateDimensionProperties"]["range"] for req in batch
                             if "updateDimensionProperties" in req]))

    def test_topic_layout_targets_live_sheet_not_first_snapshot(self):
        sheet=object.__new__(sync.GoogleSheet)
        calls=[]
        def call(path,method='GET',body=None):
            calls.append((path,method,body))
            return {'sheets':[{'properties':{'sheetId':8,'title':'snapshot'}},
                              {'properties':{'sheetId':0,'title':'Карточки'}}]}
        sheet.call=call
        sheet.topic_layout('file')
        change=calls[-1][2]['requests'][0]['updateDimensionProperties']
        self.assertEqual(change['range'],{'sheetId':0,'dimension':'COLUMNS','startIndex':4,'endIndex':5})
        self.assertEqual(change['fields'],'hiddenByUser')

    def test_one_topic_column_import_and_existing_copy_match(self):
        ws=w.empty_workspace()
        deck=w.create_deck(ws,'Original',{'name':'Bones','topic':'Old parent'})
        rows=[deepcopy(sync.HEADERS),['','','','Bones','','Question','Answer']]
        remote,_=sync.parse_rows(ws,'Recipient','anatomy','file',rows)
        card=next(iter(remote.values()))
        self.assertEqual(card['deck_id'],deck['id'])
        self.assertEqual(len(ws['decks']),1)
        stored=ws['cards'][card['id']]
        self.assertEqual(stored['author'],'Original')
        self.assertEqual(sync.serialize(ws,stored)[3],'Bones')
        deck['name']='Renamed'
        self.assertEqual(sync.serialize(ws,stored)[3],'Renamed')
        w.create_deck(ws,'Another',{'name':'Bones','topic':'Other parent'})
        deck['name']='Bones'
        with self.assertRaises(w.Problem):
            sync.parse_rows(ws,'Recipient','anatomy','file',rows)

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

    def test_native_sheet_images_attach_by_row_and_can_be_removed(self):
        image_id = "image_" + "a" * 64
        self.run_sync()
        self.sheet.read_images = lambda file_id, rows: {(2, 12): b"image bytes"}
        with patch("card_media.put", return_value=image_id):
            prepared = sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        if prepared.get("job_id"):
            sync.flush_sync(self.user, "anatomy", self.sheet, prepared["job_id"])
        self.assertEqual(self.user["workspace"]["cards"][self.cid]["q_image"], image_id)
        self.assertEqual(self.user["google"]["links"]["anatomy"]["sheet_images"][self.cid]["q_image"], image_id)
        self.sheet.read_images = lambda file_id, rows: {}
        result = sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        if result.get("job_id"):
            sync.flush_sync(self.user, "anatomy", self.sheet, result["job_id"])
        self.assertEqual(self.user["workspace"]["cards"][self.cid].get("q_image"), "")
        self.assertNotIn(self.cid, self.user["google"]["links"]["anatomy"]["sheet_images"])

    def test_app_image_replacement_is_not_reverted_by_unchanged_sheet_image(self):
        original = "image_" + "a" * 64
        replacement = "image_" + "c" * 64
        self.run_sync()
        self.sheet.read_images = lambda file_id, rows: {(2, 12): b"original"}
        with patch("card_media.put", return_value=original):
            result = sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        if result.get("job_id"):
            sync.flush_sync(self.user, "anatomy", self.sheet, result["job_id"])
        card = self.user["workspace"]["cards"][self.cid]
        w.change_card(self.user["workspace"], card, {"q_image": replacement})
        with patch("card_media.put", return_value=original):
            result = sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        if result.get("job_id"):
            sync.flush_sync(self.user, "anatomy", self.sheet, result["job_id"])
        with patch("card_media.put", return_value=original):
            sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        self.assertEqual(self.user["workspace"]["cards"][self.cid]["q_image"], replacement)

    def test_sheet_image_removal_conflicts_with_app_replacement(self):
        original = "image_" + "a" * 64
        replacement = "image_" + "c" * 64
        self.run_sync()
        self.sheet.read_images = lambda file_id, rows: {(2, 12): b"original"}
        with patch("card_media.put", return_value=original):
            result = sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        if result.get("job_id"):
            sync.flush_sync(self.user, "anatomy", self.sheet, result["job_id"])
        card = self.user["workspace"]["cards"][self.cid]
        w.change_card(self.user["workspace"], card, {"q_image": replacement})
        self.sheet.read_images = lambda file_id, rows: {}
        result = sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        conflicts = [item for item in self.user["workspace"]["conflicts"].values()
                     if item["card_id"] == self.cid and item["source"] == "google"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["current"]["q_image"], replacement)
        self.assertEqual(conflicts[0]["proposed"]["q_image"], "")
        self.assertEqual(self.user["workspace"]["cards"][self.cid]["q_image"], replacement)
        self.assertTrue(result["pending_sync"])

    def test_unchanged_google_version_reuses_image_index(self):
        image_id = "image_" + "d" * 64
        self.run_sync()
        self.sheet.version = lambda file_id: "123"
        calls = []
        def read_images(file_id, rows):
            calls.append(1)
            return {(2, 13): b"image bytes"}
        self.sheet.read_images = read_images
        with patch("card_media.put", return_value=image_id):
            result = sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
            if result.get("job_id"):
                sync.flush_sync(self.user, "anatomy", self.sheet, result["job_id"])
            sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.user["workspace"]["cards"][self.cid]["a_image"], image_id)

    def test_native_image_only_question_imports_with_stable_card_id(self):
        image_id = "image_" + "b" * 64
        self.sheet.rows.append(["", "", "", "Лекция", "", "", "Answer"])
        self.sheet.read_images = lambda file_id, rows: {(2, 12): b"image bytes"}
        with patch("card_media.put", return_value=image_id):
            prepared = sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        self.assertIn("job_id", prepared)
        sync.flush_sync(self.user, "anatomy", self.sheet, prepared["job_id"])
        imported = [card for card in self.user["workspace"]["cards"].values() if card["id"] != self.cid]
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]["q"], "")
        self.assertEqual(imported[0]["q_image"], image_id)
        with patch("card_media.put", return_value=image_id):
            sync.synchronize(self.user, "Alice", "anatomy", self.sheet, object())
        self.assertEqual(len(self.user["workspace"]["cards"]), 2)

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

    def test_import_survives_database_object_key_reordering(self):
        self.run_sync()
        self.sheet.rows.extend([
            ['', '', '', 'New topic', '', 'First new question', 'Answer'],
            ['', '', '', 'New topic', '', 'Second new question', 'Answer'],
        ])
        prepared = sync.synchronize(self.user, 'Alice', 'anatomy', self.sheet)
        # JSONB does not preserve object insertion order between requests.
        ws = self.user['workspace']
        ws['cards'] = dict(reversed(list(ws['cards'].items())))
        result = sync.flush_sync(self.user, 'anatomy', self.sheet, prepared['job_id'])
        self.assertFalse(result.get('retry'))
        self.assertEqual(len(ws['cards']), 3)
        self.assertTrue(all(row[0] for row in self.sheet.rows[1:]))
        self.assertFalse(any(c.get('import_pending') for c in ws['cards'].values()))
        before = sync.subject_hash(ws, 'anatomy')
        ws['cards'][self.cid]['q'] = 'A real simultaneous edit'
        self.assertNotEqual(sync.subject_hash(ws, 'anatomy'), before)

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

    def test_old_card_without_baseline_follows_confirmed_sheet_deletion(self):
        self.run_sync()
        link = self.user['google']['links']['anatomy']
        del link['base'][self.cid]
        self.sheet.rows.pop()
        self.run_sync()
        self.assertTrue(self.user['workspace']['cards'][self.cid]['deleted'])
        self.assertEqual(len(self.sheet.rows), 1)

    def test_new_app_card_without_baseline_is_exported(self):
        self.run_sync()
        deck = self.user['workspace']['decks'][self.card['deck_id']]
        fresh = w.create_card(self.user['workspace'], deck, {'q': 'New', 'a': 'Answer'})
        link = self.user['google']['links']['anatomy']
        link['pending'] = True
        self.run_sync()
        self.assertFalse(fresh['deleted'])
        self.assertEqual(self.sheet.rows[-1][0], fresh['id'])

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
