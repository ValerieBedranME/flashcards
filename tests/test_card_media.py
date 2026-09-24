import base64
import io
import unittest
from PIL import Image
import test_workspace_api as api_tests
import workspace as w
import google_sync as sync


class CardMediaTests(unittest.TestCase):
    setUp = api_tests.WorkspaceApiTests.setUp
    boot = api_tests.WorkspaceApiTests.boot
    call = api_tests.WorkspaceApiTests.call
    card = api_tests.WorkspaceApiTests.card

    def upload(self, client, color="red"):
        out = io.BytesIO()
        Image.new("RGB", (40, 30), color).save(out, "PNG")
        return self.call(client, '/images', {"data": base64.b64encode(out.getvalue()).decode()})['id']

    def test_private_upload_retry_and_validation(self):
        image = self.upload(self.a)
        self.assertEqual(image, self.upload(self.a))
        self.assertNotEqual(image, self.upload(self.b))
        with self.a.get('/api/v2/images/'+image) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, 'image/webp')
            self.assertEqual(Image.open(io.BytesIO(response.data)).size, (40, 30))
            self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.b.get('/api/v2/images/'+image).status_code, 404)
        with self.a.session_transaction() as session:
            session.clear()
        self.assertEqual(self.a.get('/api/v2/images/'+image).status_code, 401)
        self.call(self.b, '/images', {'data': base64.b64encode(b'<svg onload="alert(1)"></svg>').decode()}, expected=400)
        self.call(self.b, '/images', {'data': 'a'*3_000_000}, expected=413)

    def test_copy_survives_original_edit_trash_and_keeps_new_id(self):
        deck, original = self.card(self.b)
        image = self.upload(self.b)
        original = self.call(self.b, '/cards/'+original['id'], dict(original, q_image=image, a_image=image), 'PATCH')['card']
        current_deck = next(d for d in self.boot(self.b)['decks'] if d['id'] == deck['id'])
        pub = self.call(self.b, '/decks/'+deck['id']+'/publish', {'revision':current_deck['revision']})['id']
        self.call(self.a, '/library/'+pub+'/copy')
        copy = self.boot(self.a)['cards'][0]
        self.assertNotEqual(copy['id'], original['id'])
        self.assertEqual(copy['q_image'], image)
        replacement = self.upload(self.a, 'blue')
        self.call(self.a, '/cards/'+copy['id'], dict(copy, q_image=replacement), 'PATCH')
        self.assertEqual(self.boot(self.b)['cards'][0]['q_image'], image)
        self.call(self.b, '/cards/'+original['id'], {'revision':original['revision']}, 'DELETE')
        self.assertEqual(self.a.get('/api/v2/images/'+image).status_code, 200)
        self.assertEqual(self.a.get('/api/v2/library').get_json()[0]['cards'][0]['q_image'], image)
        self.assertEqual(self.boot(self.a)['srs'], {})

    def test_image_only_sides_and_foreign_reference_rejected(self):
        deck, original = self.card(self.a)
        foreign = self.upload(self.b)
        self.call(self.a, '/cards', {'deck_id':deck['id'], 'q':'', 'q_image':foreign, 'a':'A'}, expected=403)
        image = self.upload(self.a)
        card = self.call(self.a, '/cards', {'deck_id':deck['id'], 'q':'', 'a':'', 'q_image':image, 'a_image':image})['card']
        self.call(self.a, '/cards/'+card['id'], dict(card, q_image=''), 'PATCH', expected=400)
        changed = self.call(self.a, '/cards/'+card['id'], dict(card, q='Text', q_image=''), 'PATCH')['card']
        self.assertEqual(changed['id'], card['id'])
        self.assertEqual(changed['q_image'], '')
        self.call(self.a, '/cards/'+card['id'], {'revision':changed['revision']}, 'DELETE')
        trashed = next(c for c in self.boot(self.a)['trash'] if c['id'] == card['id'])
        restored = self.call(self.a, '/cards/'+card['id']+'/restore', {'revision':trashed['revision']})['card']
        self.assertEqual(restored['a_image'], image)

    def test_text_sheet_roundtrip_preserves_image_and_hash_detects_replacement(self):
        ws = w.empty_workspace()
        deck = w.create_deck(ws, 'A', {'name':'T'})
        image = 'image_' + 'a'*64
        card = w.create_card(ws, deck, {'q':'', 'a':'A', 'q_image':image})
        before = sync.subject_hash(ws, 'anatomy')
        row = sync.serialize(ws, card)
        row[6] = 'New answer'
        parsed, _ = sync.parse_rows(ws, 'A', 'anatomy', 'file', [sync.HEADERS,row])
        self.assertEqual(parsed[card['id']]['q_image'], image)
        w.change_card(ws, card, parsed[card['id']])
        self.assertEqual(card['q_image'], image)
        self.assertNotEqual(before, sync.subject_hash(ws, 'anatomy'))
        before = sync.subject_hash(ws, 'anatomy')
        w.change_card(ws, card, dict(card, q_image='image_'+'b'*64))
        self.assertNotEqual(before, sync.subject_hash(ws, 'anatomy'))

    def test_conflict_can_choose_image_removal(self):
        _, card = self.card(self.a)
        image = self.upload(self.a)
        card = self.call(self.a, '/cards/'+card['id'], dict(card,q_image=image), 'PATCH')['card']
        removed = self.call(self.a, '/cards/'+card['id'], dict(card,q_image=''), 'PATCH')['card']
        conflict = self.call(self.a, '/cards/'+card['id'], dict(card,a='Concurrent'), 'PATCH', expected=409)['conflict']
        self.call(self.a, '/conflicts/'+conflict['id'], {'version':conflict['version'], 'choice':'current'})
        self.assertFalse(self.boot(self.a)['cards'][0].get('q_image'))

