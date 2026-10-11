"""Bounded public LinkedIn job retrieval. No Apify, login, proxies or paid APIs."""
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from bs4 import BeautifulSoup

ORIGIN = 'https://www.linkedin.com'
SEARCH_PATH = '/jobs-guest/jobs/api/seeMoreJobPostings/search'
FILTERS = {
    'experienceLevel': ('f_E', {'internship': '1', 'entry-level': '2', 'associate': '3', 'mid-senior': '4', 'director': '5', 'executive': '6'}),
    'workType': ('f_WT', {'on-site': '1', 'remote': '2', 'hybrid': '3'}),
    'jobType': ('f_JT', {'full-time': 'F', 'part-time': 'P', 'contract': 'C', 'temporary': 'T', 'internship': 'I', 'volunteer': 'V'}),
}
QUERY_KEYS = {'keywords', 'location', 'geoId', 'f_E', 'f_WT', 'f_JT', 'f_TPR', 'sortBy', 'distance', 'f_C', 'f_AL', 'f_EA'}
INPUT_KEYS = {'keyword', 'location', 'locations', 'startUrls', 'limit', 'publishedAt',
              'experienceLevel', 'workType', 'jobType', 'distance', 'sortBy',
              'includeDescriptions', 'maxPagesPerSearch', 'maxJobsPerSearch', 'saveOnlyUniqueItems'}


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')
    return value


def strings(value, name, maximum=64):
    if not isinstance(value, list) or len(value) > maximum or any(
            not isinstance(x, str) or not x.strip() or len(x) > 500 for x in value):
        raise ValueError(f'{name} must be an array of up to {maximum} nonempty strings')
    return [x.strip() for x in value]


def normalize_input(body):
    if not isinstance(body, dict):
        raise ValueError('Input must be a JSON object')
    unknown = set(body) - INPUT_KEYS
    if unknown:
        raise ValueError('Unsupported input fields: ' + ', '.join(sorted(unknown)))
    limit = integer(body.get('limit', 50), 'limit', 1, 1000)
    pages = integer(body.get('maxPagesPerSearch', 3), 'maxPagesPerSearch', 1, 40)
    per_search = integer(body.get('maxJobsPerSearch', limit), 'maxJobsPerSearch', 1, 1000)
    for key in ('includeDescriptions', 'saveOnlyUniqueItems'):
        if key in body and not isinstance(body[key], bool):
            raise ValueError(f'{key} must be boolean')
    if body.get('saveOnlyUniqueItems') is False:
        raise ValueError('This service always removes duplicate job IDs within a run')
    searches = []
    urls = body.get('startUrls', [])
    if not isinstance(urls, list) or len(urls) > 64:
        raise ValueError('startUrls must be an array of at most 64 LinkedIn search URLs')
    for item in urls:
        url = item.get('url') if isinstance(item, dict) else item
        if not isinstance(url, str) or len(url) > 4000:
            raise ValueError('Invalid start URL')
        parts = urlsplit(url)
        if (parts.scheme != 'https' or parts.hostname not in ('linkedin.com', 'www.linkedin.com')
                or parts.username or parts.password or parts.port not in (None, 443)
                or parts.path.rstrip('/') != '/jobs/search'):
            raise ValueError('startUrls must use https://www.linkedin.com/jobs/search/')
        # Rebuild all requests on a fixed origin; never fetch caller-supplied hosts.
        query = {k: v[0] for k, v in parse_qs(parts.query).items() if k in QUERY_KEYS}
        searches.append(query)
    keywords = strings(body.get('keyword', []), 'keyword')
    locations = strings(body.get('locations', []), 'locations')
    if 'location' in body:
        locations.extend(strings([body['location']], 'location'))
    params = {}
    for key, (query_key, mapping) in FILTERS.items():
        selected = strings(body.get(key, []), key, 6)
        if any(x not in mapping for x in selected):
            raise ValueError(f'Unsupported {key} value')
        if selected:
            params[query_key] = ','.join(mapping[x] for x in selected)
    for key, query_key, allowed, default in (
            ('publishedAt', 'f_TPR', {'', 'r86400', 'r604800', 'r2592000'}, ''),
            ('distance', 'distance', {'', '0', '5', '10', '25', '50', '100'}, ''),
            ('sortBy', 'sortBy', {'DD', 'R'}, 'DD')):
        value = body.get(key, default)
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(f'Unsupported {key} value')
        if value:
            params[query_key] = value
    for keyword in keywords:
        for location in locations or ['United States']:
            searches.append(dict(params, keywords=keyword, location=location))
    # Stable de-duplication also keeps equivalent URLs from doubling requests.
    searches = list({urlencode(sorted(x.items())): x for x in searches}.values())
    if not 1 <= len(searches) <= 64:
        raise ValueError('Provide keyword or startUrls; at most 64 searches per run')
    return dict(searches=searches, limit=limit, maxPagesPerSearch=pages,
                maxJobsPerSearch=per_search, includeDescriptions=body.get('includeDescriptions', True))


