"""Dashboard endpoints for reversible review actions and source effectiveness."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from flask import jsonify, request
import discovery_store


def register(app, get_db, get_profile, analyze, pipeline_db):
    @app.get('/api/review')
    def review_list():
        state = request.args.get('outcome', 'needs_review')
        if state not in ('needs_review', 'rejected', 'pending_analysis', 'restored'):
            return jsonify(error='Invalid review state'), 400
        candidate = request.args.get('candidate', '')
        page = max(1, request.args.get('page', 1, type=int))
        conn = get_db()
        try:
            where, params = 'outcome=?', [state]
            if candidate:
                where += ' AND candidate=?'
                params.append(candidate)
            count = conn.execute('SELECT COUNT(*) FROM job_candidate_results WHERE ' + where, params).fetchone()[0]
            rows = conn.execute('SELECT * FROM job_candidate_results WHERE ' + where + ' ORDER BY evaluated_at DESC LIMIT 40 OFFSET ?', params + [(page-1)*40]).fetchall()
            items = []
            for row in rows:
                data = dict(row)
                data['job'] = json.loads(data.pop('job_json'))
                if not data['reason'] or data['reason'] == 'passed':
                    data['reason'] = ('Scoring needs retry' if state == 'pending_analysis' else data['analysis'])
                items.append(data)
            return jsonify(items=items, total=count, page=page)
        finally:
            conn.close()

    @app.post('/api/review/<action>')
    def review_action(action):
        if action not in ('restore', 'rescore'):
            return jsonify(error='Unknown action'), 404
        body = request.get_json(silent=True) or {}
        candidate, url = body.get('candidate'), body.get('job_url')
        if candidate not in ('candidate_a', 'candidate_b') or not isinstance(url, str):
            return jsonify(error='A valid candidate and job URL are required'), 400
        conn = get_db()
        try:
            row = conn.execute('SELECT * FROM job_candidate_results WHERE job_url=? AND candidate=?', (url, candidate)).fetchone()
            if not row:
                return jsonify(error='Review item not found'), 404
            job = json.loads(row['job_json'])
            if action == 'restore':
                stamp = datetime.now(timezone.utc).isoformat()
                conn.execute('''INSERT OR IGNORE INTO jobs_history
                    (job_url,url_hash,title,company,source,description,location,posted_time,contract_type,candidate,ai_analysis)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (url, hashlib.md5(url.encode()).hexdigest()[:12], job.get('jobTitle', ''),
                     job.get('companyName', ''), job.get('_source', ''), job.get('jobDescription', ''),
                     job.get('location', ''), job.get('postedTime', ''), job.get('contractType', ''),
                     candidate, 'Restored by user; re-score to generate an evidence-backed match.'))
                from eligibility import posted_datetime
                posted = posted_datetime(job)
                conn.execute('UPDATE jobs_history SET skipped=0,published_at=COALESCE(published_at,?) WHERE job_url=?', (posted.isoformat() if posted else None, url))
                conn.execute('''UPDATE job_candidate_results SET outcome='restored',restored_at=?,
                    override_eligibility=1 WHERE job_url=? AND candidate=?''', (stamp, url, candidate))
                import candidate_tracking
                conn.execute('DELETE FROM candidate_dismissals WHERE job_url=? AND candidate=?', (url, candidate))
                candidate_tracking.save_result(conn, job, candidate,
                    'Restored by user; re-score to generate an evidence-backed match.', 0, 'matched')
                conn.execute('UPDATE candidate_jobs SET skipped=0 WHERE job_url=? AND candidate=?', (url, candidate))
                conn.execute('INSERT OR IGNORE INTO processed_jobs(job_url) VALUES(?)', (url,))
                conn.commit()
                return jsonify(message='Restored to Jobs. Eligibility override is recorded for this candidate; re-score when ready.')
            # Release the read connection before a potentially slow model request.
            override = bool(row['override_eligibility'])
            conn.close()
            conn = None
            import job_matcher as matcher
            from eligibility import outside_discovery_window
            result = matcher.evaluate_candidate_job(job, candidate, get_profile(candidate),
                override_eligibility=override, analyzer=analyze)
            if outside_discovery_window(job) and not override:
                result.update(eligibility='rejected', reason='Posted outside the seven-day discovery window',
                    analysis='❌ Skipped via hard filter: Posted outside the seven-day discovery window')
            analysis = result['analysis']
            conn = get_db()
            outcome = matcher.persist_candidate_result(conn, job, candidate, result)
            # Re-scoring updates the visible analysis only for the same candidate,
            # preserving applied/starred/Notion state and other candidates' matches.
            conn.execute('''UPDATE candidate_jobs SET ai_analysis=?,match_score=?
                WHERE job_url=? AND candidate=?''', (analysis, matcher.get_score(analysis), url, candidate))
            conn.commit()
            return jsonify(message='Re-score complete: ' + outcome.replace('_', ' '), outcome=outcome, analysis=analysis)
        except Exception as exc:
            app.logger.exception('Review action failed')
            return jsonify(error='Review action failed; your saved item is retained.'), 500
        finally:
            if conn is not None:
                conn.close()

    @app.get('/api/source-health')
    def source_health():
        conn = get_db()
        try:
            sources = {r[0] for r in conn.execute('SELECT DISTINCT source FROM source_runs')} | {'LinkedIn','Indeed','Glassdoor'}
            import local_job_sources
            try:
                configured = {s['name']:s['enabled'] for s in local_job_sources.configured_sources()}
            except (OSError, ValueError, KeyError):
                configured = {}
            import ziprecruiter_scraper
            configured['ZipRecruiter'] = ziprecruiter_scraper.enabled()
            sources.update(configured)
            items = []
            for source in sorted(sources):
                latest = conn.execute('SELECT * FROM source_runs WHERE source=? ORDER BY fetched_at DESC LIMIT 1', (source,)).fetchone()
                success = conn.execute("SELECT MAX(fetched_at) FROM source_runs WHERE source=? AND status='success'", (source,)).fetchone()[0]
                total_matches = conn.execute('''SELECT COUNT(DISTINCT m.job_url) FROM source_matches m
                    JOIN source_runs r ON r.id=m.run_id WHERE r.source=?''', (source,)).fetchone()[0]
                item = dict(source=source, status='not measured', last_success=success,
                            jobs_fetched=None, unique_matches=total_matches, latest_unique_matches=0, errors=[],
                            applications=0, interviews=0, interview_rate=None)
                if latest:
                    item.update(status=latest['status'], last_attempt=latest['fetched_at'],
                        jobs_fetched=latest['jobs_fetched'], errors=json.loads(latest['errors_json']))
                    item['latest_unique_matches'] = conn.execute('SELECT COUNT(DISTINCT job_url) FROM source_matches WHERE run_id=?', (latest['id'],)).fetchone()[0]
                if source in configured and not configured[source]:
                    item['status'] = 'disabled'
                items.append(item)
        finally:
            conn.close()
        # Count unique applications that ever reached interview, including those
        # which subsequently became offers/rejections; repeated events count once.
        pipeline = sqlite3.connect(pipeline_db)
        try:
            tables = {r[0] for r in pipeline.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            available = {'applications', 'application_events'} <= tables
            if available:
                for item in items:
                    counts = pipeline.execute('''SELECT
                        SUM(CASE WHEN a.applied_at IS NOT NULL OR a.status IN ('applied','interviewing','mid_convo','offer','rejected')
                            OR EXISTS(SELECT 1 FROM application_events e WHERE e.application_id=a.application_id AND e.status_to='applied') THEN 1 ELSE 0 END),
                        SUM(CASE WHEN a.status IN ('interviewing','offer') OR EXISTS(
                            SELECT 1 FROM application_events e WHERE e.application_id=a.application_id
                            AND (e.status_to='interviewing' OR e.event_type IN ('email_interviewing','interviewing')))
                            THEN 1 ELSE 0 END)
                        FROM applications a WHERE lower(a.source)=lower(?)''', (item['source'],)).fetchone()
                    item['applications'], item['interviews'] = (counts[0] or 0), (counts[1] or 0)
                    if item['applications']:
                        item['interview_rate'] = round(item['interviews'] / item['applications'] * 100, 1)
            return jsonify(items=items, pipeline_available=available)
        finally:
            pipeline.close()
