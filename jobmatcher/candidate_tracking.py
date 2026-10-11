"""Independent dashboard/application state for each job and candidate."""
import hashlib
import json


def candidate_id(url, candidate):
    return hashlib.md5(url.encode()).hexdigest()[:12] + ':' + candidate


def init_schema(conn):
    columns = conn.execute('PRAGMA table_info(jobs_history)').fetchall()
    definitions = [f'"{r[1]}" {r[2]}' + (f' DEFAULT {r[4]}' if r[4] is not None else '') for r in columns]
    conn.execute('CREATE TABLE IF NOT EXISTS candidate_jobs (' + ','.join(definitions) + ', PRIMARY KEY(job_url,candidate))')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS candidate_jobs_id ON candidate_jobs(url_hash)')
    conn.execute('CREATE TABLE IF NOT EXISTS candidate_dismissals(job_url TEXT,candidate TEXT,PRIMARY KEY(job_url,candidate))')
    conn.execute('CREATE TABLE IF NOT EXISTS matcher_migrations(name TEXT PRIMARY KEY)')
    if not conn.execute("SELECT 1 FROM matcher_migrations WHERE name='candidate_tracking_v1'").fetchone():
        names = [r[1] for r in columns]
        for values in conn.execute('SELECT * FROM jobs_history').fetchall():
            row = dict(zip(names, values))
            candidate = (row.get('candidate') or 'candidate_a').lower()
            row.update(candidate=candidate, url_hash=candidate_id(row['job_url'], candidate))
            _insert(conn, row)
        # Shared matches get independent defaults. Legacy actions belong only
        # to the person named on the original history row.
        for url, candidate, raw, analysis, score, saved_at in conn.execute('''SELECT r.job_url,r.candidate,r.job_json,r.analysis,r.match_score,h.processed_at
                FROM job_candidate_results r JOIN jobs_history h ON h.job_url=r.job_url
                WHERE r.outcome='matched' ''').fetchall():
            save_result(conn, json.loads(raw), candidate, analysis, score, 'matched')
            conn.execute('UPDATE candidate_jobs SET processed_at=? WHERE job_url=? AND candidate=?', (saved_at,url,candidate))
        conn.execute("INSERT INTO matcher_migrations VALUES('candidate_tracking_v1')")
    conn.commit()


def _insert(conn, row):
    columns = ','.join('"' + key + '"' for key in row)
    conn.execute(f'INSERT OR IGNORE INTO candidate_jobs ({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))


def save_result(conn, job, candidate, analysis, score, outcome):
    url = job.get('jobUrl') or job.get('applyUrl')
    if not url:
        return
    if outcome != 'matched':
        conn.execute('''DELETE FROM candidate_jobs WHERE job_url=? AND candidate=?
            AND COALESCE(applied,0)=0 AND COALESCE(starred,0)=0 AND COALESCE(skipped,0)=0
            AND COALESCE(notion_page_id,'')='' ''', (url, candidate))
        return
    if conn.execute('SELECT 1 FROM candidate_dismissals WHERE job_url=? AND candidate=?', (url, candidate)).fetchone():
        return
    from eligibility import posted_datetime
    posted = posted_datetime(job)
    row = dict(job_url=url, candidate=candidate, url_hash=candidate_id(url,candidate),
               title=job.get('jobTitle',''), company=job.get('companyName',''),
               source=job.get('_source',''), description=job.get('jobDescription',''),
               location=job.get('location',''), posted_time=job.get('postedTime',''),
               contract_type=job.get('contractType',''), published_at=posted.isoformat() if posted else None,
               ai_analysis=analysis, match_score=score)
    _insert(conn, row)
    conn.execute('''UPDATE candidate_jobs SET ai_analysis=?,match_score=?,description=?,
        published_at=COALESCE(?,published_at) WHERE job_url=? AND candidate=?''',
        (analysis, score, row['description'], row['published_at'], url, candidate))


def dismiss(conn, job_id):
    conn.execute('''INSERT OR IGNORE INTO candidate_dismissals
        SELECT job_url,candidate FROM candidate_jobs WHERE url_hash=?''', (job_id,))
    conn.execute('DELETE FROM candidate_jobs WHERE url_hash=?', (job_id,))
