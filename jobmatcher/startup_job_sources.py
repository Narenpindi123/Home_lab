"""Public YC postings and clearly labeled HiringCafe/Wellfound index leads."""
import json
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from local_job_sources import old, posting_detail, record, request, safe_url, settings, text

SOURCES = {'ycombinator': 'Y Combinator', 'hiringcafe': 'HiringCafe', 'wellfound': 'Wellfound'}
HOSTS = {'ycombinator': {'www.ycombinator.com', 'ycombinator.com'},
         'hiringcafe': {'hiring.cafe', 'www.hiring.cafe'},
         'wellfound': {'wellfound.com', 'www.wellfound.com'}}
LIMIT = 200
YC_ROOT = 'https://www.ycombinator.com'
ROLE = r'(?:software-engineer|support|operations)'
YC_CATEGORY = re.compile(r'/jobs/role/' + ROLE + r'(?:/(?:san-francisco|new-york|los-angeles|seattle|boston|austin|remote))?')
TARGET_TITLE = re.compile(r'\b(?:security|cybersecurity|compliance|risk|support|help\s*desk|service\s*desk|network|IAM|GRC|SOC|NOC|IT)\b', re.I)


def listing_url(source, value):
    """Only individual HTTPS listings, never search/category or arbitrary URLs."""
    if not isinstance(value, str) or not safe_url(value, HOSTS[source]):
        return ''
    parts = urlsplit(value)
    path, query = parts.path.rstrip('/'), ''
    if source == 'ycombinator':
        valid = re.fullmatch(r'/companies/[A-Za-z0-9_-]+/jobs/[A-Za-z0-9_-]+', path)
        host = 'www.ycombinator.com'
    elif source == 'wellfound':
        valid = re.fullmatch(r'/jobs/[0-9]+-[A-Za-z0-9_-]+', path)
        host = 'wellfound.com'
    else:
        host = 'hiring.cafe'
        valid = re.fullmatch(r'/viewjob/[A-Za-z0-9_-]+', path)
        if not path:
            ids = parse_qs(parts.query).get('job', [])
            valid = len(ids) == 1 and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', ids[0])
            if valid:
                path, query = '/', urlencode({'job': ids[0]})
    return urlunsplit(('https', host, path, query, '')) if valid else ''


def yc_index(page):
    node = BeautifulSoup(page, 'html.parser').select_one('[data-page]')
    if node is None:
        raise ValueError('YC listing page has no public job data (possibly blocked)')
    data = json.loads(node['data-page'])
    props = data.get('props') if isinstance(data, dict) else None
    if not isinstance(props, dict) or not isinstance(props.get('jobPostings'), list):
        raise ValueError('Unexpected YC listing format')
    jobs = props['jobPostings']
    if any(not isinstance(job, dict) for job in jobs):
        raise ValueError('Malformed YC job entry')
    # Last activity and relative creation labels are not publication dates.
    links = []
    for entry in props.get('sidebarLinks', []):
        if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], str) and YC_CATEGORY.fullmatch(entry[1]):
            links.append(YC_ROOT + entry[1])
    return jobs, links


def scrape_yc():
    jobs, errors, visited, listings = [], [], set(), {}
    pages = [YC_ROOT + '/jobs/role/' + role for role in ('software-engineer', 'support', 'operations')]
    for page_url in pages:
        if page_url in visited:
            continue
        visited.add(page_url)
        try:
            entries, more = yc_index(request(page_url))
            for url in more:
                if url not in pages and len(pages) < 24:
                    pages.append(url)
            for entry in entries:
                url = listing_url('ycombinator', urljoin(YC_ROOT, str(entry.get('url', ''))))
                if url and TARGET_TITLE.search(text(entry.get('title'))):
                    listings.setdefault(url, entry)
            if len(listings) >= LIMIT:
                break
            time.sleep(.2)
        except (requests.RequestException, ValueError, TypeError) as exc:
            errors.append(f'YC index: {type(exc).__name__}: {exc}')
            if 'HTTP 403' in str(exc) or 'HTTP 429' in str(exc):
                return jobs, errors
    days = settings()['ats']['postedWithinDays']
    for url in list(listings)[:LIMIT]:
        try:
            job = posting_detail(request(url), url, SOURCES['ycombinator'])
            if job and not old(job, days):
                jobs.append(job)
            time.sleep(.2)
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            errors.append(f'YC detail: {type(exc).__name__}: {exc}')
            if 'HTTP 403' in str(exc) or 'HTTP 429' in str(exc):
                break
    return jobs, errors


def index_lead(source, item):
    if not isinstance(item, dict):
        return None
    url = listing_url(source, item.get('url'))
    title = text(item.get('title'))
    if not url or not title:
        return None
    # A search result title/snippet is evidence of a lead, not a complete posting.
    title = re.sub(r'\s*[|–—-]\s*(?:Wellfound|Hiring\.?\s*Cafe).*$', '', title, flags=re.I).strip()
    job = record(title, '', url, '', '', '', '', SOURCES[source],
                 _description_status='search_snippet_only', _discovery_method='public_search_index',
                 _search_title=text(item.get('title')), _search_snippet=text(item.get('content'))[:2000])
    return job


def scrape_index(source):
    """No challenged-page fetching or invented full descriptions/dates."""
    domain = 'hiring.cafe' if source == 'hiringcafe' else 'wellfound.com/jobs/'
    endpoint = os.environ.get('SEARCH_API_URL', 'http://127.0.0.1:8888/search')
    jobs, errors = {}, [f'{SOURCES[source]}: search-index discovery only; full descriptions, dates and availability need verification']
    with requests.Session() as session:
        for query in settings()['ats']['keywords']:
            try:
                response = session.get(endpoint, params={
                    'q': f'site:{domain} "{query}"', 'format': 'json',
                    'language': 'en-US', 'safesearch': 1, 'time_range': 'week'}, timeout=(4, 20))
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
                    raise ValueError('Unexpected search response')
                for item in payload['results']:
                    job = index_lead(source, item)
                    if job:
                        jobs.setdefault(job['jobUrl'], job)
                    if len(jobs) >= LIMIT:
                        break
                if payload.get('unresponsive_engines'):
                    errors.append('Search providers unavailable or rate-limited; remaining queries stopped')
                    break
                if len(jobs) >= LIMIT:
                    break
            except (requests.RequestException, ValueError) as exc:
                errors.append(f'Search discovery failed: {type(exc).__name__}')
                break
    if not jobs:
        errors.append('No individual listings discovered')
    return list(jobs.values()), errors


def scrape_source(source):
    if source not in SOURCES:
        raise ValueError('Unsupported startup source')
    jobs, errors = scrape_yc() if source == 'ycombinator' else scrape_index(source)
    return jobs, {'status': 'partial' if jobs and errors else 'error' if errors else 'success',
                  'errors': list(dict.fromkeys(errors))}
