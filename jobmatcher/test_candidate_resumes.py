import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import candidate_resumes
import job_matcher
import web_server


class ResumeTests(unittest.TestCase):
    def test_matchers_share_resume_ownership(self):
        self.assertIs(job_matcher.CANDIDATE_RESUMES, web_server.WEB_CANDIDATE_RESUMES)
        a = candidate_resumes.CANDIDATE_RESUMES['candidate_a']
        b = candidate_resumes.CANDIDATE_RESUMES['candidate_b']
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertTrue(set(a).isdisjoint(b))
        self.assertEqual(Path(a[0]).name, 'candidate_a.pdf')
        self.assertEqual(Path(b[0]).name, 'candidate_b.pdf')

    def test_replaced_and_deleted_resumes_invalidate_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'resume.pdf'
            path.write_text('Original lab project evidence')
            with patch.object(web_server, 'WEB_CANDIDATE_RESUMES', {'test': [str(path)]}), \
                 patch.object(web_server, '_candidate_profile_cache', {}), \
                 patch.object(web_server, 'WEB_CANDIDATE_CONTEXT', {'test': 'Policy alone is not evidence'}), \
                 patch.object(web_server, 'extract_resume_text_pdftotext', side_effect=lambda p: Path(p).read_text() if Path(p).exists() else '') as extract:
                self.assertIn('Original lab project', web_server.get_candidate_profile('test'))
                web_server.get_candidate_profile('test')
                self.assertEqual(extract.call_count, 1)
                path.write_text('Updated networking project evidence')
                profile = web_server.get_candidate_profile('test')
                self.assertIn('Updated networking project', profile)
                self.assertNotIn('Original lab project', profile)
                path.unlink()
                with self.assertRaises(RuntimeError):
                    web_server.get_candidate_profile('test')
                path.write_text('Restored support project evidence')
                self.assertIn('Restored support project', web_server.get_candidate_profile('test'))


if __name__ == '__main__':
    unittest.main()
