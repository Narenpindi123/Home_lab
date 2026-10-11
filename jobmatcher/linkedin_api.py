"""Self-hosted LinkedIn jobs API with durable runs and a serialized subprocess worker."""
import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from linkedin_local_scraper import integer, normalize_input, scrape

ACTOR = 'cheap_scraper~linkedin-job-scraper'
SOURCE_ACTORS = {'dice': 'solidcode~dice-scraper', 'ats': 'i-scraper~ats-jobs-scraper',
                 'ziprecruiter': 'crawlerbros~ziprecruiter-scraper-pro'}
TERMINAL = {'SUCCEEDED', 'FAILED', 'TIMED-OUT', 'ABORTED'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, created REAL NOT NULL, status TEXT NOT NULL,
                started TEXT NOT NULL, finished TEXT, input TEXT NOT NULL,
                items TEXT NOT NULL DEFAULT '[]', errors TEXT NOT NULL DEFAULT '[]',
                timeout INTEGER NOT NULL)''')
            if 'actor' not in {r[1] for r in db.execute('PRAGMA table_info(runs)')}:
                db.execute("ALTER TABLE runs ADD COLUMN actor TEXT NOT NULL DEFAULT 'cheap_scraper~linkedin-job-scraper'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, run_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def create(self, body, timeout, actor=ACTOR):
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT count(*) FROM runs WHERE status IN ('READY','RUNNING')").fetchone()[0] >= 10:
                raise OverflowError('Run queue is full; retry later')
            db.execute('INSERT INTO runs(id,created,status,started,input,timeout,actor) VALUES(?,?,?,?,?,?,?)',
                       (run_id, time.time(), 'READY', stamp(), json.dumps(body), timeout, actor))
        return self.get(run_id)

    def progress(self, run_id, items, errors):
        with self.connect() as db:
            db.execute('UPDATE runs SET items=?,errors=? WHERE id=?',
                       (json.dumps(items), json.dumps(errors), run_id))

    def finish(self, run_id, status, error=None):
        with self.connect() as db:
            row = db.execute('SELECT errors FROM runs WHERE id=?', (run_id,)).fetchone()
            errors = json.loads(row[0])
            if error:
                errors.append(error)
            db.execute('UPDATE runs SET status=?,finished=?,errors=? WHERE id=?',
                       (status, stamp(), json.dumps(errors), run_id))


def run_info(row):
    items, errors = json.loads(row['items']), json.loads(row['errors'])
    return dict(id=row['id'], actId=row['actor'], status=row['status'], startedAt=row['started'],
                finishedAt=row['finished'], defaultDatasetId=row['id'], itemCount=len(items),
                errors=errors, partial=bool(items and (errors or row['status'] != 'SUCCEEDED')),
                statusMessage='; '.join(errors) if errors else row['status'],
                usageTotalUsd=0, options={'timeoutSecs': row['timeout']})


def worker_loop(store):
    # Prevent two server processes from running the same jobs concurrently.
    lock = open(store.path + '.worker.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with store.connect() as db:
        interrupted = db.execute("SELECT id FROM runs WHERE status='RUNNING'").fetchall()
    for (run_id,) in interrupted:
        store.finish(run_id, 'FAILED', 'Service restarted during run; partial dataset retained')
    while True:
        with store.connect() as db:
            # Keep completed datasets for seven days; active jobs are never pruned.
            db.execute("DELETE FROM runs WHERE created<? AND status NOT IN ('READY','RUNNING')", (time.time() - 604800,))
            row = db.execute("SELECT id,timeout FROM runs WHERE status='READY' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE runs SET status='RUNNING' WHERE id=?", (row[0],))
        if not row:
            time.sleep(.5)
            continue
        run_id, timeout = row
        process = None
        try:
            process = subprocess.Popen([sys.executable, __file__, '--worker', store.path, run_id], start_new_session=True)
            process.wait(timeout=timeout)
            if store.get(run_id)['status'] == 'RUNNING':
                store.finish(run_id, 'FAILED', 'Worker exited without completing the run')
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            store.finish(run_id, 'TIMED-OUT', 'Run time limit reached; partial dataset retained')
        except Exception as exc:
            store.finish(run_id, 'FAILED', f'Worker error: {type(exc).__name__}')


def create_app(store, token):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 65536

    @app.before_request
    def authenticate():
        if request.path == '/health':
            return None
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not supplied or not hmac.compare_digest(supplied.encode(), token.encode()):
            return jsonify(error={'type': 'unauthorized', 'message': 'Bearer token required'}), 401

    @app.errorhandler(Exception)
    def error_response(exc):
        if isinstance(exc, ValueError):
            return jsonify(error={'type': 'invalid-input', 'message': str(exc)}), 400
        if isinstance(exc, OverflowError):
            return jsonify(error={'type': 'queue-full', 'message': str(exc)}), 429
        if isinstance(exc, HTTPException):
            return jsonify(error={'type': 'http-error', 'message': exc.description}), exc.code
        app.logger.exception('API request failed')
        return jsonify(error={'type': 'internal-error', 'message': 'Internal service error'}), 500

    @app.get('/health')
    def health():
        worker = app.config.get('WORKER_THREAD')
        alive = worker is None or worker.is_alive()
        return jsonify(status='ok' if alive else 'error', service='local-linkedin-api',
                       apifyRequired=False, workerAlive=alive), 200 if alive else 503

    def get_run(run_id):
        row = store.get(run_id)
        if row is None:
            from werkzeug.exceptions import NotFound
            raise NotFound('Run or dataset not found (retained for seven days)')
        return row

    def wait(row, seconds):
        end = time.monotonic() + seconds
        while row['status'] not in TERMINAL and time.monotonic() < end:
            time.sleep(.2)
            row = get_run(row['id'])
        return row

    def query_int(name, default, low, high):
        value = request.args.get(name, str(default))
        try:
            value = int(value)
        except (ValueError, TypeError):
            raise ValueError(f'{name} must be an integer') from None
        return integer(value, name, low, high)

    @app.post('/v1/jobs/search')
    @app.post('/v1/sources/<source>/runs')
    @app.post('/v2/acts/<actor>/runs')
    @app.post('/v2/actors/<actor>/runs')
    @app.post('/v2/acts/<actor>/run-sync-get-dataset-items')
    @app.post('/v2/actors/<actor>/run-sync-get-dataset-items')
    def start(actor=ACTOR, source=None):
        if source:
            if source not in SOURCE_ACTORS:
                return jsonify(error={'message': 'Unknown job source'}), 404
            actor = SOURCE_ACTORS[source]
        elif actor in SOURCE_ACTORS.values():
            source = next(k for k, v in SOURCE_ACTORS.items() if v == actor)
        elif actor in (ACTOR, 'linkedin-jobs', '2rJKkhh7vjpX7pvjg'):
            actor = ACTOR
        else:
            return jsonify(error={'message': 'Unsupported local Actor'}), 404
        supported = {'timeout', 'waitForFinish', 'maxItems'}
        if set(request.args) - supported:
            raise ValueError('Unsupported query options: ' + ', '.join(sorted(set(request.args) - supported)))
        body = request.get_json()
        if 'maxItems' in request.args:
            if not isinstance(body, dict):
                raise ValueError('Input must be a JSON object')
            body = dict(body, limit=min(integer(body.get('limit', 1000), 'limit', 1, 1000), query_int('maxItems', 50, 1, 1000)))
        if source:
            from local_job_sources import normalize_input as normalize_source
            normalize_source(source, body)
        else:
            normalize_input(body)
        timeout = query_int('timeout', 1800, 10, 3600)
        wait_seconds = query_int('waitForFinish', 0, 0, 60)
        row = store.create(body, timeout, actor)
        if request.path.endswith('/run-sync-get-dataset-items'):
            row = wait(row, min(timeout + 2, 120))
            if row['status'] not in TERMINAL:
                return jsonify(data=run_info(row), error={'message': 'Still running; poll the run ID'}), 408
            if row['status'] != 'SUCCEEDED':
                return jsonify(data=run_info(row), items=json.loads(row['items']), error={'message': 'Scrape incomplete; inspect run errors'}), 502
            return jsonify(json.loads(row['items']))
        row = wait(row, wait_seconds)
        return jsonify(data=run_info(row)), 201

    @app.get('/v1/runs/<run_id>')
    @app.get('/v2/actor-runs/<run_id>')
    def status(run_id):
        return jsonify(data=run_info(wait(get_run(run_id), query_int('waitForFinish', 0, 0, 60))))

    @app.get('/v1/runs/<run_id>/items')
    @app.get('/v2/datasets/<run_id>/items')
    def items(run_id):
        if set(request.args) - {'offset', 'limit', 'format', 'clean', 'desc'}:
            raise ValueError('Unsupported dataset query options')
        if request.args.get('format', 'json') != 'json':
            raise ValueError('Only JSON output is supported')
        row = get_run(run_id)
        values = json.loads(row['items'])
        if request.args.get('desc', 'false').lower() in ('true', '1'):
            values.reverse()
        offset, limit = query_int('offset', 0, 0, 1000000), query_int('limit', 1000, 1, 1000)
        selected = values[offset:offset + limit]
        response = jsonify(selected)
        for name, value in {'Count': len(selected), 'Offset': offset, 'Limit': limit, 'Total': len(values)}.items():
            response.headers['X-Apify-Pagination-' + name] = str(value)
        response.headers['X-Run-Status'] = row['status']
        return response

    return app


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        store, run_id = Store(sys.argv[2]), sys.argv[3]
        try:
            row = store.get(run_id)
            callback = lambda jobs, errors: store.progress(run_id, jobs, errors)
            source = next((k for k, v in SOURCE_ACTORS.items() if v == row['actor']), None)
            if source:
                from local_job_sources import normalize_input as normalize_source, scrape_source
                config = normalize_source(source, json.loads(row['input']))
                jobs, errors = scrape_source(source, config, callback)
            else:
                config = normalize_input(json.loads(row['input']))
                jobs, errors = scrape(config, callback)
            store.finish(run_id, 'FAILED' if errors else 'SUCCEEDED')
        except Exception as exc:
            store.finish(run_id, 'FAILED', f'Scraper error: {type(exc).__name__}')
        return
    root = Path(os.environ.get('LINKEDIN_API_DATA', '/app/data/linkedin-api'))
    root.mkdir(parents=True, exist_ok=True)
    token_path = root / 'token'
    if not token_path.exists():
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(32))
    token = token_path.read_text().strip()
    if not token:
        raise RuntimeError('API token file is empty')
    store = Store(root / 'runs.sqlite3')
    worker = threading.Thread(target=worker_loop, args=(store,), daemon=True)
    worker.start()
    app = create_app(store, token)
    app.config['WORKER_THREAD'] = worker
    app.run(host='0.0.0.0', port=5556, threaded=True, use_reloader=False)


if __name__ == '__main__':
    main()
