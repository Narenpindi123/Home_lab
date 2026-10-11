import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'job-pipeline'))
import application_store as store
import discovery_store
import gap_analysis
import listing_verification as listing
import resume_export
import requests


class ListingTests(unittest.TestCase):
    def test_open_closed_challenge_and_redirect(self):
        for text, title, url, expected in [
            ('SOC Analyst. Apply now. ' + 'Investigate alerts. ' * 30, 'SOC Analyst', 'https://example.org/jobs/1', 'open'),
            ('This job is closed', 'SOC Analyst', 'https://example.org/jobs/1', 'closed'),
            ('Verify you are human', 'SOC Analyst', 'https://example.org/jobs/1', 'unconfirmed'),
            ('Browse our careers', 'Careers', 'https://example.org/careers', 'unconfirmed'),
            ('SOC Analyst. ' * 30, 'SOC Analyst', 'https://example.org/jobs/1', 'unconfirmed'),
        ]:
            with self.subTest(expected=expected):
                result = listing.check('https://example.org/jobs/1', 'SOC Analyst', lambda _: (url, title, text))
                self.assertEqual(result['state'], expected)

    def test_http_and_fetch_errors(self):
        for code, expected in [(404, 'closed'), (410, 'closed'), (403, 'unconfirmed'), (503, 'unconfirmed')]:
            response = requests.Response()
            response.status_code = code
            def fetch(_):
                raise requests.HTTPError(response=response)
            self.assertEqual(listing.check('https://example.org/jobs/1', 'SOC Analyst', fetch)['state'], expected)
        def fetch(_):
            raise requests.Timeout()
        self.assertEqual(listing.check('https://example.org/jobs/1', 'SOC Analyst', fetch)['state'], 'unconfirmed')

    def test_private_destination_is_blocked(self):
        self.assertEqual(listing.check('http://127.0.0.1/', 'SOC Analyst')['state'], 'unconfirmed')

    def test_checks_are_cached_and_can_be_refreshed(self):
        conn = sqlite3.connect(':memory:')
        with patch.object(listing, 'check', return_value={'state': 'open', 'checked_at': datetime.now(timezone.utc).isoformat()}) as check:
            listing.verify(conn, 'https://example.org/1', 'SOC Analyst')
            listing.verify(conn, 'https://example.org/1', 'SOC Analyst')
            self.assertEqual(check.call_count, 1)
            listing.verify(conn, 'https://example.org/1', 'SOC Analyst', force=True)
            self.assertEqual(check.call_count, 2)
        conn.close()


