"""Jobmatcher adapter for the self-hosted LinkedIn service."""
import os
from pathlib import Path
import time
import requests

LAST_HEALTH = {'status': 'unknown', 'errors': []}


def scrape_jobs(search_urls):
    base = os.environ['LINKEDIN_API_URL'].rstrip('/')
    token_path = os.environ.get('LINKEDIN_API_TOKEN_FILE', '/app/data/linkedin-api/token')
    token = Path(token_path).read_text().strip()
    timeout = 3600
    with requests.Session() as session:
        session.headers['Authorization'] = 'Bearer ' + token
        response = session.post(base + '/v1/jobs/search', json={
            'startUrls': [{'url': url} for url in search_urls], 'limit': 1000,
            'maxPagesPerSearch': 8, 'maxJobsPerSearch': 60, 'includeDescriptions': True,
        }, params={'timeout': timeout}, timeout=(10, 30))
        response.raise_for_status()
        run = response.json()['data']
        deadline = time.monotonic() + timeout + 120
        while run['status'] in ('READY', 'RUNNING'):
            if time.monotonic() >= deadline:
                raise TimeoutError('Local LinkedIn API wait expired')
            response = session.get(base + '/v2/actor-runs/' + run['id'], params={'waitForFinish': 20}, timeout=(10, 30))
            response.raise_for_status()
            run = response.json()['data']
        response = session.get(base + '/v2/datasets/' + run['defaultDatasetId'] + '/items', timeout=(10, 30))
        response.raise_for_status()
        jobs = response.json()
    errors = run.get('errors', [])
    if run['status'] != 'SUCCEEDED' and not errors:
        errors = ['Local LinkedIn API run ' + run['status']]
    LAST_HEALTH.update(status='partial' if jobs and errors else 'error' if errors else 'success', errors=errors)
    # Jobmatcher already records discovery health and applies its own eligibility rules.
    return jobs
