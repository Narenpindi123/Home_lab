import asyncio
import ast
import concurrent.futures
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import ziprecruiter_scraper as scraper


def page(posting):
    return '<script type="application/ld+json">' + json.dumps(posting) + '</script>'


class ZipRecruiterTests(unittest.TestCase):
    def setUp(self):
        self.url = 'https://www.ziprecruiter.com/c/Example/Job/Analyst?jid=123'
        self.posting = {'@type': 'JobPosting', 'title': 'SOC Analyst', 'url': self.url,
                        'description': '<p>Monitor SIEM alerts and investigate security incidents.</p>',
                        'hiringOrganization': {'name': 'Example'},
                        'jobLocation': {'address': {'addressLocality': 'Chicago',
                                        'addressRegion': 'IL', 'addressCountry': 'US'}}}

    def test_graph_normalization_and_unknown_date(self):
        jobs, _, _ = scraper.parse_page(page({'@graph': [self.posting]}), self.url)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['location'], 'Chicago, IL, US')
        self.assertEqual(jobs[0]['publishedAt'], '')
        self.assertEqual(jobs[0]['postedTime'], 'Unknown')
        self.assertNotIn('<p>', jobs[0]['jobDescription'])
        self.assertEqual(jobs[0]['_source'], 'ZipRecruiter')

    def test_remote_restriction_and_posting_date_preserved(self):
        self.posting.update(jobLocationType='TELECOMMUTE', datePosted='2026-10-08',
                            applicantLocationRequirements={'name': 'Canada'}, jobLocation=[])
        job = scraper.normalize_posting(self.posting, self.url)
        self.assertEqual(job['location'], 'Remote; Canada')
        self.assertEqual(job['publishedAt'], '2026-10-08')

    def test_listing_links_are_deduplicated_and_restricted(self):
        html = '<a href="/jobs/example?jid=abc&utm_source=test">Job</a>' * 2
        html += '<a href="https://evil.test/jobs/fake">Bad</a><a href="/jobs-search">Search</a>'
        _, links, _ = scraper.parse_page(html, self.url)
        self.assertEqual(links, ['https://www.ziprecruiter.com/jobs/example?jid=abc'])
        self.assertEqual(scraper.listing_url('https://user@www.ziprecruiter.com/jobs/x'), '')
        self.assertEqual(scraper.listing_url(''), '')

    def test_challenge_is_error_not_empty_success(self):
        with self.assertRaisesRegex(RuntimeError, 'human verification'):
            scraper.parse_page('<h1>Verify you are human</h1>', self.url)
        self.assertTrue(scraper.parse_page('<p>No jobs found</p>', self.url)[2])
        self.assertFalse(scraper.parse_page('<p>Unexpected markup</p>', self.url)[2])

    def test_script_challenge_string_does_not_block_valid_page(self):
        jobs, _, _ = scraper.parse_page(page(self.posting) +
            '<script>const message="verify you are human";</script>', self.url)
        self.assertEqual(len(jobs), 1)

    def test_browser_failure_reported(self):
        with patch.object(scraper, '_collect', new=AsyncMock(side_effect=OSError('no browser'))):
            self.assertEqual(asyncio.run(scraper._scrape()), [])
        self.assertEqual(scraper.LAST_HEALTH['status'], 'error')

    def test_timeout_preserves_partial_results(self):
        async def collect(jobs, errors):
            jobs[self.url] = scraper.normalize_posting(self.posting, self.url)
            raise asyncio.TimeoutError()
        with patch.object(scraper, '_collect', new=collect):
            self.assertEqual(len(asyncio.run(scraper._scrape())), 1)
        self.assertEqual(scraper.LAST_HEALTH['status'], 'partial')

    def test_challenge_stops_searches_and_browser_is_closed(self):
        browser = Mock()
        module = SimpleNamespace(_create_browser=AsyncMock(return_value=browser))
        reader = AsyncMock(side_effect=RuntimeError('human verification'))
        with patch.dict('sys.modules', {'browser_scraper': module}), patch.object(scraper, '_read_page', reader):
            asyncio.run(scraper._scrape())
        self.assertEqual(reader.await_count, 1)
        browser.stop.assert_called_once()
        self.assertEqual(scraper.LAST_HEALTH['status'], 'error')

    def test_disabled_does_not_launch_browser(self):
        with patch.dict(os.environ, ZIPRECRUITER_BROWSER_ENABLED='false'), patch.object(scraper, '_scrape') as run:
            self.assertEqual(scraper.scrape_jobs(), [])
            run.assert_not_called()

    def test_search_leads_preserve_uncertainty_and_reject_non_listings(self):
        job = scraper.normalize_search_result({'url': self.url, 'title': 'SOC Analyst',
                                               'content': 'Posted two days ago. Monitor SIEM.'})
        self.assertEqual(job['jobDescription'], '')
        self.assertEqual(job['publishedAt'], '')
        self.assertEqual(job['_description_status'], 'search_snippet_only')
        self.assertIn('Monitor SIEM', job['_search_snippet'])
        for url in ('https://evil.test/c/Example/Job/Analyst?jid=1',
                    'https://www.ziprecruiter.com/jobs-search?jid=1',
                    'https://www.ziprecruiter.com/c/Example/Job/Analyst'):
            self.assertIsNone(scraper.normalize_search_result({'url': url}))

    def test_search_rate_limit_stops_without_retry(self):
        session = Mock()
        session.get.return_value.raise_for_status.side_effect = scraper.requests.HTTPError('429')
        jobs, errors = scraper.search_index_jobs(session)
        self.assertEqual(jobs, [])
        self.assertTrue(errors)
        self.assertEqual(session.get.call_count, 1)

    def test_search_results_deduplicate_and_cap(self):
        session = Mock()
        session.get.return_value.json.return_value = {'results': [
            {'url': self.url}, {'url': self.url + '&utm_source=search'},
            {'url': self.url.replace('jid=123', 'jid=456')}]}
        with patch.object(scraper, 'MAX_JOBS', 1):
            jobs, errors = scraper.search_index_jobs(session)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(errors, [])
        self.assertEqual(session.get.call_count, 1)

    def test_blocked_browser_falls_back_with_partial_not_success(self):
        async def blocked():
            scraper.LAST_HEALTH.update(status='error', errors=['Human verification required'])
            return []
        lead = scraper.normalize_search_result({'url': self.url})
        with patch.object(scraper, '_scrape', blocked), patch.object(
                scraper, 'cached_search_index_jobs', return_value=([lead], [])):
            self.assertEqual(scraper.scrape_jobs(), [lead])
        self.assertEqual(scraper.LAST_HEALTH['status'], 'partial')
        self.assertTrue(any('full descriptions' in e for e in scraper.LAST_HEALTH['errors']))

    def test_complete_browser_record_wins_over_search_lead(self):
        full = scraper.normalize_posting(self.posting, self.url)
        async def partial():
            scraper.LAST_HEALTH.update(status='partial', errors=['One search blocked'])
            return [full]
        with patch.object(scraper, '_scrape', partial), patch.object(
                scraper, 'cached_search_index_jobs', return_value=([scraper.normalize_search_result({'url': self.url})], [])):
            self.assertEqual(scraper.scrape_jobs(), [full])

    def test_index_lead_never_scores_even_after_eligibility_override(self):
        root = Path(__file__).parent
        tree = ast.parse((root / 'job_matcher.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'evaluate_candidate_job')
        analyzer = Mock(side_effect=AssertionError('Incomplete listing must not be scored'))
        namespace = dict(hard_filter_job=lambda *a: (True, 'passed'),
                         candidate_matches_track=lambda *a: True,
                         eligibility_decision=lambda *a: ('eligible', 'passed'), analyze_job=analyzer)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(root / 'job_matcher.py'), 'exec'), namespace)
        lead = scraper.normalize_search_result({'url': self.url})
        for override in (False, True):
            result = namespace['evaluate_candidate_job'](lead, 'candidate_a', '', override_eligibility=override)
            self.assertEqual(result['eligibility'], 'needs_review')
        analyzer.assert_not_called()

    def test_recent_cache_avoids_requests_and_preserves_discovery_time(self):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'ziprecruiter_search_cache.json').write_text(json.dumps({
                'scraped_at': stamp, 'jobs': [scraper.normalize_search_result({'url': self.url})]}))
            with patch.dict(os.environ, DB_PATH=directory + '/jobs.db'), patch.object(scraper, 'search_index_jobs') as search:
                jobs, errors = scraper.cached_search_index_jobs()
            search.assert_not_called()
            self.assertEqual(jobs[0]['_discovered_at'], stamp)
            self.assertEqual(jobs[0]['publishedAt'], '')
            self.assertTrue(any('cached' in error for error in errors))

    def test_expired_cache_is_not_imported_when_search_fails(self):
        stamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'ziprecruiter_search_cache.json').write_text(json.dumps({
                'scraped_at': stamp, 'jobs': [scraper.normalize_search_result({'url': self.url})]}))
            with patch.dict(os.environ, DB_PATH=directory + '/jobs.db'), patch.object(
                    scraper, 'search_index_jobs', return_value=([], ['Rate limited'])):
                jobs, errors = scraper.cached_search_index_jobs()
            self.assertEqual(jobs, [])
            self.assertEqual(errors, ['Rate limited'])

    def test_engine_failure_inside_http_success_stops_remaining_searches(self):
        session = Mock()
        session.get.return_value.json.return_value = {'results': [], 'unresponsive_engines': [['engine', 'CAPTCHA']]}
        jobs, errors = scraper.search_index_jobs(session)
        self.assertEqual(jobs, [])
        self.assertEqual(session.get.call_count, 1)
        self.assertTrue(any('providers unavailable' in error for error in errors))

    def test_matcher_dispatches_and_records_ziprecruiter(self):
        # Exercise the actual orchestration without loading Telegram/AI clients.
        root = Path(__file__).parent
        tree = ast.parse((root / 'job_matcher.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'scrape_jobs')
        job = scraper.normalize_posting(self.posting, self.url)
        store = SimpleNamespace(record_run=Mock())
        namespace = dict(concurrent=concurrent, init_db=Mock(return_value=Mock()),
            discovery_store=store, _load_host_jobs=Mock(return_value=[]),
            browser_scraper=SimpleNamespace(scrape_all_jobs=Mock(return_value=[]), LAST_HEALTH={}),
            os=SimpleNamespace(environ={}),
            local_sources_client=SimpleNamespace(scrape_source=Mock(return_value=([], {'errors': [], 'status': 'success'}))),
            startup_job_sources=SimpleNamespace(SOURCES={}, scrape_source=Mock()),
            ziprecruiter_scraper=SimpleNamespace(scrape_jobs=Mock(return_value=[job]),
                LAST_HEALTH={'errors': [], 'status': 'success'}))
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(root / 'job_matcher.py'), 'exec'), namespace)
        ready = Mock()
        self.assertEqual(namespace['scrape_jobs'](on_ready=ready), [job])
        ready.assert_any_call([job])
        self.assertTrue(any(c.args[1] == 'ZipRecruiter' and c.kwargs['status'] == 'success'
                            for c in store.record_run.call_args_list))
        config = json.loads((root / 'apify_sources.json').read_text())
        self.assertFalse(config['enabled'])
        self.assertFalse(any(s.get('enabled') for s in config['sources']))


if __name__ == '__main__':
    unittest.main()
