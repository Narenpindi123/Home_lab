"""Public package must start without private files or notification credentials."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class PublicSetupTests(unittest.TestCase):
    def test_first_run_without_telegram_or_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / 'new/data/jobs.db')
            env = dict(os.environ, DB_PATH=db, JOB_PIPELINE_DB=db,
                       TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='')
            code = '''
import job_matcher
import web_server
assert job_matcher.bot is None
job_matcher.send_telegram_msg('must not send')
client = web_server.app.test_client()
assert client.get('/').status_code == 200
assert client.get('/api/jobs').status_code == 200
with web_server.get_db() as conn:
    assert conn.execute('SELECT count(*) FROM jobs_history').fetchone()[0] == 0
'''
            subprocess.run([sys.executable, '-c', code], env=env,
                           cwd=Path(__file__).parent, check=True, timeout=20,
                           capture_output=True, text=True)

    def test_telegram_commands_require_configured_chat(self):
        import job_matcher
        with patch.object(job_matcher, 'TELEGRAM_CHAT_ID', '123'):
            self.assertTrue(job_matcher.authorized_chat(SimpleNamespace(chat=SimpleNamespace(id=123))))
            self.assertFalse(job_matcher.authorized_chat(SimpleNamespace(chat=SimpleNamespace(id=456))))
        with patch.object(job_matcher, 'TELEGRAM_CHAT_ID', ''):
            self.assertFalse(job_matcher.authorized_chat(SimpleNamespace(chat=SimpleNamespace(id=123))))

    def test_plain_host_startup_binds_only_to_loopback(self):
        import web_server
        with patch.dict(os.environ, {}, clear=True), patch.object(web_server.app, 'run') as run:
            web_server.start_web_server()
        self.assertEqual(run.call_args.kwargs['host'], '127.0.0.1')
