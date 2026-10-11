import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import recover_rejected_jobs as recovery


class RecoveryAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches = [
            patch.object(recovery, 'COOLDOWN_FILE', Path(self.temp.name) / 'cooldowns.json'),
            patch.object(recovery, 'blocked_sources', set()),
            patch.object(recovery, 'request_interval', 0),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.item = dict(source='LinkedIn', key='linkedin:1234567890',
                         url='https://www.linkedin.com/jobs/view/1234567890',
                         title='SOC Analyst', company='Example', location='Austin, TX')

    def test_rate_limit_is_persisted_and_stops_later_requests(self):
        response = Mock(status_code=429, headers={'Retry-After': '1800'})
        with patch.object(recovery.time, 'time', return_value=10000):
            with patch.object(recovery.requests, 'get', return_value=response) as get:
                self.assertEqual(recovery.fetch_job(self.item), (None, 'source_access_blocked:HTTP_429'))
                self.assertEqual(recovery.fetch_job(self.item), (None, 'source_access_blocked'))
                self.assertEqual(get.call_count, 1)
        self.assertEqual(recovery.source_cooldowns()['LinkedIn']['retry_at'], 11800)

    def test_access_denial_does_not_produce_a_job(self):
        with patch.object(recovery.requests, 'get', return_value=Mock(status_code=403, headers={})):
            job, reason = recovery.fetch_job(self.item)
        self.assertIsNone(job)
        self.assertIn('403', reason)

    def test_expired_and_empty_pages_are_not_invented_descriptions(self):
        for html, expected in [('<html>No longer accepting applications</html>', 'expired'),
                               ('<html>Sign in to continue</html>', 'description_unavailable')]:
            with patch.object(recovery.requests, 'get', return_value=Mock(status_code=200, text=html)):
                self.assertEqual(recovery.fetch_job(self.item), (None, expected))


if __name__ == '__main__':
    unittest.main()
