"""Regression tests for the August 13 all-jobs-rejected failure."""
import copy
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch, mock_open

import job_matcher as matcher
from matching_rules import required_experience_years, candidate_matches_track


class MatchingRegressionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('fcntl.flock'))
        temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(matcher, 'DB_PATH', os.path.join(temp, 'jobs.db')))
        self.stack.enter_context(patch.object(matcher, 'application_store', None))
        self.job = dict(jobTitle='Information Security Analyst', companyName='Example',
                        jobDescription='Entry-level monitoring and GRC compliance work. Training is provided.',
                        jobUrl='https://example.test/job/1', location='Austin, TX',
                        publishedAt=datetime.now(timezone.utc).isoformat(),
                        postedTime='Today', _source='LinkedIn', contractType='Full-time')
        self.conn = matcher.init_db()
        self.addCleanup(self.conn.close)

    def test_normal_run_scores_without_embeddings_and_keeps_other_candidate_match(self):
        self.stack.enter_context(patch.object(matcher, 'extract_resumes', return_value={
            'candidate_a': {'resume': 'profile'}, 'candidate_b': {'resume': 'profile'}}))
        self.stack.enter_context(patch.object(matcher, 'scrape_jobs', return_value=[self.job]))
        self.stack.enter_context(patch.object(matcher, 'summarize_resumes', return_value='Candidate'))
        embed = self.stack.enter_context(patch.object(matcher, 'get_ollama_embedding',
                                                      side_effect=AssertionError('Embedding gate called')))
        analyze = self.stack.enter_context(patch.object(matcher, 'analyze_job',
                                                        side_effect=['Match score: 85/100', 'Match score: 35/100']))
        self.stack.enter_context(patch.object(matcher, 'send_telegram_msg'))
        self.stack.enter_context(patch.object(matcher, 'send_telegram_doc'))
        self.stack.enter_context(patch('builtins.open', mock_open()))
        matcher.run_job_matcher()
        embed.assert_not_called()
        self.assertEqual(analyze.call_count, 2)
        self.assertEqual(self.conn.execute('SELECT candidate,match_score FROM jobs_history').fetchone(), ('candidate_b', 85))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM job_candidate_results').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM processed_jobs').fetchone()[0], 1)

    def test_user_state_and_existing_analysis_are_preserved(self):
        matcher.ensure_job_in_history(self.conn, self.job['jobUrl'], self.job)
        self.conn.execute("UPDATE jobs_history SET candidate='candidate_a',match_score=60,applied=1,starred=1,skipped=1,notion_page_id='existing'")
        self.conn.commit()
        with patch.object(matcher, 'analyze_job', return_value='Match score: 90/100'):
            result = matcher.evaluate_candidate_job(self.job, 'candidate_b', 'profile')
        matcher.persist_candidate_result(self.conn, self.job, 'candidate_b', result)
        self.assertEqual(self.conn.execute('SELECT candidate,match_score,applied,starred,skipped,notion_page_id FROM jobs_history').fetchone(),
                         ('candidate_a', 60, 1, 1, 1, 'existing'))

    def test_ai_failure_is_audited_for_retry_without_inventing_a_match(self):
        with patch.object(matcher, 'analyze_job', return_value='Analysis unavailable — AI request failed.'):
            result = matcher.evaluate_candidate_job(self.job, 'candidate_a', 'profile')
        self.assertEqual(matcher.persist_candidate_result(self.conn, self.job, 'candidate_a', result), 'pending_analysis')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs_history').fetchone()[0], 0)
        self.assertIn('Entry-level', self.conn.execute('SELECT job_json FROM job_candidate_results').fetchone()[0])

    def test_selected_chat_endpoint_is_preserved_without_embeddings(self):
        endpoint = ('http://127.0.0.1:11434', 'gemma4:e2b')
        with patch.object(matcher, 'ACTIVE_CHAT_ENDPOINT', endpoint), \
             patch.object(matcher, 'ACTIVE_EMBED_ENDPOINT', None), \
             patch.object(matcher.http_session, 'get') as get, \
             patch.object(matcher.http_session, 'post') as post:
            matcher.check_ollama_endpoints()
            self.assertEqual(matcher.ACTIVE_CHAT_ENDPOINT, endpoint)
            get.assert_not_called()
            post.assert_not_called()

    def test_processed_url_with_pending_candidate_resumes_without_rescraping(self):
        result = {'link': self.job['jobUrl'], 'analysis': 'Match score: 85/100', 'eligibility': 'eligible'}
        matcher.persist_candidate_result(self.conn, self.job, 'candidate_a', result)
        matcher.persist_candidate_result(self.conn, self.job, 'candidate_b',
            dict(result, analysis='Analysis unavailable — retry scoring.'))
        matcher.mark_processed(self.conn, self.job['jobUrl'])
        with patch.object(matcher, 'extract_resumes', return_value={
                'candidate_a': {'resume': 'profile'}, 'candidate_b': {'resume': 'profile'}}), \
             patch.object(matcher, 'scrape_jobs', return_value=[]), \
             patch.object(matcher, 'summarize_resumes', return_value='Candidate'), \
             patch.object(matcher, 'analyze_job', return_value='Match score: 75/100') as analyze, \
             patch.object(matcher, 'send_telegram_msg'), patch.object(matcher, 'send_telegram_doc'), \
             patch('builtins.open', mock_open()):
            matcher.run_job_matcher()
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT outcome,match_score FROM job_candidate_results WHERE candidate='candidate_b'").fetchone(), ('matched', 75))

    def test_local_scoring_preserves_prompt_and_limits_context(self):
        from grounded_scoring import scoring_payload
        prompt = 'resume evidence ' * 500
        payload = scoring_payload('gemma4:e2b', prompt)
        self.assertEqual(payload['messages'][0]['content'], prompt)
        self.assertFalse(payload['think'])
        self.assertEqual(payload['options']['num_ctx'], 8192)

    def test_experience_and_clearance_still_reject_without_ai(self):
        job = dict(self.job, jobDescription='Required: 3+ years of professional experience.')
        with patch.object(matcher, 'analyze_job') as analyze:
            result = matcher.evaluate_candidate_job(job, 'candidate_a', 'profile')
            self.assertIn('requires 3+', result['analysis'])
            analyze.assert_not_called()
        job['jobDescription'] = 'Active secret clearance required.'
        self.assertFalse(matcher.hard_filter_job(job)[0])

    def test_experience_ranges_preferences_and_company_age(self):
        cases = {
            'Our company has served clients for 50 years. Training provided.': None,
            'Required: 1–3 years of experience.': 1,
            'Experience: 0-2 years': 0,
            '2+ years of experience preferred.': None,
            'Preferred qualifications: 3 years of experience.': None,
            'Minimum 2 years of professional experience.': 2,
            'Required: 3+ years of IT experience.': 3,
            'Minimum Requirements: Two (2) years of experience in GRC.': 2,
            r'Minimum Requirements: Two (2\) years of experience in GRC.': 2,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(required_experience_years(text), expected)

    def test_support_track_accepts_equivalent_job_titles(self):
        for title in ('Service Desk Technician', 'Information Technology Support Engineer',
                      'IAM Analyst', 'Vulnerability Management Analyst', 'NOC Analyst',
                      'Cloud Support Associate', 'Technology Controls Analyst'):
            job = dict(self.job, jobTitle=title, jobDescription='Provide entry-level desktop support.')
            self.assertTrue(candidate_matches_track(job, 'candidate_a'))
            if title not in ('IAM Analyst', 'Technology Controls Analyst'):
                self.assertFalse(candidate_matches_track(job, 'candidate_b'))

    def test_generic_titles_and_substrings_do_not_become_target_roles(self):
        software = dict(self.job, jobTitle='Entry Level Software Engineer',
                        jobDescription='Develop Python software on Linux and Windows.')
        administrative = dict(self.job, jobTitle='Administrative Analyst',
                              jobDescription='Track contract deliverables and administrative requirements.')
        self.assertFalse(candidate_matches_track(software, 'candidate_a'))
        self.assertFalse(candidate_matches_track(administrative, 'candidate_b'))

    def test_description_requires_multiple_domain_signals(self):
        job = dict(self.job, jobTitle='Operations Analyst',
                   jobDescription='Maintain compliance with office procedures.')
        self.assertFalse(candidate_matches_track(job, 'candidate_b'))
        job['jobDescription'] = 'Perform risk management and vendor risk assessments.'
        self.assertFalse(candidate_matches_track(job, 'candidate_b'))
        job['jobDescription'] += ' Assess information security controls using NIST.'
        self.assertTrue(candidate_matches_track(job, 'candidate_b'))

    def test_financial_trade_and_clerical_risk_roles_are_not_cyber_grc(self):
        for title in ('Credit Risk Analyst', 'Trade Compliance Analyst',
                      'Part-Time Administrative Assistant - NASA GRC'):
            job = dict(self.job, jobTitle=title,
                       jobDescription='Support risk management and compliance reporting.')
            self.assertFalse(candidate_matches_track(job, 'candidate_a'))
            self.assertFalse(candidate_matches_track(job, 'candidate_b'))

        file_review = dict(self.job, jobTitle='File Review Analyst',
                           jobDescription='Review bank lending compliance and risk management.')
        self.assertFalse(candidate_matches_track(file_review, 'candidate_a'))

    def test_seniority_hidden_in_description_is_rejected(self):
        job = dict(self.job, jobTitle='GRC Analyst',
                   jobDescription='The Governance and Compliance Sr. Analyst will report to the manager.')
        self.assertFalse(matcher.hard_filter_job(job, 'candidate_b')[0])

        junior = dict(self.job, jobTitle='IT Support Engineer',
                      jobDescription='Escalated tickets are assigned to a senior engineer who will assist.')
        self.assertTrue(matcher.hard_filter_job(junior, 'candidate_a')[0])

    def test_numbered_midlevel_titles_are_rejected(self):
        for title in ('Security Engineer II', 'IT Specialist III',
                      'Tier 3 Service Desk Engineer'):
            job = dict(self.job, jobTitle=title,
                       jobDescription='Security operations role with training provided.')
            self.assertFalse(matcher.hard_filter_job(job, 'candidate_a')[0])
        entry = dict(self.job, jobTitle='Cyber Threat Intelligence Analyst I')
        self.assertTrue(matcher.hard_filter_job(entry, 'candidate_a')[0])

    def test_failed_rescore_removes_only_unmanaged_dashboard_row(self):
        matcher.ensure_job_in_history(self.conn, self.job['jobUrl'], self.job)
        self.conn.execute("UPDATE jobs_history SET candidate='candidate_a',match_score=80")
        self.conn.commit()
        result = dict(title=self.job['jobTitle'], company=self.job['companyName'],
                      link=self.job['jobUrl'], location=self.job['location'], posted='Today',
                      contract='Full-time', source='LinkedIn',
                      analysis='❌ Skipped via hard filter: outside target track.',
                      eligibility='rejected', reason='outside target track')
        self.assertEqual(matcher.persist_candidate_result(self.conn, self.job, 'candidate_a', result), 'rejected')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM jobs_history').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
