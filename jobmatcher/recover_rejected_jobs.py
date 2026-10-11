"""Re-evaluate the August 13 embedding regression without sending notifications.

Checkpoints fetched descriptions and outcomes, keeps original discovery dates,
and never changes an existing dashboard row or application workflow state.
"""
import argparse
import concurrent.futures
import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
import job_matcher as matcher

ROOT = Path(__file__).resolve().parent
RECOVERY_DIR = ROOT / 'data' / 'recovery-20260904'
START = '2026-08-13 00:00:00'
END = '2026-09-05 05:00:00'  # End of September 4 in Texas Central time.
blocked_sources = set()
blocked_lock = threading.Lock()
request_lock = threading.Lock()
last_request = 0.0
request_interval = 10.0
COOLDOWN_FILE = RECOVERY_DIR / 'source_cooldowns.json'


def source_cooldowns():
    if COOLDOWN_FILE.exists():
        return json.loads(COOLDOWN_FILE.read_text())
    return {}


def remember_cooldown(source, response):
    """Persist a source's access response so later runs do not hammer it."""
    with blocked_lock:
        data = source_cooldowns()
        retry_after = response.headers.get('Retry-After', '')
        delay = 900
        if retry_after.isdigit():
            delay = max(delay, int(retry_after))
        elif retry_after:
            try:
                delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
        data[source] = {'status': response.status_code, 'retry_at': time.time() + delay,
                        'retry_after_header': retry_after}
        with COOLDOWN_FILE.open('w') as output:
            json.dump(data, output)
        blocked_sources.add(source)


def identity(url):
    if 'linkedin.com/' in url:
        match = re.search(r'(\d{8,})(?:\?|/|$)', url)
        if match:
            return 'linkedin:' + match.group(1)
    if 'indeed.com/' in url:
        key = parse_qs(urlparse(url).query).get('jk', [''])[0]
        if key:
            return 'indeed:' + key
    return url.split('?')[0]


def recovery_items(conn):
    conn.row_factory = sqlite3.Row
    rows = conn.execute('''SELECT p.job_url,p.processed_at,a.title,a.company,a.location,a.source
        FROM processed_jobs p LEFT JOIN applications a ON a.job_url=p.job_url
        WHERE p.processed_at>=? AND p.processed_at<?
        AND NOT EXISTS(SELECT 1 FROM jobs_history h WHERE h.job_url=p.job_url)
        ORDER BY p.processed_at DESC,a.title IS NULL''', (START, END)).fetchall()
    grouped = {}
    for row in rows:
        key = identity(row['job_url'])
        item = grouped.setdefault(key, dict(key=key, url=row['job_url'],
            processed_at=row['processed_at'], aliases=[], title=row['title'] or '',
            company=row['company'] or '', location=row['location'] or '',
            source=row['source'] or ('LinkedIn' if key.startswith('linkedin:') else 'Indeed')))
        if row['job_url'] not in item['aliases']:
            item['aliases'].append(row['job_url'])
    return list(grouped.values())


def fetch_job(item):
    global last_request
    source = item['source']
    with blocked_lock:
        if source in blocked_sources:
            return None, 'source_access_blocked'
    url = (f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{item['key'].split(':')[1]}"
           if item['key'].startswith('linkedin:') else item['url'])
    try:
        with request_lock:
            delay = request_interval - (time.monotonic() - last_request)
            if delay > 0:
                time.sleep(delay)
            with blocked_lock:
                if source in blocked_sources:
                    return None, 'source_access_blocked'
            last_request = time.monotonic()
        response = requests.get(url, timeout=(8, 30))
    except requests.RequestException as error:
        return None, 'fetch_error:' + type(error).__name__
    if response.status_code in (403, 429, 999):
        remember_cooldown(source, response)
        return None, f'source_access_blocked:HTTP_{response.status_code}'
    if response.status_code in (404, 410):
        return None, 'expired'
    if response.status_code != 200:
        return None, f'HTTP_{response.status_code}'
    soup = BeautifulSoup(response.text, 'html.parser')
    if 'no longer accepting applications' in soup.get_text(' ', strip=True).lower():
        return None, 'expired'
    desc = soup.select_one('div.show-more-less-html__markup, #jobDescriptionText')
    if desc is None or len(desc.get_text(strip=True)) < 150:
        return None, 'description_unavailable'
    def field(selector, fallback):
        node = soup.select_one(selector)
        return node.get_text(' ', strip=True) if node else fallback
    return dict(jobTitle=field('h2.top-card-layout__title, h2', item['title']),
                companyName=field('a.topcard__org-name-link', item['company']),
                location=field('span.topcard__flavor--bullet', item['location']),
                jobDescription=desc.get_text('\n', strip=True), jobUrl=item['url'],
                applyUrl=item['url'], _source=source,
                postedTime=field('span.posted-time-ago__text', 'Previously discovered'),
                contractType='Unknown', _recovered_from=item['processed_at']), 'fetched'


def evaluate_item(item, profiles, cache):
    cached = cache.get(item['key'])
    if cached and cached['job_json']:
        job = json.loads(cached['job_json'])
        reason = 'cached'
    else:
        time.sleep(0.4)
        job, reason = fetch_job(item)
    if not job:
        return item, None, [], reason
    results = []
    for candidate, profile in profiles.items():
        results.append((candidate, matcher.evaluate_candidate_job(job, candidate, profile)))
    return item, job, results, reason


