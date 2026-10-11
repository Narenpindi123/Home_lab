"""Client for the local Dice and employer-board API; never calls paid services."""
import os
from pathlib import Path
import time
import requests


def scrape_source(source):
    base = os.environ.get('JOB_SOURCES_API_URL', 'http://job-sources-api:5556').rstrip('/')
    token_file = os.environ.get('JOB_SOURCES_API_TOKEN_FILE', '/app/data/job-sources-api/token')
    with requests.Session() as session:
        session.headers['Authorization'] = 'Bearer ' + Path(token_file).read_text().strip()
        response = session.post(base + '/v1/sources/' + source + '/runs', json={},
                                params={'timeout': 1200}, timeout=(10, 30))
        response.raise_for_status()
        run = response.json()['data']
        # Covers the two serialized local source runs including queue time.
        deadline = time.monotonic() + 2600
        while run['status'] in ('READY', 'RUNNING'):
            if time.monotonic() > deadline:
                raise TimeoutError('Local source wait exceeded deadline')
            response = session.get(base + '/v2/actor-runs/' + run['id'],
                                   params={'waitForFinish': 20}, timeout=(10, 30))
            response.raise_for_status()
            run = response.json()['data']
        response = session.get(base + '/v2/datasets/' + run['defaultDatasetId'] + '/items', timeout=(10, 30))
        response.raise_for_status()
        jobs = response.json()
    errors = run.get('errors', [])
    if run['status'] != 'SUCCEEDED' and not errors:
        errors = ['Local run finished ' + run['status']]
    return jobs, {'status': 'partial' if jobs and errors else 'error' if errors else 'success', 'errors': errors}