def text(element):
    return element.get_text(' ', strip=True) if element else ''


def parse_cards(html):
    soup = BeautifulSoup(html, 'html.parser')
    jobs = []
    for card in soup.select('div.base-card'):
        link = card.select_one('a[href*="/jobs/view/"]')
        match = re.search(r'/jobs/view/(?:[^/?]*-)?(\d+)(?:[/?]|$)', link.get('href', '') if link else '')
        title = text(card.select_one('h3'))
        if not match or not title:
            continue
        job_id = match[1]
        date = card.select_one('time')
        url = f'{ORIGIN}/jobs/view/{job_id}/'
        jobs.append(dict(id=job_id, jobTitle=title, companyName=text(card.select_one('h4')),
                         location=text(card.select_one('.job-search-card__location')),
                         jobUrl=url, applyUrl=url, publishedAt=date.get('datetime', '') if date else '',
                         postedTime=text(date), jobDescription='', contractType='', _source='LinkedIn'))
    return jobs


def add_description(job, html):
    soup = BeautifulSoup(html, 'html.parser')
    description = soup.select_one('.show-more-less-html__markup, .description__text')
    job['jobDescription'] = text(description)
    for item in soup.select('.description__job-criteria-item'):
        label = text(item.select_one('h3')).lower()
        value = text(item.select_one('.description__job-criteria-text'))
        key = {'seniority level': 'seniorityLevel', 'employment type': 'contractType',
               'job function': 'jobFunction', 'industries': 'industries'}.get(label)
        if key:
            job[key] = value
    return bool(job['jobDescription'])


class UpstreamError(Exception):
    pass


class PublicClient:
    def __init__(self, delay=1.5):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US,en;q=0.9'})
        self.delay, self.last = delay, 0

    def get(self, path, params=None):
        time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        try:
            with self.session.get(ORIGIN + path, params=params, timeout=(10, 30),
                                  allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise UpstreamError(f'LinkedIn returned HTTP {response.status_code}; no bypass attempted')
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 2_000_000:
                        raise UpstreamError('LinkedIn response exceeded size limit')
                    chunks.append(chunk)
                return b''.join(chunks).decode('utf-8', errors='replace')
        except requests.RequestException as exc:
            raise UpstreamError(f'LinkedIn network error: {type(exc).__name__}') from None


def scrape(config, progress=lambda jobs, errors: None, client=None):
    owned = client is None
    client = client or PublicClient()
    jobs, errors, seen = [], [], set()
    try:
        for search_no, query in enumerate(config['searches'], 1):
            start, found = 0, 0
            for _ in range(config['maxPagesPerSearch']):
                html = client.get(SEARCH_PATH, dict(query, start=start))
                cards = parse_cards(html)
                if not cards:
                    # Empty guest fragments are normal at the end of pagination.
                    if html.strip() and not re.search(r'no (?:matching )?jobs|no results|0 jobs', html, re.I):
                        raise UpstreamError(f'Search {search_no}: unrecognized response; may be blocked')
                    break
                start += len(cards)
                for job in cards:
                    if job['id'] in seen:
                        continue
                    seen.add(job['id'])
                    jobs.append(job)
                    found += 1
                    if config['includeDescriptions']:
                        try:
                            detail = client.get('/jobs-guest/jobs/api/jobPosting/' + job['id'])
                            if not add_description(job, detail):
                                errors.append(f"Job {job['id']}: description missing")
                        except UpstreamError:
                            progress(jobs, errors)
                            raise
                    progress(jobs, errors)
                    if len(jobs) >= config['limit'] or found >= config['maxJobsPerSearch']:
                        break
                if len(jobs) >= config['limit'] or found >= config['maxJobsPerSearch']:
                    break
            if len(jobs) >= config['limit']:
                break
    except UpstreamError as exc:
        errors.append(str(exc))
    finally:
        if owned:
            client.session.close()
    progress(jobs, errors)
    return jobs, errors