class ResumeTests(unittest.TestCase):
    def test_complete_resume_and_wrapped_bullet_replacement(self):
        text = 'Name\nEducation\nMS Cybersecurity\nProjects\n• Built a Splunk\n  monitoring lab\nOther experience\n'
        result = resume_export.assemble(text, [{'original': 'Built a Splunk monitoring lab', 'proposed': 'Built a monitoring lab using Splunk'}])
        self.assertIn('MS Cybersecurity', result)
        self.assertIn('Other experience', result)
        self.assertIn('Built a monitoring lab using Splunk', result)
        self.assertNotIn('Splunk\n  monitoring', result)

    def test_stale_duplicate_and_overlapping_bullets_fail(self):
        for text, bullets in [('Nothing', [{'original': 'Built a lab', 'proposed': 'Built lab'}]),
                             ('Built a lab\nBuilt a lab', [{'original': 'Built a lab', 'proposed': 'Built lab'}]),
                             ('Built a lab', [{'original': 'Built a lab', 'proposed': 'Built lab'}] * 2)]:
            with self.assertRaises(ValueError):
                resume_export.assemble(text, bullets)

    def test_new_claims_are_rejected(self):
        self.assertTrue(resume_export.faithful_wording('Built a Splunk monitoring lab', 'Built a monitoring lab using Splunk'))
        self.assertFalse(resume_export.faithful_wording('Built a Splunk monitoring lab', 'Deployed Splunk in production'))
        self.assertFalse(resume_export.faithful_wording('Analyzed 10 alerts', 'Analyzed 100 alerts'))

    def test_pdf_contains_complete_readable_unicode_resume(self):
        import fitz
        text = 'Candidate A\nEducation\nMS Cybersecurity\nExperience\n• Built a monitoring lab\nProjects\nLinux and Splunk'
        pdf = resume_export.pdf_bytes(text)
        with fitz.open(stream=pdf, filetype='pdf') as doc:
            recovered = '\n'.join(page.get_text() for page in doc)
            for section in ('Candidate A', 'Education', 'MS Cybersecurity', 'Experience', 'Projects', 'Linux and Splunk'):
                self.assertIn(section.lower(), recovered.lower())


    def test_networking_format_letter_serif_rules_and_aligned_dates(self):
        import fitz
        text = 'Candidate Name\nUnited States | candidate@example.org\nEducation\nExample University                  Aug 2024 - Jan 2026\nMS Cybersecurity                    Example City, US\n• GPA: 3.5 / 4.0\nExperience\nExample Company                     Jun 2026 - Present\nSecurity Intern                     Remote\n• Investigated alerts in a lab.'
        with fitz.open(stream=resume_export.pdf_bytes(text), filetype='pdf') as doc:
            self.assertEqual(len(doc), 1)
            page = doc[0]
            self.assertEqual((page.rect.width, page.rect.height), (612, 792))
            spans = [span for block in page.get_text('dict')['blocks'] for line in block.get('lines', []) for span in line['spans']]
            self.assertTrue(all('LiberationSerif' in span['font'] for span in spans))
            heading = next(span for span in spans if span['text'] == 'EDUCATION')
            self.assertIn('Bold', heading['font'])
            self.assertAlmostEqual(heading['size'], 11)
            date = next(span for span in spans if span['text'] == 'Aug 2024 - Jan 2026')
            self.assertAlmostEqual(date['bbox'][2], 576, delta=1)
            self.assertGreaterEqual(len(page.get_drawings()), 2)

    def test_long_resume_flows_without_clipping_or_dropping_content(self):
        import fitz
        text = 'Candidate Name\nProjects\n' + '\n'.join(f'• Example project {i}: ' + 'Supported security investigations. ' * 8 for i in range(60))
        with fitz.open(stream=resume_export.pdf_bytes(text), filetype='pdf') as doc:
            self.assertGreater(len(doc), 1)
            recovered = ''.join(page.get_text() for page in doc)
            for i in range(60):
                self.assertIn(f'Example project {i}:', recovered)
            for page in doc:
                for word in page.get_text('words'):
                    self.assertGreaterEqual(word[0], 35)
                    self.assertLessEqual(word[2], 577)
                    self.assertLessEqual(word[3], 770)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'test.db')
        store.init_schema(self.db)
        self.start = datetime.now(timezone.utc).date()

    def tearDown(self):
        self.temp.cleanup()

    def application(self, url, days=0, score=85, historical=False):
        return store.ensure_application(db_path=self.db, job_url=url, candidate='candidate_a', title='SOC Analyst', source='Employer boards',
                                        match_score=score, status='applied', historical_import=historical,
                                        applied_at=(datetime.combine(self.start, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=days)).isoformat())

    def test_cohort_rates_dont_mix_older_applications_and_keep_interviews(self):
        old = self.application('https://example.org/old', days=-20)
        new = self.application('https://example.org/new')
        for _ in range(2):
            store.record_event(old['application_id'], 'email_interviewing', db_path=self.db)
        store.update_application_status(old['application_id'], 'rejected', db_path=self.db)
        report = store.build_weekly_report(db_path=self.db, week_start=self.start)
        self.assertEqual(report['new_applications'], 1)
        self.assertEqual(report['interviews'], 1)
        self.assertEqual(report['response_rate'], 0)
        self.assertEqual(report['interview_conversion'], 0)
        self.assertEqual(report['by_score_band'][0]['interviewing'], 1)
        self.assertIsNone(report['calibration'][0]['interview_rate'])

    def test_snapshot_survives_rescoring_and_legacy_remains_unknown(self):
        app = self.application('https://example.org/1', score=85)
        store.ensure_application(db_path=self.db, job_url=app['job_url'], candidate='candidate_a', match_score=35)
        legacy = self.application('https://example.org/legacy', historical=True)
        with store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT applied_match_score FROM applications WHERE application_id=?', (app['application_id'],)).fetchone()[0], 85)
            self.assertIsNone(conn.execute('SELECT applied_match_score FROM applications WHERE application_id=?', (legacy['application_id'],)).fetchone()[0])

    def test_first_manual_submission_freezes_score(self):
        app = store.ensure_application(db_path=self.db, job_url='https://example.org/2', match_score=74)
        store.update_application_status(app['application_id'], 'applied', db_path=self.db)
        store.ensure_application(db_path=self.db, job_url=app['job_url'], match_score=90)
        store.update_application_status(app['application_id'], 'applied', db_path=self.db)
        with store.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT applied_match_score FROM applications').fetchone()[0], 74)

    def test_calibration_rates_require_five_resolved_cases(self):
        for i in range(5):
            app = self.application(f'https://example.org/{i}')
            if i < 2:
                store.update_application_status(app['application_id'], 'interviewing', db_path=self.db)
            store.update_application_status(app['application_id'], 'rejected', db_path=self.db)
        self.application('https://example.org/pending')
        withdrawn = self.application('https://example.org/withdrawn')
        store.update_application_status(withdrawn['application_id'], 'withdrawn', db_path=self.db)
        group = store.build_weekly_report(db_path=self.db, week_start=self.start)['calibration'][0]
        self.assertEqual((group['resolved'], group['pending'], group['interview_rate'], group['excluded']), (5, 1, 40, 1))

    def test_migration_is_idempotent_and_does_not_invent_historical_scores(self):
        with store.connect(self.db) as conn:
            conn.execute('CREATE TABLE jobs_history(job_url TEXT,candidate TEXT,match_score INTEGER,applied INTEGER,processed_at TEXT)')
            conn.execute("INSERT INTO jobs_history VALUES ('https://example.org/legacy','candidate_a',99,1,'2026-09-01')")
        store.migrate_jobs_history(self.db)
        store.init_schema(self.db)
        store.migrate_jobs_history(self.db)
        with store.connect(self.db) as conn:
            rows = conn.execute('SELECT applied_match_score FROM applications').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0][0])


