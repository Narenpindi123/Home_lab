import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, mock_open
from flask import Flask
import job_matcher as matcher
import discovery_store
import review_api
from eligibility import location_decision, clearance_decision, outside_discovery_window, freshness_key
from grounded_scoring import render_analysis


class EligibilityTests(unittest.TestCase):
    def test_glassdoor_description_uses_stable_listing_id_route(self):
        from glassdoor_scraper import description_url
        source = 'https://www.glassdoor.com/job-listing/example-JV_TEST.htm?jl=1010255563234'
        self.assertEqual(description_url(source),
                         'https://www.glassdoor.com/job-listing/j?jl=1010255563234')

    def test_browser_failure_is_measured(self):
        import asyncio, browser_scraper
        from unittest.mock import AsyncMock
        with patch.object(browser_scraper, '_create_browser', new=AsyncMock(side_effect=RuntimeError('test'))):
            self.assertEqual(asyncio.run(browser_scraper._scrape_all()), [])
        self.assertEqual(browser_scraper.LAST_HEALTH['status'], 'error')
        self.assertTrue(browser_scraper.LAST_HEALTH['errors'])

    def test_remote_countries_and_unknowns(self):
        for location, expected in [('Remote, UK only','rejected'), ('Remote, Canada','rejected'),
                ('Remote, US','eligible'), ('Austin, TX','eligible'), ('Remote, India','eligible'),
                ('Bengaluru','eligible'), ('Remote','needs_review'), ('','needs_review'),
                ('Remote worldwide','eligible'), ('US / UK','needs_review')]:
            with self.subTest(location=location):
                self.assertEqual(location_decision(location)[0], expected)
        self.assertEqual(location_decision('Remote, US', 'Applicants must reside in Canada.')[0], 'rejected')
        self.assertEqual(location_decision('Ontario, CA, US', '**Location: Canada**')[0], 'rejected')
        self.assertEqual(location_decision('Albany, NY', 'Must currently reside in the Albany, NY area.')[0], 'needs_review')
        self.assertEqual(location_decision(
            'Kansas City, MO',
            'Remote work is available. However, our preference is for this role to be based in Kansas City.'
        )[0], 'eligible')
        self.assertEqual(location_decision(
            'Eagan, MN', 'ImageTrend is a software company based in Eagan, MN.'
        )[0], 'eligible')
        self.assertEqual(location_decision(
            'Remote, US', 'Candidates must live in the United States.'
        )[0], 'eligible')

    def test_clearance_negation_preferences_and_actual_requirements(self):
        for text, expected in [('No security clearance required.','eligible'),
            ('No US citizenship required.','eligible'),
            ('Minimum Clearance Required: None Clearance Level Must Be Able to Obtain: None','eligible'),
            ('Minimum Clearance Required: None. US citizenship required.','rejected'),
            ('Minimum Clearance Required: Secret','rejected'),
            ('Candidates do not need a security clearance.','eligible'),
            ('Security clearance is not required.','eligible'),
            ('U.S. citizenship is not required.','eligible'),
            ('Security clearance preferred.','eligible'),
            ('We hire regardless of citizenship.','eligible'),
            ('Active secret clearance required.','rejected'),
            ('Must be a U.S. citizen.','rejected'),
            ('No security clearance required, but US citizenship is required.','rejected'),
            ('Security clearance.','needs_review')]:
            with self.subTest(text=text):
                self.assertEqual(clearance_decision(text)[0], expected)

    def test_seven_day_window_and_unknown_dates(self):
        now = datetime(2026,9,11,18,tzinfo=timezone.utc)
        for days, excluded in [(1,False),(6,False),(7,False),(8,True)]:
            job = {'publishedAt':(now-timedelta(days=days)).date().isoformat()}
            self.assertEqual(outside_discovery_window(job, now), excluded)
        self.assertFalse(outside_discovery_window({'publishedAt':'Unknown'}, now))
        self.assertGreater(freshness_key({'publishedAt':'2026-09-11'}), freshness_key({'publishedAt':'2026-09-05'}))


