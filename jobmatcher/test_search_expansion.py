import unittest
from unittest.mock import AsyncMock, patch

import browser_scraper as scraper
from eligibility import location_decision
from matching_rules import candidate_matches_track


class SearchExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_page_does_not_hide_deeper_results(self):
        def card(job_id):
            return (f'<div class="base-card"><h3>IT Support Specialist</h3>'
                    f'<a href="https://www.linkedin.com/jobs/view/{job_id}/">Job</a></div>')
        with patch.object(scraper, 'LINKEDIN_SEARCH_URLS', ['https://www.linkedin.com/jobs/search/?keywords=IT']), \
                patch.object(scraper, '_goto', new_callable=AsyncMock, return_value=True) as navigate, \
                patch.object(scraper, '_scroll_page', new_callable=AsyncMock), \
                patch.object(scraper, '_random_delay', new_callable=AsyncMock), \
                patch.object(scraper, '_safe_get_content', new_callable=AsyncMock,
                             side_effect=[card(123), card(123), card(456)]):
            jobs = await scraper._scrape_linkedin(None, None, {
                'https://www.linkedin.com/jobs/view/123/',
                'https://www.linkedin.com/jobs/view/456/',
            })
        self.assertEqual(len(jobs), 2)
        self.assertEqual(navigate.await_count, 3)
        self.assertIn('start=50', navigate.await_args_list[-1].args[1])

    def test_known_metro_still_honors_explicit_foreign_restriction(self):
        metro = 'Albuquerque-Santa Fe Metropolitan Area'
        self.assertEqual(location_decision(metro)[0], 'eligible')
        self.assertEqual(location_decision(metro, 'Location: Canada')[0], 'rejected')
        self.assertEqual(location_decision('Greater London')[0], 'rejected')
        self.assertEqual(location_decision('Unknown Metropolitan Area')[0], 'needs_review')

    def test_new_it_routes_are_candidate_specific(self):
        for title in ['NOC Technician', 'Data Center Technician', 'Access Provisioning Analyst']:
            job = {'jobTitle': title, 'jobDescription': ''}
            self.assertTrue(candidate_matches_track(job, 'candidate_a'))
            self.assertFalse(candidate_matches_track(job, 'candidate_b'))


if __name__ == '__main__':
    unittest.main()