class GapTests(unittest.TestCase):
    def test_verified_candidate_track_and_importance_filtering(self):
        conn = sqlite3.connect(':memory:')
        discovery_store.init_schema(conn)
        for i, candidate, title, quote, eligibility in [
            (1, 'candidate_a', 'SOC Analyst', 'Must have Splunk and Linux experience.', 'eligible'),
            (2, 'candidate_a', 'GRC Analyst', 'NIST knowledge is preferred.', 'eligible'),
            (3, 'candidate_b', 'GRC Analyst', 'Must have CISSP certification.', 'eligible'),
            (4, 'candidate_a', 'Senior SOC Analyst', 'Must have CISSP certification.', 'rejected'),
        ]:
            job = {'jobTitle': title, 'jobDescription': quote}
            analysis = '**Requirements to verify — supporting evidence**\n- Job: `' + quote + '`\n**Resume guidance:**'
            conn.execute('INSERT INTO job_candidate_results(job_url,candidate,job_json,analysis,match_score,outcome,eligibility) VALUES (?,?,?,?,40,?,?)',
                         (f'https://example.org/{i}', candidate, json.dumps(job), analysis, 'rejected', eligibility))
        report = gap_analysis.build_report(conn, 'candidate_a', 'x' * 5000 + '\nLinux administration')
        self.assertEqual({g['skill'] for g in report['gaps']}, {'Splunk', 'NIST'})
        self.assertEqual(next(g for g in report['gaps'] if g['skill'] == 'Splunk')['required'], 1)
        self.assertEqual(next(g for g in report['gaps'] if g['skill'] == 'NIST')['preferred'], 1)
        self.assertIn('Linux', report['already_evidenced'])
        conn.execute("UPDATE job_candidate_results SET analysis='**Requirements to verify** - Job: `Must have CISSP certification.`'")
        self.assertEqual(gap_analysis.build_report(conn, 'candidate_a', '')['gaps'], [])
        conn.close()


class ExportAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bootstrap = tempfile.TemporaryDirectory()
        db = str(Path(cls.bootstrap.name) / 'bootstrap.db')
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE jobs_history(job_url TEXT PRIMARY KEY,candidate TEXT,match_score INTEGER,processed_at TEXT)')
        with patch.dict(os.environ, {'DB_PATH': db, 'JOB_PIPELINE_DB': db, 'JOB_PIPELINE_MODULE_DIR': str(Path(store.__file__).parent)}):
            cls.web = importlib.import_module('web_server')

    @classmethod
    def tearDownClass(cls):
        cls.bootstrap.cleanup()

    def test_fallback_extraction_preserves_columns_and_bullet_continuation(self):
        import fitz
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.pdf'
            with fitz.open() as doc:
                page = doc.new_page()
                for point, text in [((36, 40), 'Candidate Name'),
                                    ((36, 70), 'EXPERIENCE'),
                                    ((36, 90), 'Example Company'),
                                    ((420, 90), 'Jun 2026 - Present'),
                                    ((36, 110), '- Built a monitoring lab'),
                                    ((44, 122), 'using Linux and Python.')]:
                    page.insert_text(point, text, fontsize=10)
                doc.save(path)
            for error in (FileNotFoundError(), self.web.subprocess.TimeoutExpired('pdftotext', 20),
                          self.web.subprocess.CalledProcessError(1, 'pdftotext')):
                with self.subTest(error=type(error).__name__), patch.object(self.web.subprocess, 'run', side_effect=error):
                    text = self.web.extract_resume_text_pdftotext(str(path))
                blocks = resume_export._blocks(text)
                self.assertIn(('entry', 'Example Company', 'Jun 2026 - Present'), blocks)
                self.assertIn(('bullet', '- Built a monitoring lab using Linux and Python.', ''), blocks)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / 'test.db')
        store.init_schema(self.db)
        self.app_record = store.ensure_application(db_path=self.db, job_url='https://example.org/1', title='SOC Analyst')
        self.artifact = store.save_artifact(self.app_record['application_id'], 'resume_tailoring',
                                            {'source_resume': 'SOC.pdf', 'resume_text': 'Candidate A\nEducation\nMS Cybersecurity\nProjects\nBuilt a lab'}, db_path=self.db)
        self.patch = patch.multiple(self.web, PIPELINE_DB_PATH=self.db, DB_PATH=self.db)
        self.patch.start()
        self.client = self.web.app.test_client()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_approval_gate_closed_gate_and_durable_idempotent_export(self):
        url = f'/api/artifacts/{self.artifact}/resume-export'
        self.assertEqual(self.client.post(url).status_code, 409)
        store.approve_artifact(self.artifact, db_path=self.db)
        with patch.object(listing, 'verify', return_value={'state': 'closed'}):
            self.assertEqual(self.client.post(url).status_code, 409)
        with patch.object(listing, 'verify', return_value={'state': 'unconfirmed'}):
            first = self.client.post(url)
            self.assertEqual(first.status_code, 200, first.get_json())
            again = self.client.post(url)
            self.assertEqual(first.get_json()['artifact_id'], again.get_json()['artifact_id'])
            pdf = self.client.get(first.get_json()['download_url'])
            self.assertTrue(pdf.data.startswith(b'%PDF'))
            self.assertEqual(pdf.data, self.client.get(first.get_json()['download_url']).data)
        self.assertFalse(any('pdf_base64' in item['content'] for item in store.list_artifacts(self.app_record['application_id'], db_path=self.db)))

    def test_generation_uses_selected_source_rejects_new_claims_and_closed_listing(self):
        row = {'candidate': 'candidate_a', 'title': 'SOC Analyst', 'company': 'Example',
               'job_url': 'https://example.org/1', 'description': 'Analyze alerts'}
        source = 'Name\nEducation\nMS Cybersecurity\nProjects\n• Built a Splunk monitoring lab for security alerts.'
        evidence = [{'accomplishment_id': 1, 'source_resume': 'SOC.pdf', 'original_text': 'Built a Splunk monitoring lab for security alerts.'}]
        with patch.object(self.web, 'WEB_CANDIDATE_RESUMES', {'candidate_a': ['/owned/SOC.pdf']}), \
             patch.object(self.web, 'extract_resume_text_pdftotext', return_value=source), \
             patch.object(self.web, 'seed_accomplishment_bank', return_value=evidence), \
             patch.object(self.web, 'query_job_ai_json', return_value={'bullets': [{'source_id': 1, 'proposed': 'Built a monitoring lab for security alerts using Splunk.'}]}):
            artifact, content = self.web.generate_resume_tailoring(row, self.app_record, 'SOC.pdf', {'state': 'open'})
            self.assertIn('MS Cybersecurity', content['resume_text'])
            self.assertIn('using Splunk', content['resume_text'])
            self.assertEqual(content['listing_verification']['state'], 'open')
            with self.assertRaises(ValueError):
                self.web.generate_resume_tailoring(row, self.app_record, '../other.pdf')
        with patch.object(self.web, 'job_row_by_id', return_value=row), patch.object(listing, 'verify', return_value={'state': 'closed'}):
            self.assertEqual(self.client.post('/api/jobs/123/resume-tailoring', json={'source_resume': 'SOC.pdf'}).status_code, 409)

    def test_evidence_refresh_preserves_old_artifact_ids(self):
        store.add_accomplishments('candidate_a', [{'text': 'Built an older lab for monitoring alerts.'}], source_resume='SOC.pdf', db_path=self.db)
        old_id = store.list_accomplishments('candidate_a', db_path=self.db)[0]['accomplishment_id']
        with patch.object(self.web, 'WEB_CANDIDATE_RESUMES', {'candidate_a': ['/owned/SOC.pdf']}), \
             patch.object(self.web, 'extract_resume_text_pdftotext', return_value='Projects\n• Built a Linux monitoring lab for security alerts.'):
            evidence = self.web.seed_accomplishment_bank('candidate_a', refresh=True)
        self.assertIn(old_id, [item['accomplishment_id'] for item in evidence])
        self.assertTrue(any('Linux' in item['original_text'] for item in evidence))

    def test_new_format_keeps_old_pdf_snapshot_and_creates_new_version(self):
        import base64
        old = store.save_artifact(self.app_record['application_id'], 'resume_export',
                                  {'draft_id': self.artifact, 'pdf_base64': base64.b64encode(b'%PDF old snapshot').decode()},
                                  db_path=self.db, state='approved')
        store.approve_artifact(self.artifact, db_path=self.db)
        with patch.object(listing, 'verify', return_value={'state': 'open'}):
            response = self.client.post(f'/api/artifacts/{self.artifact}/resume-export')
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get_json()['artifact_id'], old)
        self.assertEqual(self.client.get(f'/api/artifacts/{old}/resume.pdf').data, b'%PDF old snapshot')

    def test_legacy_draft_requires_regeneration(self):
        artifact = store.save_artifact(self.app_record['application_id'], 'resume_tailoring', {'bullets': []}, db_path=self.db, state='approved')
        self.assertEqual(self.client.post(f'/api/artifacts/{artifact}/resume-export').status_code, 409)


if __name__ == '__main__':
    unittest.main()
