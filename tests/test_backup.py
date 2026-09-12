import unittest
from unittest.mock import MagicMock, patch
from cryptography.fernet import Fernet, InvalidToken

import backup_data


class BackupTests(unittest.TestCase):
    def test_encryption_integrity_round_trip_and_migration(self):
        documents = {"users": {"Test": {"email": "private@example.invalid", "salt": "original",
                     "srs": {"1": {"due": 123, "interval": 8}}}}, "reset": {}}
        key = Fernet.generate_key()
        encrypted = backup_data.seal(documents, key)
        self.assertNotIn(b'private@example.invalid', encrypted)
        restored = backup_data.unseal(encrypted, key)
        self.assertEqual(restored, documents)
        result = backup_data.check_migration(restored, [{"id": 1, "q": "Q", "a": "A", "topic": "Topic"}])
        self.assertEqual(result, {"profiles": 1, "migrated": 1})
        self.assertNotIn('workspace', restored['users']['Test'])
        with self.assertRaises(InvalidToken):
            backup_data.unseal(encrypted, Fernet.generate_key())

    def test_nonempty_database_cannot_be_overwritten(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = [True]
        with patch.object(backup_data.psycopg, 'connect') as connect:
            connect.return_value.__enter__.return_value = db
            with self.assertRaises(ValueError):
                backup_data.restore_database('test-only', {'users': {}})
        self.assertFalse(any('INSERT' in call.args[0] for call in db.execute.call_args_list))
