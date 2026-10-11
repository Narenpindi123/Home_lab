import unittest
from browser_scraper import canonical_linkedin_url, linkedin_job_id


class LinkedInIdentityTests(unittest.TestCase):
    def test_tracking_variants_and_slug_share_identity(self):
        urls = [
            'https://www.linkedin.com/jobs/view/grc-analyst-at-example-4441553219?trackingId=first',
            'https://www.linkedin.com/jobs/view/4441553219/?trackingId=second',
            'https://www.linkedin.com/jobs/search/?currentJobId=4441553219',
        ]
        self.assertEqual({canonical_linkedin_url(url) for url in urls},
                         {'https://www.linkedin.com/jobs/view/4441553219/'})
        self.assertEqual(linkedin_job_id(urls[0]), '4441553219')

    def test_non_job_link_is_unchanged(self):
        url = 'https://www.linkedin.com/jobs/search/?keywords=GRC'
        self.assertEqual(canonical_linkedin_url(url), url)
        self.assertIsNone(linkedin_job_id(url))
