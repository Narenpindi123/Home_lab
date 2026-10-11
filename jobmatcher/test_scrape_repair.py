import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone
import requests
import apify_sources as apify
import job_matcher as matcher

class FallbackTests(unittest.TestCase):
    def test_rate_limited_cloud_falls_back_and_stays_in_cooldown(self):
        cloud=('http://127.0.0.1:11434','gpt-oss:20b-cloud')
        local=('http://127.0.0.1:11434','gemma4:e2b')
        error_response=Mock(status_code=429,headers={'Retry-After':'7200'})
        bad=Mock();bad.raise_for_status.side_effect=requests.HTTPError(response=error_response)
        good=Mock();good.json.return_value={'message':{'content':'valid result'}}
        def choose():
            if matcher.ACTIVE_CHAT_ENDPOINT is None:matcher.ACTIVE_CHAT_ENDPOINT=local
        with patch.object(matcher,'ACTIVE_CHAT_ENDPOINT',cloud),patch.object(matcher,'_ollama_down_until',{}),patch.object(matcher,'check_ollama_endpoints',side_effect=choose),patch.object(matcher.http_session,'post',side_effect=[bad,good]),patch.object(matcher.time,'sleep'),patch.object(matcher.time,'time',return_value=100):
            self.assertEqual(matcher.get_ollama_response('prompt',json_mode=True),'valid result')
            self.assertEqual(matcher._ollama_down_until[('chat',*cloud)],7300)
            self.assertEqual(matcher.ACTIVE_CHAT_ENDPOINT,local)

class GlassdoorTests(unittest.TestCase):
    def test_details_are_identity_checked_and_converted_to_text(self):
        from glassdoor_scraper import parse_job_details
        data={'listingId':123,'jobDescription':'<p>Monitor alerts &amp; investigate incidents.</p>'}
        self.assertEqual(parse_job_details(data,'123'),'Monitor alerts & investigate incidents.')
        with self.assertRaises(ValueError):parse_job_details(data,'456')
        with self.assertRaises(ValueError):parse_job_details({'jobDescription':'wrong job'},'123')

    def test_search_pane_uses_its_own_id_and_ignores_snippets(self):
        from glassdoor_scraper import search_page_descriptions
        from bs4 import BeautifulSoup
        soup=BeautifulSoup('<div class="JobCard_jobDescriptionSnippet">Not a full description</div>'
            '<div class="JobDetails_jobDescription" data-brandviews="PAGE:jlid=123">'
            'Full description of the selected listing.</div>', 'lxml')
        self.assertEqual(search_page_descriptions(soup),{'123':'Full description of the selected listing.'})

class DescriptionCacheTests(unittest.TestCase):
    def test_old_description_does_not_become_fresh_with_new_snapshot(self):
        import json
        import glassdoor_scraper as g
        from datetime import datetime, timedelta, timezone
        from unittest.mock import mock_open
        now=datetime.now(timezone.utc)
        snapshot={'scraped_at':now.isoformat(),'jobs':[
            {'jobUrl':'https://example.com/?jl=1','jobDescription':'A full cached job description.',
             '_description_fetched_at':(now-timedelta(days=2)).isoformat()},
            {'jobUrl':'https://example.com/?jl=2','jobDescription':'A fresh full job description.',
             '_description_fetched_at':(now-timedelta(hours=1)).isoformat()}]}
        with patch.object(g.Path,'read_text',return_value=json.dumps(snapshot)):
            descriptions,timestamps=g.load_description_cache()
        self.assertEqual(set(descriptions),{'2'})
        self.assertEqual(timestamps['2'],snapshot['jobs'][1]['_description_fetched_at'])

class GlassdoorRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_waits_then_retries_same_listing(self):
        from unittest.mock import AsyncMock
        import glassdoor_scraper as g
        import json
        tab=Mock()
        tab.evaluate=AsyncMock(side_effect=[json.dumps({'status':429,'retryAfter':'90'}),
            json.dumps({'status':200,'data':{'listingId':123,'jobDescription':'A full description of this job.'}})])
        with patch.object(g.asyncio,'sleep',new_callable=AsyncMock) as sleep:
            self.assertEqual(await g.fetch_job_description(tab,'https://www.glassdoor.com/job-listing/j?jl=123'),
                             'A full description of this job.')
            sleep.assert_awaited_once_with(90)
            self.assertEqual(tab.evaluate.await_count,2)

    async def test_rate_limit_retries_are_bounded(self):
        from unittest.mock import AsyncMock
        import glassdoor_scraper as g
        tab=Mock();tab.evaluate=AsyncMock(return_value='{"status":429,"retryAfter":"60"}')
        with patch.object(g.asyncio,'sleep',new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(RuntimeError,'HTTP 429'):
                await g.fetch_job_description(tab,'https://www.glassdoor.com/job-listing/j?jl=123')
            self.assertEqual(tab.evaluate.await_count,3)
            self.assertEqual(sleep.await_count,2)

class EvidenceFormattingTests(unittest.TestCase):
    def test_markdown_formatting_does_not_discard_exact_evidence(self):
        from grounded_scoring import verified_excerpt
        source='**Identity:** Microsoft Entra ID (Azure AD), MFA and conditional access basics.'
        quote='Identity: Microsoft Entra ID (Azure AD), MFA and conditional access basics.'
        found=verified_excerpt(quote,source)
        self.assertIsNotNone(found)
        self.assertIn(found,source)
        self.assertIsNotNone(verified_excerpt('Review user-reported suspicious email.',r'Review user\-reported suspicious email.'))

    def test_formatting_tolerance_does_not_accept_changed_claims(self):
        from grounded_scoring import verified_excerpt
        self.assertIsNone(verified_excerpt('Requires 2 years of experience.','Requires **5 years** of experience.'))
        self.assertIsNone(verified_excerpt('Citizenship is required.','Citizenship is **not** required.'))
        self.assertIsNone(verified_excerpt('Monitor alerts and investigate incidents.','Monitor alerts and escalate them; investigate incidents.'))

if __name__=='__main__':unittest.main()
