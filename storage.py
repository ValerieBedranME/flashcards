"""Persistent JSON documents in PostgreSQL; JSON files for local development."""
import json
import os
import threading

from flask import g, request

LOCAL_LOCK = threading.RLock()


def init_storage(app):
    @app.before_request
    def start_storage():
        if not request.path.startswith('/api/'):
            return
        url = app.config.get('DATABASE_URL')
        if url:
            import psycopg
            g.db = psycopg.connect(url, connect_timeout=10)
            # Serialize read-modify-write across Vercel instances, not just threads.
            g.db.execute("SET LOCAL statement_timeout = '15s'")
            g.db.execute('SELECT pg_advisory_xact_lock(784261903)')
            g.db.execute('''CREATE TABLE IF NOT EXISTS flashcards_documents (
                name TEXT PRIMARY KEY, data JSONB NOT NULL
            )''')
        elif os.environ.get('VERCEL'):
            return {'error': 'База данных ещё не подключена'}, 503
        else:
            LOCAL_LOCK.acquire()
            g.local_storage_locked = True

    @app.after_request
    def finish_storage(response):
        db = g.get('db')
        if db:
            if response.status_code < 400:
                db.commit()
            else:
                db.rollback()
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.teardown_request
    def close_storage(error):
        db = g.pop('db', None)
        if db:
            db.close()  # Rolls back any unfinished transaction on errors.
        if g.pop('local_storage_locked', False):
            LOCAL_LOCK.release()


def load_document(name, path):
    db = g.get('db')
    if db:
        row = db.execute(
            'SELECT data FROM flashcards_documents WHERE name = %s', (name,)
        ).fetchone()
        return row[0] if row else {}
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as source:
        return json.load(source)


def save_document(name, path, data):
    db = g.get('db')
    if db:
        from psycopg.types.json import Jsonb
        db.execute('''INSERT INTO flashcards_documents (name, data) VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data''',
            (name, Jsonb(data)))
        return
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as target:
        json.dump(data, target, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
