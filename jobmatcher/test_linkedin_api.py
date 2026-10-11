import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from linkedin_api import ACTOR, Store, create_app
from linkedin_local_scraper import normalize_input, scrape, UpstreamError


def card(job_id):
    return f'''<div class="base-card"><h3>Security Analyst</h3><h4>Example Inc</h4>
        <a href="https://www.linkedin.com/jobs/view/security-analyst-{job_id}?trackingId=x">Job</a>
        <span class="job-search-card__location">Chicago, IL</span><time datetime="2026-10-09">Today</time></div>'''


class FakeLinkedIn:
    def __init__(self, pages):
        self.pages, self.calls = iter(pages), []

    def get(self, path, params=None):
        self.calls.append((path, params))
        value = next(self.pages)
        if isinstance(value, Exception):
            raise value
        return value


class ScraperTests(unittest.TestCase):
    def test_pagination_deduplication_and_global_limit(self):
        client = FakeLinkedIn([card(100), card(100) + card(200), card(300) + card(400)])
        config = normalize_input({'keyword': ['SOC'], 'limit': 3, 'includeDescriptions': False})
        jobs, errors = scrape(config, client=client)
        self.assertFalse(errors)
        self.assertEqual([x['id'] for x in jobs], ['100', '200', '300'])
        self.assertEqual([x[1]['start'] for x in client.calls], [0, 1, 3])
        self.assertEqual(jobs[0]['jobUrl'], 'https://www.linkedin.com/jobs/view/100/')

    def test_details_and_employment_type(self):
        client = FakeLinkedIn([card(100), '''<div class="show-more-less-html__markup">Analyze alerts.</div>
            <li class="description__job-criteria-item"><h3>Employment type</h3>
            <span class="description__job-criteria-text">Full-time</span></li>'''])
        jobs, errors = scrape(normalize_input({'keyword': ['SOC'], 'limit': 1}), client=client)
        self.assertEqual(jobs[0]['jobDescription'], 'Analyze alerts.')
        self.assertEqual(jobs[0]['contractType'], 'Full-time')
        self.assertFalse(errors)

    def test_block_retains_partial_results_and_stops_requests(self):
        client = FakeLinkedIn([card(100) + card(200), UpstreamError('LinkedIn returned HTTP 429')])
        jobs, errors = scrape(normalize_input({'keyword': ['SOC']}), client=client)
        self.assertEqual(len(jobs), 1)
        self.assertIn('429', errors[0])
        self.assertEqual(len(client.calls), 2)

    def test_login_page_is_error_not_empty_success(self):
        _, errors = scrape(normalize_input({'keyword': ['SOC']}), client=FakeLinkedIn(['<html>Sign in</html>']))
        self.assertTrue(errors)

    def test_filters_and_keyword_location_product(self):
        config = normalize_input({'keyword': ['SOC', 'GRC'], 'locations': ['Chicago', 'Boston'],
            'experienceLevel': ['entry-level', 'associate'], 'workType': ['remote'], 'publishedAt': 'r604800'})
        self.assertEqual(len(config['searches']), 4)
        self.assertEqual(config['searches'][0]['f_E'], '2,3')
        self.assertEqual(config['searches'][0]['f_WT'], '2')

    def test_invalid_or_unsupported_inputs_fail_before_fetch(self):
        for body in [None, {}, {'keyword': 'SOC'}, {'keyword': ['SOC'], 'limit': True},
                     {'keyword': ['SOC'], 'limit': 1001}, {'keyword': ['SOC'], 'proxyConfiguration': {}},
                     {'startUrls': ['https://127.0.0.1/jobs/search/']},
                     {'startUrls': ['https://www.linkedin.com@localhost/jobs/search/']},
                     {'startUrls': ['http://www.linkedin.com/jobs/search/']},
                     {'startUrls': ['https://www.linkedin.com:444/jobs/search/']},
                     {'startUrls': ['https://www.linkedin.com/feed/']}]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                normalize_input(body)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'runs.db')
        self.client = create_app(self.store, 'test-token').test_client()
        self.headers = {'Authorization': 'Bearer test-token'}

    def tearDown(self):
        self.temp.cleanup()

    def test_auth_and_invalid_json_do_not_enqueue(self):
        self.assertEqual(self.client.post('/v1/jobs/search', json={'keyword': ['SOC']}).status_code, 401)
        self.assertEqual(self.client.post('/v1/jobs/search', json=[], headers=self.headers).status_code, 400)
        self.assertEqual(self.client.get('/health').status_code, 200)

    def test_apify_routes_persist_and_paginate_results(self):
        response = self.client.post(f'/v2/acts/{ACTOR}/runs', json={'keyword': ['SOC']}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        run = response.json['data']
        self.store.progress(run['id'], [{'id': '1'}, {'id': '2'}, {'id': '3'}], [])
        self.store.finish(run['id'], 'SUCCEEDED')
        self.assertEqual(Store(self.store.path).get(run['id'])['status'], 'SUCCEEDED')
        response = self.client.get(f"/v2/datasets/{run['defaultDatasetId']}/items?offset=1&limit=1", headers=self.headers)
        self.assertEqual(response.json, [{'id': '2'}])
        self.assertEqual(response.headers['X-Apify-Pagination-Total'], '3')
        self.assertEqual(response.headers['X-Run-Status'], 'SUCCEEDED')
        response = self.client.get(f"/v2/actor-runs/{run['id']}", headers=self.headers)
        self.assertEqual(response.json['data']['usageTotalUsd'], 0)

    def test_failed_runs_expose_partial_dataset(self):
        row = self.store.create({'keyword': ['SOC']}, 30)
        self.store.progress(row['id'], [{'id': '1'}], ['HTTP 429'])
        self.store.finish(row['id'], 'FAILED')
        response = self.client.get('/v1/runs/' + row['id'], headers=self.headers)
        self.assertTrue(response.json['data']['partial'])
        self.assertEqual(response.json['data']['status'], 'FAILED')

    def test_queue_limit_and_missing_run(self):
        for _ in range(10):
            self.store.create({'keyword': ['SOC']}, 30)
        response = self.client.post('/v1/jobs/search', json={'keyword': ['SOC']}, headers=self.headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.client.get('/v1/runs/absent', headers=self.headers).status_code, 404)

    def test_query_validation_happens_before_enqueue(self):
        for query in ['timeout=nope', 'waitForFinish=-1', 'maxItems=0', 'proxy=other']:
            response = self.client.post('/v1/jobs/search?' + query, json={'keyword': ['SOC']}, headers=self.headers)
            self.assertEqual(response.status_code, 400)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)


