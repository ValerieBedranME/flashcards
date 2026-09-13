from copy import deepcopy
import unittest

import workspace as w


class WorkspaceTests(unittest.TestCase):
    def test_single_topic_scope_keeps_old_shared_topics_and_copies_separate(self):
        ws = w.empty_workspace()
        first = w.create_deck(ws, 'A', {'name':'One', 'topic':'Old group'})
        second = w.create_deck(ws, 'B', {'name':'Two', 'topic':'Old group'})
        a = w.create_card(ws, first, {'q':'A', 'a':'A'})
        b = w.create_card(ws, second, {'q':'B', 'a':'B'})
        self.assertEqual(first['topic_id'], second['topic_id'])
        srs = {a['id']:{'due':100,'last':'know'}}
        before = deepcopy(ws)
        self.assertEqual(w.study_summary(ws,srs,'anatomy',mode='all',deck_id=first['id'])['ids'],[a['id']])
        self.assertEqual(w.study_summary(ws,srs,'anatomy',mode='all',deck_id=second['id'])['ids'],[b['id']])
        with self.assertRaises(w.Problem):
            w.study_summary(ws,srs,'latin',deck_id=first['id'])
        self.assertEqual(ws,before)

    def test_migration_preserves_changes_ids_and_progress_and_can_restore(self):
        base = [{"id": 1, "topic": "Тема", "q": "Q1", "a": "A1"},
                {"id": 2, "topic": "Тема", "q": "Q2", "a": "A2"}]
        user = {"email": "test@example.com", "salt": "keep", "password_hash": "keep",
                "edited": {"1": {"q": "Личная правка", "topic": "Другая тема"}},
                "deleted": [2], "added": [{"id": 7, "q": "Личная", "a": "Ответ", "topic": "Тема"}],
                "srs": {"1": {"last": "know", "due": 1234, "interval": 8}}}
        before = deepcopy(user)
        self.assertTrue(w.migrate_user(user, base, "Alice"))
        self.assertEqual([c["id"] for c in w.active_cards(user["workspace"])], [1, 7])
        self.assertEqual(user["workspace"]["cards"]["1"]["q"], "Личная правка")
        self.assertTrue(user["workspace"]["cards"]["2"]["deleted"])
        self.assertEqual(user["srs"], before["srs"])
        self.assertEqual(user["salt"], before["salt"])
        self.assertEqual(user["legacy_backup_v1"], before)
        after = deepcopy(user)
        self.assertFalse(w.migrate_user(user, base, "Alice"))
        self.assertEqual(user, after)
        restored = deepcopy(user["legacy_backup_v1"])
        self.assertEqual(restored, before)

    def test_study_scopes_and_due_boundary_keep_single_progress(self):
        ws = w.empty_workspace()
        a = w.create_deck(ws, "A", {"name": "A", "subject_id": "anatomy", "topic": "Одинаковая"})
        b = w.create_deck(ws, "A", {"name": "B", "subject_id": "latin", "topic": "Одинаковая"})
        cards = [w.create_card(ws, d, {"q": "Q", "a": "A"}) for d in (a, a, b)]
        srs = {str(cards[0]["id"]): {"due": 100, "last": "know"},
               str(cards[2]["id"]): {"due": 101, "last": "unsure"}}
        self.assertNotEqual(a["topic_id"], b["topic_id"])
        self.assertEqual(w.study_summary(ws, srs, now=100)["ids"], [cards[0]["id"]])
        self.assertEqual(w.study_summary(ws, srs, "anatomy", a["topic_id"], "all", 100)["total"], 2)
        self.assertEqual(w.study_summary(ws, srs, "microbiology", now=100)["total"], 0)
        w.change_card(ws, cards[0], deleted=True)
        self.assertEqual(w.study_summary(ws, srs, now=100)["due"], 0)
        w.change_card(ws, cards[0], deleted=False)
        self.assertEqual(w.study_summary(ws, srs, now=100)["stats"]["know"], 1)

    def test_idempotency_rejects_reusing_key_with_other_content(self):
        ws = w.empty_workspace()
        fn = lambda: w.create_deck(ws, "A", {"name": "A"})
        first = w.operation(ws, "one", {"name": "A"}, fn)
        self.assertEqual(w.operation(ws, "one", {"name": "A"}, fn), first)
        self.assertEqual(len(ws["decks"]), 1)
        with self.assertRaises(w.Problem):
            w.operation(ws, "one", {"name": "B"}, fn)

    def test_approved_nine_card_fixture_has_exact_scope_counts(self):
        ws = w.empty_workspace()
        srs, decks = {}, []
        for subject, topic, total, due in [('anatomy', 'Первая', 4, 2), ('anatomy', 'Вторая', 2, 1), ('latin', 'Первая', 3, 1)]:
            deck = w.create_deck(ws, 'Owner', {'name': topic, 'topic': topic, 'subject_id': subject})
            decks.append(deck)
            for i in range(total):
                card = w.create_card(ws, deck, {'q': str(i), 'a': 'Answer'})
                if i < due:
                    srs[card['id']] = {'due': 100, 'last': 'know'}
        archived = w.create_card(ws, decks[0], {'q': 'Archived', 'a': 'Answer'})
        w.change_card(ws, archived, deleted=True)
        srs[archived['id']] = {'due': 99, 'last': 'dontknow'}
        scopes = [(None, None), ('anatomy', None), ('anatomy', decks[0]['topic_id'])]
        self.assertEqual([len(w.study_summary(ws, srs, sid, tid, 'due', 100)['ids']) for sid, tid in scopes], [4, 3, 2])
        self.assertEqual([len(w.study_summary(ws, srs, sid, tid, 'all', 100)['ids']) for sid, tid in scopes], [9, 6, 4])
        self.assertEqual(w.study_summary(ws, srs, 'microbiology', now=100)['ids'], [])

    def test_rebase_keeps_unrelated_changes_and_rejects_incompatible_content(self):
        before = {'cards': {'a': {'q': 'Old'}, 'b': {'q': 'Old'}}, 'srs': {}}
        local = deepcopy(before); local['cards']['a']['q'] = 'Local'
        latest = deepcopy(before); latest['cards']['b']['q'] = 'Other'
        latest['srs']['b'] = {'interval': 8}
        merged = w.rebase(before, local, latest)
        self.assertEqual(merged['cards'], {'a': {'q': 'Local'}, 'b': {'q': 'Other'}})
        self.assertEqual(merged['srs'], latest['srs'])
        latest['cards']['a']['q'] = 'Competing'
        with self.assertRaises(w.Problem):
            w.rebase(before, local, latest)


if __name__ == "__main__":
    unittest.main()