def main():
    global request_interval
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--retry-unavailable', action='store_true')
    parser.add_argument('--retry-rejected', action='store_true')
    parser.add_argument('--source', choices=('LinkedIn', 'Indeed'))
    parser.add_argument('--request-interval', type=float, default=10.0)
    args = parser.parse_args()
    request_interval = max(5.0, args.request_interval)
    RECOVERY_DIR.mkdir(exist_ok=True)
    backup = RECOVERY_DIR / 'processed_jobs.before.db'
    if not backup.exists():
        with sqlite3.connect(f'file:{matcher.DB_PATH}?mode=ro', uri=True) as source:
            with sqlite3.connect(backup) as dest:
                source.backup(dest)
        os.chmod(backup, 0o600)
    conn = matcher.init_db()
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS job_recovery_items (
        identity TEXT PRIMARY KEY, original_url TEXT, original_processed_at TEXT,
        aliases_json TEXT, job_json TEXT, outcome TEXT, detail TEXT,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    cache = {r['identity']: dict(r) for r in conn.execute('SELECT * FROM job_recovery_items')}
    existing = {identity(r[0]) for r in conn.execute('SELECT job_url FROM jobs_history')}
    items = recovery_items(conn)
    if args.source:
        items = [item for item in items if item['source'] == args.source]
    cooldowns = source_cooldowns()
    deferred = {source: state for source, state in cooldowns.items()
                if state['retry_at'] > time.time()}
    items = [item for item in items if item['source'] not in deferred]
    if deferred:
        print('SOURCE_COOLDOWNS ' + json.dumps(deferred), flush=True)
    terminal = {'matched', 'rejected', 'expired', 'existing'}
    if args.retry_rejected:
        terminal.remove('rejected')
    if not args.retry_unavailable:
        terminal.add('unavailable')
    items = [item for item in items if cache.get(item['key'], {}).get('outcome') not in terminal]
    # After a cooldown, attempt untouched listings before retrying a URL that
    # itself failed. This avoids starving the whole queue on one unavailable job.
    items.sort(key=lambda item: (
        cache.get(item['key'], {}).get('detail', '').startswith('source_access_blocked:HTTP_'),
        cache.get(item['key'], {}).get('updated_at', ''),
    ))
    if args.limit:
        items = items[:args.limit]
    print(f'Recovery queue: {len(items)} unique listings; original database backup saved.', flush=True)
    if not items:
        print('No runnable jobs while source cooldown is active.' if deferred
              else 'Nothing pending.', flush=True)
        return
    # Use the already configured local Ollama cloud model; no embedding calls.
    matcher.FORCE_TARGET = 'local'
    matcher.ACTIVE_CHAT_ENDPOINT = ('http://127.0.0.1:11434', 'gpt-oss:120b-cloud')
    matcher.MODEL_NAME = 'gpt-oss:120b-cloud'
    matcher.OLLAMA_EMBEDDINGS_DISABLED_UNTIL = time.time() + 86400
    profile_file = RECOVERY_DIR / 'candidate_profiles.json'
    if profile_file.exists():
        profiles = json.loads(profile_file.read_text())
    else:
        profiles = {name: matcher.summarize_resumes(resumes, name)
                    for name, resumes in matcher.extract_resumes().items() if resumes}
        with profile_file.open('w') as output:
            json.dump(profiles, output)
        os.chmod(profile_file, 0o600)
    if not profiles:
        raise RuntimeError('No candidate resumes available; recovery stopped.')
    counts = Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(evaluate_item, item, profiles, cache): item for item in items
                   if item['key'] not in existing}
        for item in items:
            if item['key'] in existing:
                conn.execute('''INSERT OR REPLACE INTO job_recovery_items
                    (identity,original_url,original_processed_at,aliases_json,outcome,detail)
                    VALUES (?,?,?,?,?,?)''',
                    (item['key'], item['url'], item['processed_at'], json.dumps(item['aliases']),
                     'existing', 'Canonical listing already retained; preserved unchanged'))
                counts['existing'] += 1
        conn.commit()
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            item = futures[future]
            try:
                item, job, results, detail = future.result()
                outcomes = []
                for candidate, result in results:
                    outcomes.append(matcher.persist_candidate_result(conn, job, candidate, result))
                outcome = ('matched' if 'matched' in outcomes else
                           'pending_analysis' if 'pending_analysis' in outcomes else
                           'rejected' if outcomes else 'expired' if detail == 'expired' else 'unavailable')
                if outcome == 'matched':
                    # Recovery items did not exist at startup; preserve first discovery date.
                    conn.execute('UPDATE jobs_history SET processed_at=? WHERE job_url=?',
                                 (item['processed_at'], item['url']))
                conn.execute('''INSERT INTO job_recovery_items
                    (identity,original_url,original_processed_at,aliases_json,job_json,outcome,detail)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                    job_json=excluded.job_json,outcome=excluded.outcome,detail=excluded.detail,
                    updated_at=CURRENT_TIMESTAMP''',
                    (item['key'], item['url'], item['processed_at'], json.dumps(item['aliases']),
                     json.dumps(job) if job else None, outcome, detail))
                conn.commit()
                counts[outcome] += 1
                print(f'RECOVERY {done}/{len(futures)} {item["key"]} {outcome} {dict(counts)}', flush=True)
            except Exception as error:
                counts['error'] += 1
                print(f'RECOVERY_ERROR {item["key"]}: {type(error).__name__}: {error}', flush=True)
    totals = dict(conn.execute('SELECT outcome,count(*) FROM job_recovery_items GROUP BY outcome'))
    print('RECOVERY_TOTALS ' + json.dumps(totals), flush=True)
    print('DASHBOARD ' + str(tuple(conn.execute('SELECT count(*),max(processed_at) FROM jobs_history').fetchone())), flush=True)
    conn.close()


if __name__ == '__main__':
    main()
