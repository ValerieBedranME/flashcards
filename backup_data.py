"""Encrypted PostgreSQL snapshots. Restore defaults to an empty database only.

Credentials are read from the environment and are never printed. Keep BACKUP_KEY
outside the repository and separate from the encrypted backup file.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time

from cryptography.fernet import Fernet
import psycopg
from psycopg.types.json import Jsonb

import workspace


def seal(documents, key):
    body = {"format": "flashcards-documents-v1", "created_at": time.time(), "documents": documents}
    return Fernet(key).encrypt(json.dumps(body, ensure_ascii=False).encode())


def unseal(raw, key):
    body = json.loads(Fernet(key).decrypt(raw))
    if body.get("format") != "flashcards-documents-v1" or not isinstance(body.get("documents"), dict):
        raise ValueError("Invalid backup format")
    if any(not isinstance(name, str) or not isinstance(data, dict) for name, data in body["documents"].items()):
        raise ValueError("Invalid document structure")
    return body["documents"]


def check_migration(documents, base):
    """Dry-run against a copy; no database write or password/progress output."""
    users = deepcopy(documents.get("users", {}))
    converted = 0
    for name, user in users.items():
        legacy = "workspace" not in user
        expected = {str(card["id"]): dict(card, **user.get("edited", {}).get(str(card["id"]), {}))
                    for card in list(base) + user.get("added", [])} if legacy else {}
        auth = {k: deepcopy(user.get(k)) for k in ("salt", "password_hash", "email", "srs", "stacks")}
        converted += workspace.migrate_user(user, base, name)
        if auth != {k: user.get(k) for k in auth}:
            raise ValueError("Migration changed account credentials or progress")
        once = deepcopy(user)
        if workspace.migrate_user(user, base, name) or user != once:
            raise ValueError("Migration is not idempotent")
        for cid, original in expected.items():
            card = user["workspace"]["cards"].get(cid, {})
            if any(card.get(k) != original.get(k) for k in ("id", "q", "a")):
                raise ValueError("Migration changed card identity or content")
            if card.get("deleted") != (cid in {str(value) for value in user.get("deleted", [])}):
                raise ValueError("Migration changed archived cards")
    return {"profiles": len(users), "migrated": converted}


def export_database(url):
    with psycopg.connect(url, connect_timeout=10) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        rows = db.execute("SELECT name, data FROM flashcards_documents ORDER BY name").fetchall()
        return dict(rows)


def restore_database(url, documents):
    with psycopg.connect(url, connect_timeout=10) as db:
        db.execute("SELECT pg_advisory_xact_lock(784261903)")
        db.execute("CREATE TABLE IF NOT EXISTS flashcards_documents (name TEXT PRIMARY KEY, data JSONB NOT NULL)")
        if db.execute("SELECT EXISTS (SELECT 1 FROM flashcards_documents)").fetchone()[0]:
            raise ValueError("Restore target is not empty; existing data was not changed")
        for name, value in documents.items():
            db.execute("INSERT INTO flashcards_documents (name, data) VALUES (%s, %s)", (name, Jsonb(value)))
        restored = dict(db.execute("SELECT name, data FROM flashcards_documents").fetchall())
        if restored != documents:
            raise ValueError("Restore comparison failed; transaction rolled back")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "check", "restore-empty"))
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    key = os.environ.get("BACKUP_KEY", "").encode()
    Fernet(key)  # Validate before opening a database or output file.
    if args.action == "export":
        url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
        if not url:
            raise ValueError("DATABASE_URL is required")
        documents = export_database(url)
        raw = seal(documents, key)
        if unseal(raw, key) != documents:
            raise ValueError("Backup verification failed")
        fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
    else:
        raw = args.file.read_bytes()
        documents = unseal(raw, key)
        if args.action == "restore-empty":
            url = os.environ.get("RESTORE_DATABASE_URL")
            if not url:
                raise ValueError("RESTORE_DATABASE_URL must explicitly identify an empty test database")
            restore_database(url, documents)
    base = json.loads(Path(__file__).with_name("cards.json").read_text())
    print(json.dumps({"action": args.action, "documents": len(documents),
                      "sha256": hashlib.sha256(raw).hexdigest(), **check_migration(documents, base)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Connection errors can contain database details. Show only the error type.
        raise SystemExit("Backup action stopped safely (" + type(error).__name__ + "). Check configuration and target.") from None