class AdapterTests(unittest.TestCase):
    def test_jobmatcher_reads_partial_results_and_reports_errors(self):
        import linkedin_api_client as adapter
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value.json.return_value = {'data': {'id': 'run1', 'status': 'READY'}}
        poll, dataset = MagicMock(), MagicMock()
        poll.json.return_value = {'data': {'id': 'run1', 'defaultDatasetId': 'run1',
                                          'status': 'FAILED', 'errors': ['LinkedIn HTTP 429']}}
        dataset.json.return_value = [{'id': '123', 'jobTitle': 'SOC Analyst'}]
        session.get.side_effect = [poll, dataset]
        with patch.dict('os.environ', {'LINKEDIN_API_URL': 'http://linkedin-api:5556'}), \
                patch.object(Path, 'read_text', return_value='local-token'), \
                patch.object(adapter.requests, 'Session', return_value=session):
            jobs = adapter.scrape_jobs(['https://www.linkedin.com/jobs/search/?keywords=SOC'])
        self.assertEqual(jobs[0]['id'], '123')
        self.assertEqual(adapter.LAST_HEALTH['status'], 'partial')
        self.assertIn('429', adapter.LAST_HEALTH['errors'][0])
        self.assertTrue(session.post.call_args.args[0].startswith('http://linkedin-api:5556/'))


if __name__ == '__main__':
    unittest.main()
