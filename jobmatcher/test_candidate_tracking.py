import importlib
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

import candidate_tracking
import job_matcher as matcher


class CandidateTrackingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = os.path.join(directory, 'jobs.db')
        self.stack.enter_context(patch.object(matcher, 'DB_PATH', self.path))
        self.stack.enter_context(patch.object(matcher, 'application_store', None))
        self.conn = matcher.init_db()
        self.addCleanup(self.conn.close)
        with patch.dict(os.environ, {'DB_PATH': self.path, 'JOB_PIPELINE_DB': self.path}):
            self.web = importlib.import_module('web_server')
        self.stack.enter_context(patch.object(self.web, 'DB_PATH', self.path))
        self.stack.enter_context(patch.object(self.web, 'application_store', None))
        self.client = self.web.app.test_client()
        self.job = dict(jobUrl='https://example.test/shared', jobTitle='GRC Analyst',
            companyName='Example', jobDescription='Entry-level GRC compliance and security controls.',
            location='Remote, United States', publishedAt=datetime.now(timezone.utc).isoformat(), _source='Indeed')
        for candidate in ('candidate_a', 'candidate_b'):
            matcher.persist_candidate_result(self.conn, self.job, candidate,
                dict(link=self.job['jobUrl'], analysis='Match score: 85/100', eligibility='eligible'))
        self.ids = {c: candidate_tracking.candidate_id(self.job['jobUrl'], c) for c in ('candidate_a','candidate_b')}

    def detail(self, candidate):
        response = self.client.get('/api/jobs/' + self.ids[candidate])
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_individual_actions_and_delete_are_isolated(self):
        rows = self.client.get('/api/jobs').get_json()['jobs']
        self.assertEqual(len({r['id'] for r in rows}), 2)
        for action, field in [('star','starred'), ('applied','applied'), ('skip','skipped')]:
            response = self.client.post('/api/jobs/' + self.ids['candidate_a'] + '/' + action)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertTrue(self.detail('candidate_a')[field])
            self.assertFalse(self.detail('candidate_b')[field])
        self.client.delete('/api/jobs/' + self.ids['candidate_a'])
        self.assertEqual(self.detail('candidate_b')['candidate'], 'candidate_b')
        candidate_tracking.save_result(self.conn,self.job,'candidate_a','Match score: 90/100',90,'matched')
        self.conn.commit()
        self.assertEqual(self.client.get('/api/jobs/' + self.ids['candidate_a']).status_code,404)

    def test_bulk_actions_use_candidate_ids(self):
        for action, field in [('star','starred'), ('applied','applied'), ('skip','skipped')]:
            response = self.client.post('/api/jobs/bulk', json={'action':action,'job_ids':[self.ids['candidate_b']]})
            self.assertEqual(response.status_code,200)
            self.assertTrue(self.detail('candidate_b')[field])
            self.assertFalse(self.detail('candidate_a')[field])
        self.client.post('/api/jobs/bulk',json={'action':'delete','job_ids':[self.ids['candidate_b']]})
        self.assertEqual(self.detail('candidate_a')['candidate'],'candidate_a')

    def test_migration_assigns_legacy_actions_only_to_owner(self):
        self.conn.execute('DELETE FROM candidate_jobs')
        self.conn.execute("DELETE FROM matcher_migrations WHERE name='candidate_tracking_v1'")
        self.conn.execute("UPDATE jobs_history SET candidate='candidate_a',applied=1,starred=1,notion_page_id='old-page'")
        candidate_tracking.init_schema(self.conn)
        self.assertTrue(self.detail('candidate_a')['applied'])
        self.assertFalse(self.detail('candidate_b')['applied'])
        self.assertEqual(self.detail('candidate_b')['notion_page_id'],'')
        self.client.post('/api/jobs/' + self.ids['candidate_a'] + '/applied')
        candidate_tracking.init_schema(self.conn)
        self.assertFalse(self.detail('candidate_a')['applied'])

    def test_rescore_does_not_reset_candidate_state(self):
        self.client.post('/api/jobs/' + self.ids['candidate_b'] + '/star')
        candidate_tracking.save_result(self.conn,self.job,'candidate_b','Match score: 75/100',75,'matched')
        self.conn.commit()
        self.assertTrue(self.detail('candidate_b')['starred'])
        self.assertFalse(self.detail('candidate_a')['starred'])
        self.assertEqual(self.detail('candidate_a')['match_score'],85)

    def test_notion_lookup_does_not_reuse_other_candidate_page(self):
        def page(candidate):
            return {'id':candidate,'properties':{
                'Candidate':{'type':'rich_text','rich_text':[{'plain_text':candidate}]},
                'URL':{'type':'url','url':self.job['jobUrl']}}}
        schema={'Candidate':{'type':'rich_text'},'URL':{'type':'url'}}
        with patch.object(self.web,'notion_query_pages',return_value=[page('candidate_a'),page('candidate_b')]):
            found=self.web.find_existing_notion_job(schema,'GRC Analyst','Example',self.job['jobUrl'],'candidate_b')
        self.assertEqual(found['id'],'candidate_b')

    def test_ready_results_are_saved_before_linkedin_finishes(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        def browser(_):
            started.set()
            if not release.wait(5):
                raise RuntimeError('ready results were blocked by browser')
            finished.set()
            return [dict(self.job,jobUrl='https://example.test/linkedin',_source='LinkedIn')]
        seen = []
        def ready(batch):
            if not batch:
                return
            seen.append(batch[0]['_source'])
            if batch[0]['_source'] == 'Indeed':
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.is_set())
                candidate_tracking.save_result(self.conn,batch[0],'candidate_b','Match score: 80/100',80,'matched')
                self.conn.commit()
                self.assertEqual(self.detail('candidate_b')['match_score'],80)
                release.set()
        with patch.object(matcher.browser_scraper,'scrape_all_jobs',side_effect=browser), \
             patch.object(matcher.local_sources_client,'scrape_source',return_value=([], {'errors': [], 'status': 'success'})), \
             patch.object(matcher,'_load_host_jobs',side_effect=lambda source,conn: [self.job] if source=='Indeed' else []):
            try:
                matcher.scrape_jobs(set(),on_ready=ready)
            finally:
                release.set()
        self.assertEqual(seen,['Indeed','LinkedIn'])
