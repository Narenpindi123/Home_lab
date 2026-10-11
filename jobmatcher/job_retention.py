"""Expire unscored listings after 30 days, preserving application history."""
import json
import logging
from datetime import datetime, timedelta, timezone

from eligibility import posted_datetime

RETENTION_DAYS = 30


def expired_urls(conn, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RETENTION_DAYS)).date()
    dates = {}
    saved_dates = {}
    scored = set()

    def remember(url, stamp):
        if stamp is not None:
            dates[url] = max(stamp, dates.get(url, stamp))

    for url, published, posted, score, saved in conn.execute(
            'SELECT job_url,published_at,posted_time,match_score,processed_at FROM jobs_history'):
        remember(url, posted_datetime({'publishedAt': published, 'postedTime': posted}))
        stamp = posted_datetime({'publishedAt': saved})
        if stamp is not None:
            saved_dates[url] = stamp
        if score not in (None, 0):
            scored.add(url)
    for url, raw, score, saved in conn.execute(
            'SELECT job_url,job_json,match_score,evaluated_at FROM job_candidate_results'):
        if score not in (None, 0):
            scored.add(url)
        stamp = posted_datetime({'publishedAt': saved})
        if stamp is not None:
            saved_dates[url] = min(stamp, saved_dates.get(url, stamp))
        try:
            job = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(job, dict):
            continue
        # Legacy Indeed/Glassdoor publishedAt was a fetch timestamp, not a
        # posting date. Only trust their original postedTime field.
        if str(job.get('_source', '')).lower() in ('indeed', 'glassdoor'):
            job = {'postedTime': job.get('postedTime')}
        remember(url, posted_datetime(job))
    # Prefer the newer publication when sources disagree (possible repost).
    for url, stamp in saved_dates.items():
        dates.setdefault(url, stamp)
    return sorted(url for url, stamp in dates.items()
                  if url not in scored and stamp.date() < cutoff)


def purge_expired_jobs(conn, now=None):
    """Delete listing content atomically; keep dedup keys and source statistics."""
    counts = {'jobs_history': 0, 'job_candidate_results': 0, 'expired_urls': 0}
    with conn:
        # Reserve the main database writer before reading. Starting with temp
        # table writes leaves a deferred WAL snapshot that cannot be upgraded
        # if a source worker commits during expired_urls() (SQLITE_BUSY_SNAPSHOT).
        # BEGIN IMMEDIATE waits using the connection's busy timeout instead.
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS expired_job_urls (job_url TEXT PRIMARY KEY)')
        conn.execute('DELETE FROM expired_job_urls')
        urls = expired_urls(conn, now)
        conn.executemany('INSERT INTO expired_job_urls VALUES (?)', ((url,) for url in urls))
        counts['expired_urls'] = len(urls)
        conn.execute('INSERT OR IGNORE INTO processed_jobs(job_url) SELECT job_url FROM expired_job_urls')
        for table in ('jobs_history', 'job_candidate_results'):
            counts[table] = conn.execute(
                f'DELETE FROM {table} WHERE job_url IN (SELECT job_url FROM expired_job_urls)').rowcount
        conn.execute('DROP TABLE expired_job_urls')
    logging.getLogger(__name__).info('30-day unscored listing cleanup: %s', counts)
    return counts


def retention_worker(stop_event):
    """Run on startup and every 24 hours, independently of scraper availability."""
    import job_matcher
    while not stop_event.is_set():
        try:
            conn = job_matcher.init_db()
            try:
                purge_expired_jobs(conn)
            finally:
                conn.close()
        except Exception:
            logging.getLogger(__name__).exception('Listing cleanup failed; retrying in one hour')
            stop_event.wait(3600)
        else:
            stop_event.wait(86400)
