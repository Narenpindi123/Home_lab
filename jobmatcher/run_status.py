"""Run progress shared by cron workers and the dashboard process."""
import fcntl
import json
import sqlite3
from datetime import datetime, timezone


def save(db_path, state):
    payload = dict(state, updated_at=datetime.now(timezone.utc).isoformat())
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS matcher_run_state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        conn.execute('INSERT INTO matcher_run_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                     (json.dumps(payload),))


def is_running(db_path):
    try:
        with open(db_path + '.matcher.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
                return False
            except BlockingIOError:
                return True
    except FileNotFoundError:
        return False


def read(db_path):
    state = {'running': False, 'phase': 'Idle', 'completed': 0, 'matches': 0}
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='matcher_run_state'").fetchone()
            row = conn.execute('SELECT payload FROM matcher_run_state WHERE id=1').fetchone() if exists else None
            if row:
                state.update(json.loads(row[0]))
    except (sqlite3.Error, ValueError):
        pass
    active = is_running(db_path)
    if state['running'] and not active:
        state.update(phase='Interrupted', error='The run stopped before completion. Saved results are retained.')
    elif active and not state['running']:
        state.update(phase='Starting', completed=0, matches=0, error=None)
    state['running'] = active
    return state