class EvidenceTests(unittest.TestCase):
    def test_scoring_transport_uses_native_context_and_schema(self):
        import job_matcher as matcher
        from unittest.mock import Mock
        response = Mock()
        response.json.return_value = {'message': {'content': '{"score":50}'}}
        with patch.object(matcher, 'check_ollama_endpoints'), patch.object(matcher, 'ACTIVE_CHAT_ENDPOINT', ('http://example.test', 'test-model')), patch.object(matcher.http_session, 'post', return_value=response) as post:
            self.assertEqual(matcher.get_ollama_response('long profile', json_mode=True), '{"score":50}')
            self.assertEqual(post.call_args.args[0], 'http://example.test/api/chat')
            payload = post.call_args.kwargs['json']
            self.assertGreaterEqual(payload['options']['num_ctx'], 16384)
            self.assertIn('matched_skills', payload['format']['required'])
            self.assertFalse(payload['stream'])

    def test_pdf_whitespace_is_accepted_but_changed_words_are_not(self):
        from grounded_scoring import verified_excerpt
        original = 'Home lab project: monitor\nSIEM alerts with Wazuh.'
        self.assertEqual(verified_excerpt('Home lab project: monitor SIEM alerts with Wazuh.', original), original)
        self.assertIsNone(verified_excerpt('Production project: monitor SIEM alerts with Wazuh.', original))

    def test_scheduled_scoring_requests_json_mode(self):
        import job_matcher as matcher
        response = json.dumps({'score':50, 'missing_requirements':[{'job_quote':'Required: professional SIEM experience.'}]})
        with patch.object(matcher, 'get_ollama_response', return_value=response) as ai:
            result = matcher.analyze_job('Analyst','Example','Required: professional SIEM experience.','US','Lab projects')
            self.assertIn('50/100', result)
            self.assertTrue(ai.call_args.kwargs['json_mode'])

    def test_scheduled_scoring_retries_failed_evidence_validation(self):
        import job_matcher as matcher
        valid = json.dumps({
            'score': 60,
            'matched_skills': [{
                'skill': 'SIEM monitoring',
                'job_quote': 'Monitor SIEM alerts daily.',
                'profile_quote': 'Home lab monitored SIEM alerts daily.',
            }],
            'missing_requirements': [],
        })
        with patch.object(matcher, 'get_ollama_response', side_effect=['{}', valid]) as ai:
            result = matcher.analyze_job(
                'SOC Analyst', 'Example', 'Monitor SIEM alerts daily.', 'US',
                'Home lab monitored SIEM alerts daily.',
            )
        self.assertIn('60/100', result)
        self.assertEqual(ai.call_count, 2)

    def test_verified_quotes_only_and_experience_cap(self):
        desc = 'Monitor SIEM alerts daily. Required: 3 years of professional experience.'
        profile = 'Home lab project: monitor SIEM alerts with Wazuh.'
        data = {'score':92, 'matched_skills':[{'job_quote':'Monitor SIEM alerts daily.', 'profile_quote':profile}],
                'missing_requirements':[{'job_quote':'Required: 3 years of professional experience.'}, {'job_quote':'Fabricated requirement of many years.'}]}
        output = render_analysis(json.dumps(data), desc, profile)
        self.assertIn('35/100',output)
        self.assertIn('Home lab project:',output)
        self.assertNotIn('Fabricated',output)
        self.assertIn('Required: 3 years',output)

    def test_invalid_output_never_becomes_match(self):
        for raw in ['Match score: 99/100', '{}', '{"score":99,"matched_skills":[]}',
                    '{"score":true}', '{"score":1000}']:
            self.assertIn('Analysis unavailable',render_analysis(raw,'Description','Profile'))
        raw = json.dumps({'score':95,'matched_skills':[{'job_quote':'Monitor SIEM alerts daily.', 'profile_quote':'Invented professional experience.'}]})
        self.assertIn('Analysis unavailable',render_analysis(raw,'Monitor SIEM alerts daily.','Home lab only'))

    def test_policy_context_is_not_resume_evidence(self):
        quote = 'Candidate has many cybersecurity certifications.'
        raw = json.dumps({'score':95, 'matched_skills':[{'job_quote':quote,'profile_quote':quote}]})
        profile = 'Actual resume\nCANDIDATE POLICY CONTEXT (not resume evidence):\n' + quote
        self.assertIn('Analysis unavailable',render_analysis(raw,quote,profile))


class ReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name,'jobs.db')
        self.patches = [patch.object(matcher,'DB_PATH',self.path), patch.object(matcher,'application_store',None)]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        self.conn = matcher.init_db()
        self.addCleanup(self.conn.close)
        self.job = dict(jobTitle='SOC Analyst', companyName='Example', jobUrl='https://example.test/1',
            location='Remote', jobDescription='Entry-level SOC analyst monitoring SIEM alerts.', _source='LinkedIn', publishedAt='2026-09-11')
        self.analyze = lambda *args: 'Match score: 85/100'
        app = Flask(__name__)
        def get_db():
            conn = sqlite3.connect(self.path); conn.row_factory = sqlite3.Row; return conn
        review_api.register(app,get_db,lambda c:'Home lab profile',self.analyze,self.path)
        self.client = app.test_client()

    def save_review(self):
        with patch.object(matcher,'analyze_job') as ai:
            result = matcher.evaluate_candidate_job(self.job,'candidate_a','Profile')
            ai.assert_not_called()
        self.assertEqual(matcher.persist_candidate_result(self.conn,self.job,'candidate_a',result),'needs_review')

    def test_review_restore_rescore_preserves_user_state(self):
        self.save_review()
        queue = self.client.get('/api/review').get_json()
        self.assertEqual(queue['total'],1)
        body = {'candidate':'candidate_a','job_url':self.job['jobUrl']}
        self.assertEqual(self.client.post('/api/review/restore',json=body).status_code,200)
        self.conn.execute("UPDATE candidate_jobs SET applied=1,starred=1,notion_page_id='keep'")
        self.conn.commit()
        response = self.client.post('/api/review/rescore',json=body)
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['outcome'],'matched')
        row = self.conn.execute('SELECT applied,starred,notion_page_id,match_score FROM candidate_jobs').fetchone()
        self.assertEqual(row,(1,1,'keep',85))
        self.assertEqual(self.conn.execute('SELECT override_eligibility FROM job_candidate_results').fetchone()[0],1)

    def test_rejects_invalid_candidate_and_missing_item(self):
        self.assertEqual(self.client.post('/api/review/restore',json={'candidate':'invalid','job_url':'x'}).status_code,400)
        self.assertEqual(self.client.post('/api/review/restore',json={'candidate':'candidate_a','job_url':'x'}).status_code,404)

    def test_rejected_job_is_saved_and_rescore_keeps_filter(self):
        self.job['location']='Remote, UK only'
        result = matcher.evaluate_candidate_job(self.job,'candidate_a','Profile')
        self.assertEqual(matcher.persist_candidate_result(self.conn,self.job,'candidate_a',result),'rejected')
        self.assertEqual(self.client.get('/api/review?outcome=rejected').get_json()['total'],1)
        body={'candidate':'candidate_a','job_url':self.job['jobUrl']}
        self.assertEqual(self.client.post('/api/review/rescore',json=body).get_json()['outcome'],'rejected')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM jobs_history').fetchone()[0],0)

    def test_source_counts_unique_matches_and_historical_interviews(self):
        run = discovery_store.record_run(self.conn,'LinkedIn',[self.job], '2026-09-11T12:00:00+00:00')
        discovery_store.record_run(self.conn,'LinkedIn',[self.job], '2026-09-11T12:00:00+00:00')
        discovery_store.record_match(self.conn,self.job,'candidate_a')
        discovery_store.record_match(self.conn,self.job,'candidate_b')
        self.conn.executescript('''CREATE TABLE applications(application_id TEXT,source TEXT,status TEXT,applied_at TEXT);
            CREATE TABLE application_events(application_id TEXT,status_to TEXT,event_type TEXT);
            INSERT INTO applications VALUES('1','LinkedIn','rejected','2026-09-01');
            INSERT INTO application_events VALUES('1','interviewing','status_changed');
            INSERT INTO application_events VALUES('1','interviewing','email_interviewing');''')
        data=self.client.get('/api/source-health').get_json()
        row=next(r for r in data['items'] if r['source']=='LinkedIn')
        self.assertEqual((row['unique_matches'],row['latest_unique_matches'],row['applications'],row['interviews']),(1,1,1,1))
        self.assertEqual(row['interview_rate'],100)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM source_runs').fetchone()[0],1)

    def test_date_backfill_uses_source_dates_not_legacy_fetch_times(self):
        matcher.ensure_job_in_history(self.conn, self.job['jobUrl'], self.job)
        self.save_review()
        self.assertEqual(discovery_store.backfill_publication_dates(self.conn), 1)
        self.assertTrue(self.conn.execute('SELECT published_at FROM jobs_history').fetchone()[0].startswith('2026-09-11'))
        self.conn.execute("UPDATE jobs_history SET published_at=NULL,source='Glassdoor'")
        self.conn.commit()
        self.assertEqual(discovery_store.backfill_publication_dates(self.conn), 0)
        self.assertIsNone(self.conn.execute('SELECT published_at FROM jobs_history').fetchone()[0])

    def test_failures_do_not_overwrite_last_success(self):
        discovery_store.record_run(self.conn,'LinkedIn',[], '2026-09-10T12:00:00+00:00',status='success')
        discovery_store.record_run(self.conn,'LinkedIn',[], '2026-09-11T12:00:00+00:00',errors=['Blocked page'])
        row=next(r for r in self.client.get('/api/source-health').get_json()['items'] if r['source']=='LinkedIn')
        self.assertEqual(row['status'],'error')
        self.assertEqual(row['last_success'],'2026-09-10T12:00:00+00:00')
        self.assertEqual(row['errors'],['Blocked page'])

if __name__=='__main__': unittest.main()
