import html
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

import startup_job_sources as sources
import local_job_sources

YC_URL = 'https://www.ycombinator.com/companies/example/jobs/abc-security-analyst'


def listing_page(entries, links=None):
    return '<div data-page="' + html.escape(json.dumps({'props': {
        'jobPostings': entries, 'sidebarLinks': links or []}}), quote=True) + '"></div>'


def detail_page(date=None):
    return '<script type="application/ld+json">' + json.dumps({
        '@type': 'JobPosting', 'title': 'Security Analyst',
        'hiringOrganization': {'name': 'Example'}, 'description': '<p>Monitor security alerts and investigate incidents.</p>',
        'datePosted': date, 'jobLocationType': 'TELECOMMUTE',
        'applicantLocationRequirements': {'name': 'United States'},
        'jobLocation': {'address': {'addressCountry': 'US'}}}) + '</script>'


class StartupSourceTests(unittest.TestCase):
    def test_individual_url_canonicalization(self):
        self.assertEqual(sources.listing_url('ycombinator', YC_URL + '?utm_source=test#x'), YC_URL)
        self.assertEqual(sources.listing_url('wellfound', 'https://www.wellfound.com/jobs/123-soc-analyst/?ref=a'),
                         'https://wellfound.com/jobs/123-soc-analyst')
        for url in ('https://hiring.cafe/?job=abc_123&utm_source=x', 'https://hiring.cafe/viewjob/abc_123?ref=x'):
            self.assertNotIn('utm_', sources.listing_url('hiringcafe', url))
            self.assertTrue(sources.listing_url('hiringcafe', url))

    def test_rejects_categories_untrusted_hosts_and_credentials(self):
        for source, url in [('wellfound', 'https://wellfound.com/jobs'),
                            ('wellfound', 'https://wellfound.com/jobs/security'),
                            ('hiringcafe', 'https://hiring.cafe/?searchState=x'),
                            ('hiringcafe', 'https://hiring.cafe/?job=a&job=b'),
                            ('hiringcafe', 'https://hiring.cafe/?job=..%2Fprivate'),
                            ('ycombinator', 'https://www.ycombinator.com/jobs/role/support'),
                            ('ycombinator', YC_URL.replace('https:', 'http:')),
                            ('ycombinator', YC_URL.replace('www.ycombinator.com', 'www.ycombinator.com.evil.test')),
                            ('ycombinator', YC_URL.replace('https://', 'https://user:pass@')),
                            ('ycombinator', YC_URL.replace('.com/', '.com:8443/')),
                            ('wellfound', None)]:
            with self.subTest(url=url):
                self.assertEqual(sources.listing_url(source, url), '')

    def test_yc_discovery_links_are_bounded_and_same_origin(self):
        entries, links = sources.yc_index(listing_page([], [
            ['Remote', '/jobs/role/support/remote'], ['External', 'https://evil.test/jobs'],
            ['Wrong role', '/jobs/role/marketing'], ['India', '/jobs/role/support/india']]))
        self.assertEqual(entries, [])
        self.assertEqual(links, [sources.YC_ROOT + '/jobs/role/support/remote'])

    def test_yc_rejects_blocked_or_malformed_pages(self):
        for page in ['<h1>Verify you are human</h1>', '<div data-page="{}"></div>', listing_page([None])]:
            with self.assertRaises(ValueError):
                sources.yc_index(page)

    def test_yc_dedup_description_and_actual_publication_date(self):
        today = datetime.now(timezone.utc).isoformat()
        listing = listing_page([{'title': 'Security Analyst', 'url': YC_URL, 'lastActive': '1 day'}])
        with patch.object(sources, 'request', side_effect=[listing] * 3 + [detail_page(today)]) as fetch, patch.object(sources.time, 'sleep'):
            jobs, health = sources.scrape_source('ycombinator')
        self.assertEqual(health['status'], 'success')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(jobs[0]['publishedAt'], today)
        self.assertEqual(jobs[0]['jobDescription'], 'Monitor security alerts and investigate incidents.')
        self.assertEqual(jobs[0]['companyName'], 'Example')
        self.assertEqual(jobs[0]['_source'], 'Y Combinator')

    def test_yc_filters_old_date_not_recent_activity(self):
        listing = listing_page([{'title': 'Security Analyst', 'url': YC_URL, 'lastActive': '1 day'}])
        old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        with patch.object(sources, 'request', side_effect=[listing] * 3 + [detail_page(old)]), patch.object(sources.time, 'sleep'):
            jobs, errors = sources.scrape_yc()
        self.assertEqual(jobs, [])
        self.assertEqual(errors, [])
        job = local_job_sources.posting_detail(detail_page(), YC_URL, 'Y Combinator')
        self.assertEqual(job['publishedAt'], '')

    def test_yc_stops_on_rate_limit(self):
        with patch.object(sources, 'request', side_effect=ValueError('HTTP 429')) as fetch:
            jobs, health = sources.scrape_source('ycombinator')
        self.assertEqual(jobs, [])
        self.assertEqual(health['status'], 'error')
        self.assertEqual(fetch.call_count, 1)

    def test_index_leads_never_invent_description_date_or_company(self):
        job = sources.index_lead('wellfound', {'url': 'https://wellfound.com/jobs/123-security-analyst',
            'title': 'Security Analyst at Example | Wellfound', 'content': '<b>Hiring today</b>'})
        self.assertEqual(job['jobTitle'], 'Security Analyst at Example')
        for field in ['jobDescription', 'publishedAt', 'companyName', 'location']:
            self.assertEqual(job[field], '')
        self.assertEqual(job['_description_status'], 'search_snippet_only')
        self.assertEqual(job['_search_snippet'], 'Hiring today')

    def test_search_partial_preserves_leads_and_stops_unavailable_engines(self):
        item = {'url': 'https://wellfound.com/jobs/123-security-analyst', 'title': 'Security Analyst'}
        response = Mock()
        response.json.return_value = {'results': [item, item, {'url': 'https://wellfound.com/jobs'}],
                                      'unresponsive_engines': [['engine', 'too many requests']]}
        with patch.object(sources.requests, 'Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.get.return_value = response
            jobs, health = sources.scrape_source('wellfound')
            self.assertEqual(session.get.call_count, 1)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(health['status'], 'partial')
        self.assertTrue(any('stopped' in e for e in health['errors']))

    def test_search_failure_is_error_not_empty_success(self):
        with patch.object(sources.requests, 'Session') as factory:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = sources.requests.Timeout()
            jobs, health = sources.scrape_source('hiringcafe')
        self.assertEqual(jobs, [])
        self.assertEqual(health['status'], 'error')

    def test_all_sources_advertised_in_health(self):
        names = {s['name'] for s in local_job_sources.configured_sources()}
        self.assertTrue(set(sources.SOURCES.values()) <= names)

    def test_matcher_routes_all_sources_and_isolates_failure(self):
        import job_matcher as matcher
        complete = {'jobUrl': YC_URL, '_source': 'Y Combinator'}
        def fetch(source):
            if source == 'hiringcafe':
                raise sources.requests.Timeout()
            return ([complete] if source == 'ycombinator' else []), {'status': 'success', 'errors': []}
        with patch.object(matcher, 'init_db', return_value=Mock()), \
             patch.object(matcher, '_load_host_jobs', return_value=[]), \
             patch.object(matcher.discovery_store, 'record_run') as record_run, \
             patch.object(matcher.browser_scraper, 'scrape_all_jobs', return_value=[]), \
             patch.dict(matcher.os.environ, {'LINKEDIN_API_URL': ''}), \
             patch.object(matcher.local_sources_client, 'scrape_source', return_value=([], {'status': 'success', 'errors': []})), \
             patch.object(matcher.ziprecruiter_scraper, 'scrape_jobs', return_value=[]), \
             patch.object(matcher.startup_job_sources, 'scrape_source', side_effect=fetch):
            ready = []
            jobs = matcher.scrape_jobs(on_ready=ready.extend)
        self.assertIn(complete, jobs)
        self.assertIn(complete, ready)
        measured = {call.args[1]: call for call in record_run.call_args_list}
        self.assertTrue(set(sources.SOURCES.values()) <= measured.keys())
        self.assertTrue(measured['HiringCafe'].kwargs['errors'])

    def test_search_lead_is_reviewed_without_ai_scoring(self):
        import job_matcher as matcher
        job = sources.index_lead('wellfound', {'url': 'https://wellfound.com/jobs/123-soc-analyst',
                                            'title': 'SOC Analyst'})
        analyzer = Mock()
        with patch.object(matcher, 'hard_filter_job', return_value=(True, 'passed')), \
             patch.object(matcher, 'candidate_matches_track', return_value=True), \
             patch.object(matcher, 'eligibility_decision', return_value=('eligible', 'passed')):
            result = matcher.evaluate_candidate_job(job, 'candidate_a', 'profile', analyzer=analyzer)
        self.assertEqual(result['eligibility'], 'needs_review')
        analyzer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
