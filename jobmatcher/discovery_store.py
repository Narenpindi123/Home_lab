"""Persistent review decisions and source-run measurements shared by web and matcher."""
import json
from datetime import datetime, timezone


def init_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS job_candidate_results (
        job_url TEXT NOT NULL, candidate TEXT NOT NULL, job_json TEXT NOT NULL,
        analysis TEXT NOT NULL, match_score INTEGER, outcome TEXT NOT NULL,
        evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(job_url,candidate));
    CREATE TABLE IF NOT EXISTS source_runs (
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
        status TEXT NOT NULL, jobs_fetched INTEGER NOT NULL, errors_json TEXT NOT NULL,
        UNIQUE(source,fetched_at));
    CREATE TABLE IF NOT EXISTS source_matches (
        run_id INTEGER NOT NULL, job_url TEXT NOT NULL, candidate TEXT NOT NULL,
        PRIMARY KEY(run_id,job_url,candidate));
    ''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(job_candidate_results)')}
    for name, definition in [('reason', "TEXT DEFAULT ''"), ('eligibility', "TEXT DEFAULT 'unknown'"),
                             ('restored_at', 'TEXT'), ('override_eligibility', 'INTEGER DEFAULT 0')]:
        if name not in columns:
            conn.execute(f'ALTER TABLE job_candidate_results ADD COLUMN {name} {definition}')
    history_cols = {r[1] for r in conn.execute('PRAGMA table_info(jobs_history)')}
    if history_cols and 'published_at' not in history_cols:
        conn.execute('ALTER TABLE jobs_history ADD COLUMN published_at TEXT')
    conn.commit()


def record_run(conn, source, jobs, fetched_at=None, errors=None, status=None):
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    errors = errors or []
    status = status or ('partial' if jobs and errors else 'error' if errors else 'success')
    conn.execute('''INSERT INTO source_runs(source,fetched_at,status,jobs_fetched,errors_json)
        VALUES(?,?,?,?,?) ON CONFLICT(source,fetched_at) DO NOTHING''',
        (source, fetched_at, status, len(jobs), json.dumps(errors)))
    run_id = conn.execute('SELECT id FROM source_runs WHERE source=? AND fetched_at=?', (source, fetched_at)).fetchone()[0]
    conn.commit()
    for job in jobs:
        job['_source_run_id'] = run_id
    return run_id


def record_match(conn, job, candidate):
    if job.get('_source_run_id'):
        conn.execute('INSERT OR IGNORE INTO source_matches VALUES(?,?,?)',
                     (job['_source_run_id'], job.get('jobUrl') or job.get('applyUrl'), candidate))
        conn.commit()


def record_host_snapshot(path, source, snapshot):
    """Measure host fetches immediately; JSON remains the fallback if SQLite is busy."""
    import os
    import sqlite3
    db_path = os.path.join(os.path.dirname(path), 'processed_jobs.db')
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            init_schema(conn)
            record_run(conn, source, snapshot['jobs'], snapshot['scraped_at'],
                       snapshot.get('errors', []), snapshot.get('status', 'unknown'))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'Source metrics will be imported from JSON on the next matcher run: {type(exc).__name__}')


def backfill_publication_dates(conn):
    """Recover only source-supplied absolute dates, never old fetch timestamps."""
    from eligibility import posted_datetime
    updated = 0
    for url, source, posted_time in conn.execute(
            'SELECT job_url,source,posted_time FROM jobs_history WHERE published_at IS NULL').fetchall():
        stamp = posted_datetime({'postedTime': posted_time})
        if stamp is None:
            rows = conn.execute('SELECT job_json FROM job_candidate_results WHERE job_url=? ORDER BY evaluated_at DESC', (url,)).fetchall()
            for (raw,) in rows:
                try:
                    job = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                # Legacy Indeed/Glassdoor publishedAt was the fetch time.
                stamp = posted_datetime(job if (source or '').lower() == 'linkedin' else {'postedTime':job.get('postedTime')})
                if stamp:
                    break
        if stamp:
            conn.execute('UPDATE jobs_history SET published_at=? WHERE job_url=? AND published_at IS NULL', (stamp.isoformat(),url))
            updated += 1
    conn.commit()
    return updated
