import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from job_retention import expired_urls, purge_expired_jobs, retention_worker

NOW = datetime(2026, 9, 11, 18, tzinfo=timezone.utc)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE jobs_history(job_url TEXT PRIMARY KEY, published_at TEXT, posted_time TEXT,
                                      match_score INTEGER, processed_at TEXT);
            CREATE TABLE job_candidate_results(job_url TEXT, job_json TEXT, match_score INTEGER, evaluated_at TEXT);
            CREATE TABLE processed_jobs(job_url TEXT PRIMARY KEY);
            CREATE TABLE source_matches(job_url TEXT);
            CREATE TABLE applications(job_url TEXT, status TEXT);
        ''')

    def add(self, url, date=None, job=None):
        self.conn.execute('INSERT INTO jobs_history(job_url,published_at) VALUES(?,?)', (url, date))
        if job is not None:
            self.conn.execute('INSERT INTO job_candidate_results(job_url,job_json) VALUES(?,?)', (url, json.dumps(job)))
        self.conn.commit()

    def test_cutoff_and_unknown_dates(self):
        for url, date in [('old', '2026-08-11'), ('boundary', '2026-08-12'),
                          ('fresh', '2026-09-10'), ('unknown', None), ('bad', 'invalid')]:
            self.add(url, date)
        self.assertEqual(expired_urls(self.conn, NOW), ['old'])
        self.assertEqual(purge_expired_jobs(self.conn, NOW)['jobs_history'], 1)
        self.assertEqual(purge_expired_jobs(self.conn, NOW)['expired_urls'], 0)

    def test_review_only_deletion_preserves_history_and_dedup(self):
        self.conn.execute('INSERT INTO job_candidate_results(job_url,job_json) VALUES(?,?)',
                          ('old', json.dumps({'publishedAt': '2026-07-01', '_source': 'Dice'})))
        self.conn.execute("INSERT INTO applications VALUES('old','interviewing')")
        self.conn.execute("INSERT INTO source_matches VALUES('old')")
        self.conn.commit()
        result = purge_expired_jobs(self.conn, NOW)
        self.assertEqual(result['job_candidate_results'], 1)
        self.assertEqual(self.conn.execute('SELECT * FROM processed_jobs').fetchall(), [('old',)])
        self.assertEqual(self.conn.execute('SELECT status FROM applications').fetchone()[0], 'interviewing')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_matches').fetchone()[0], 1)

    def test_reposts_legacy_fetch_dates_and_invalid_json(self):
        self.add('repost', '2026-07-01', {'publishedAt': '2026-09-10'})
        self.add('legacy', None, {'_source': 'Indeed', 'publishedAt': '2026-07-01', 'postedTime': '2 days ago'})
        self.add('legacy_absolute', None, {'_source': 'Glassdoor', 'postedTime': '2026-07-01'})
        for raw in ('broken', 'null', '[]'):
            self.conn.execute('INSERT INTO job_candidate_results(job_url,job_json) VALUES(?,?)', ('bad', raw))
        self.conn.commit()
        self.assertEqual(expired_urls(self.conn, NOW), ['legacy_absolute'])

    def test_scored_jobs_are_preserved_in_either_table(self):
        for url in ('null', 'zero', 'scored', 'candidate_scored'):
            self.add(url, '2026-07-01', {'publishedAt': '2026-07-01'})
        self.conn.execute("UPDATE jobs_history SET match_score=0 WHERE job_url='zero'")
        self.conn.execute("UPDATE jobs_history SET match_score=75 WHERE job_url='scored'")
        self.conn.execute("UPDATE job_candidate_results SET match_score=50 WHERE job_url='candidate_scored'")
        self.conn.commit()
        self.assertEqual(expired_urls(self.conn, NOW), ['null', 'zero'])
        purge_expired_jobs(self.conn, NOW)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs_history').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM job_candidate_results').fetchone()[0], 2)

    def test_unknown_publication_uses_saved_age_but_reposts_use_publication(self):
        for url, published in [('old_saved', None), ('fresh_saved', None),
                               ('repost_saved', '2026-09-10')]:
            self.add(url, published)
        self.conn.execute("UPDATE jobs_history SET processed_at='2026-07-01'")
        self.conn.execute("UPDATE jobs_history SET processed_at='2026-09-10' WHERE job_url='fresh_saved'")
        self.conn.execute("INSERT INTO job_candidate_results VALUES('review_only','broken',NULL,'2026-07-01')")
        self.conn.commit()
        self.assertEqual(expired_urls(self.conn, NOW), ['old_saved', 'review_only'])

    def test_daily_worker_runs_without_scraping(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        conn = Mock()
        with patch('job_matcher.init_db', return_value=conn), patch('job_retention.purge_expired_jobs') as purge:
            retention_worker(stop)
        purge.assert_called_once_with(conn)
        conn.close.assert_called_once()
        stop.wait.assert_called_once_with(86400)

    def test_cleanup_serializes_with_concurrent_source_writes(self):
        # Source workers can finish while retention scans a WAL database.
        # A deferred read snapshot cannot be upgraded after their commit.
        with tempfile.TemporaryDirectory() as directory:
            path = directory + '/jobs.db'
            with sqlite3.connect(path) as conn, sqlite3.connect(path, timeout=0.01) as writer:
                self.conn.backup(conn)
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute("INSERT INTO jobs_history(job_url,published_at) VALUES('old','2026-07-01')")
                conn.commit()
                blocked = []

                def scan(connection, now):
                    urls = expired_urls(connection, now)
                    try:
                        writer.execute("INSERT INTO source_matches VALUES('new')")
                        writer.commit()
                    except sqlite3.OperationalError as exc:
                        self.assertIn('locked', str(exc))
                        writer.rollback()
                        blocked.append(True)
                    return urls

                with patch('job_retention.expired_urls', side_effect=scan):
                    self.assertEqual(purge_expired_jobs(conn, NOW)['expired_urls'], 1)
                self.assertEqual(blocked, [True])
                writer.execute("INSERT INTO source_matches VALUES('new')")
                writer.commit()
                self.assertEqual(conn.execute('SELECT * FROM source_matches').fetchall(), [('new',)])


if __name__ == '__main__':
    unittest.main()
