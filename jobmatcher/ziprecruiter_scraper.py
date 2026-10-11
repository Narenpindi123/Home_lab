"""Bounded public ZipRecruiter discovery through local Chromium, without Apify.

Reads JobPosting structured data and follows public listing links. Challenges
stop collection and are reported; this collector never signs in or applies.
"""
import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote

import requests

from bs4 import BeautifulSoup

LAST_HEALTH = {"errors": [], "status": "unknown"}
QUERIES = (
    "SOC Analyst", "Cybersecurity Analyst", "GRC Analyst", "IT Risk Analyst",
    "IAM Analyst", "Vulnerability Management Analyst", "Third Party Risk Analyst",
    "Technology Controls Analyst", "IT Support Specialist", "Help Desk Analyst",
    "NOC Analyst", "Network Support Specialist",
)
MAX_JOBS = 200
MAX_DETAILS_PER_SEARCH = 20
RUN_TIMEOUT = 600


def enabled():
    return os.environ.get('ZIPRECRUITER_BROWSER_ENABLED', 'true').lower() == 'true'


def listing_url(value, base='https://www.ziprecruiter.com/'):
    if not isinstance(value, str) or not value.strip():
        return ''
    try:
        parts = urlsplit(urljoin(base, value))
        if (parts.scheme != 'https' or parts.hostname not in
                ('ziprecruiter.com', 'www.ziprecruiter.com') or
                parts.username or parts.password or parts.port not in (None, 443)):
            return ''
        query = [(k, v) for k, v in parse_qsl(parts.query) if k == 'jid']
        if not query and not parts.path.startswith(('/c/', '/jobs/')):
            return ''
        return urlunsplit(('https', 'www.ziprecruiter.com', parts.path, urlencode(query), ''))
    except ValueError:
        return ''


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _text(value):
    return BeautifulSoup(value, 'html.parser').get_text(' ', strip=True) if isinstance(value, str) else ''


def normalize_posting(item, page_url):
    url = listing_url(item.get('url')) or listing_url(page_url)
    title = _text(item.get('title'))
    if not url or not title:
        return None
    company = item.get('hiringOrganization', {})
    company = company.get('name', '') if isinstance(company, dict) else company
    locations = item.get('jobLocation', [])
    if not isinstance(locations, list):
        locations = [locations]
    location_parts = []
    for location in locations:
        address = location.get('address', {}) if isinstance(location, dict) else {}
        if isinstance(address, dict):
            country = address.get('addressCountry', '')
            if isinstance(country, dict):
                country = country.get('name', '')
            location_parts.append(', '.join(filter(None, [
                _text(address.get('addressLocality')), _text(address.get('addressRegion')), _text(country)])))
        elif isinstance(address, str):
            location_parts.append(address)
    if item.get('jobLocationType') == 'TELECOMMUTE':
        location_parts.insert(0, 'Remote')
        requirements = item.get('applicantLocationRequirements', [])
        if not isinstance(requirements, list):
            requirements = [requirements]
        location_parts.extend(_text(r.get('name')) for r in requirements if isinstance(r, dict))
    published = _text(item.get('datePosted'))
    contract = item.get('employmentType', '')
    if isinstance(contract, list):
        contract = ', '.join(v for v in contract if isinstance(v, str))
    return dict(jobTitle=title, companyName=_text(company), jobUrl=url, applyUrl=url,
                jobDescription=_text(item.get('description')), location='; '.join(filter(None, location_parts)),
                publishedAt=published, postedTime=published or 'Unknown',
                contractType=_text(contract), _source='ZipRecruiter')


def parse_page(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    # Ignore script strings so an unused challenge library is not a false block.
    for node in soup(['script', 'style']):
        if node.get('type') != 'application/ld+json':
            node.decompose()
    visible = soup.get_text(' ', strip=True).lower()
    if any(marker in visible for marker in (
            'verify you are human', 'verify that you are human', 'access denied',
            'unusual traffic', 'security verification', 'just a moment...',
            'please complete the captcha', 'press & hold', 'press and hold')):
        raise RuntimeError('ZipRecruiter blocked the browser or requires human verification')
    jobs = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for item in _walk(data):
            types = item.get('@type', [])
            if isinstance(types, str):
                types = [types]
            if 'JobPosting' in types:
                job = normalize_posting(item, page_url)
                if job:
                    jobs[job['jobUrl']] = job
    links = list(dict.fromkeys(url for a in soup.select('a[href]')
                              if (url := listing_url(a.get('href'), page_url))))
    empty = any(marker in visible for marker in (
        'no jobs found', 'no matching jobs', 'no results found', '0 jobs found'))
    return list(jobs.values()), links, empty


async def _read_page(browser, url):
    tab = await asyncio.wait_for(browser.get(url, new_tab=True), timeout=30)
    try:
        # Allow client-rendered public data to appear, with a bounded wait.
        for attempt in range(4):
            await asyncio.sleep(2)
            html = await asyncio.wait_for(tab.get_content(), timeout=15)
            parsed = parse_page(html, url)
            if parsed[0] or parsed[1] or parsed[2]:
                return parsed
        raise ValueError('Unrecognized page: no job data or explicit empty results')
    finally:
        await tab.close()


async def _collect(jobs, errors):
    import browser_scraper
    browser = None
    visited = set()
    try:
        # Temporary profile keeps this source separate from LinkedIn sessions.
        browser = await asyncio.wait_for(browser_scraper._create_browser(anonymous=True), timeout=45)
        for query in QUERIES:
            url = 'https://www.ziprecruiter.com/jobs-search?' + urlencode(
                {'search': query, 'location': 'United States'})
            try:
                found, links, _ = await _read_page(browser, url)
                for job in found:
                    jobs[job['jobUrl']] = job
                needed = [u for u in links if u not in visited and
                          len(jobs.get(u, {}).get('jobDescription', '')) <= 20]
                for link in needed[:MAX_DETAILS_PER_SEARCH]:
                    if len(jobs) >= MAX_JOBS:
                        break
                    visited.add(link)
                    try:
                        details, _, _ = await _read_page(browser, link)
                        if not details:
                            errors.append('Listing page has no readable JobPosting data')
                        for job in details:
                            jobs[job['jobUrl']] = job
                    except RuntimeError:
                        raise
                    except Exception as exc:
                        errors.append(f'Listing: {type(exc).__name__}: {exc}')
                if len(jobs) >= MAX_JOBS:
                    break
            except RuntimeError as exc:
                errors.append(str(exc))
                break  # Do not repeatedly hit a challenge.
            except Exception as exc:
                errors.append(f'Search {query}: {type(exc).__name__}: {exc}')
    finally:
        if browser:
            browser.stop()


async def _scrape():
    jobs, errors = {}, []
    try:
        await asyncio.wait_for(_collect(jobs, errors), timeout=RUN_TIMEOUT)
    except asyncio.TimeoutError:
        errors.append('Browser collection exceeded the ten-minute limit')
    except Exception as exc:
        errors.append(f'Browser: {type(exc).__name__}: {exc}')
    results = list(jobs.values())[:MAX_JOBS]
    missing = sum(len(j['jobDescription']) <= 20 for j in results)
    if missing:
        errors.append(f'{missing} jobs have missing descriptions')
    LAST_HEALTH.update(errors=list(dict.fromkeys(errors)),
                       status='partial' if results and errors else 'error' if errors else 'success')
    return results


def scrape_jobs():
    if not enabled():
        LAST_HEALTH.update(errors=[], status='disabled')
        return []
    LAST_HEALTH.update(errors=[], status='unknown')
    jobs = asyncio.run(_scrape())
    if LAST_HEALTH['status'] in ('error', 'partial'):
        errors = list(LAST_HEALTH['errors'])
        indexed, search_errors = cached_search_index_jobs()
        # Prefer complete browser records over index-only leads.
        combined = {job['jobUrl']: job for job in jobs}
        for job in indexed:
            combined.setdefault(job['jobUrl'], job)
        jobs = list(combined.values())[:MAX_JOBS]
        errors.extend(search_errors)
        if any(j.get('_description_status') == 'search_snippet_only' for j in jobs):
            errors.append('Search-index discovery only: full descriptions, posting dates and availability need review')
        LAST_HEALTH.update(status='partial' if jobs else 'error', errors=list(dict.fromkeys(errors)))
    return jobs


def normalize_search_result(item):
    """Discover links without fetching challenged pages or treating snippets as JDs."""
    if not isinstance(item, dict):
        return None
    url = listing_url(item.get('url'))
    if not url:
        return None
    match = re.fullmatch(r'/c/([^/]+)/Job/([^/]+)(?:/-in-([^/]+))?/?', urlsplit(url).path)
    if not match or not dict(parse_qsl(urlsplit(url).query)).get('jid'):
        return None  # Exclude category, salary and search landing pages.
    company, title, location = match.groups()
    def label(value):
        return unquote(value or '').replace('-', ' ').strip()
    return dict(jobTitle=label(title), companyName=label(company), location=label(location),
                jobUrl=url, applyUrl=url, jobDescription='', publishedAt='', postedTime='Unknown',
                contractType='', _source='ZipRecruiter', _description_status='search_snippet_only',
                _discovery_method='public_search_index',
                _search_title=_text(item.get('title')),
                _search_snippet=_text(item.get('content'))[:2000])


def search_index_jobs(session=None):
    """Use the configured SearXNG service; never retry a rate limit or fetch a listing."""
    endpoint = os.environ.get('SEARCH_API_URL', 'http://127.0.0.1:8888/search')
    jobs, errors = {}, []
    own_session = session is None
    session = session or requests.Session()
    try:
        for query in QUERIES:
            try:
                response = session.get(endpoint, params={
                    'q': f'site:ziprecruiter.com/c/ "{query}"', 'format': 'json',
                    'language': 'en-US', 'safesearch': 1, 'time_range': 'week'},
                    headers={'Accept': 'application/json'}, timeout=(4, 20))
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
                    raise ValueError('Invalid search response')
                for item in payload['results']:
                    job = normalize_search_result(item)
                    if job:
                        jobs.setdefault(job['jobUrl'], job)
                    if len(jobs) >= MAX_JOBS:
                        break
                unavailable = payload.get('unresponsive_engines') or []
                if not payload['results'] and unavailable:
                    errors.append('Search providers unavailable or rate-limited; collection stopped')
                    break
                if len(jobs) >= MAX_JOBS:
                    break
            except (requests.RequestException, ValueError) as exc:
                # Do not log request URLs, headers or bodies.
                errors.append(f'Search-index discovery failed: {type(exc).__name__}')
                break
    finally:
        if own_session:
            session.close()
    if not jobs and not errors:
        errors.append('Search index returned no individual ZipRecruiter listings')
    return list(jobs.values()), errors


def cached_search_index_jobs():
    """Reuse discoveries for six hours; retain dated fallback leads for at most a day."""
    directory = Path(os.environ.get('DB_PATH', str(Path(__file__).parent / 'data' / 'processed_jobs.db'))).parent
    path = directory / 'ziprecruiter_search_cache.json'
    now = datetime.now(timezone.utc)
    cached, age, stamp, cached_errors = [], float('inf'), '', []
    try:
        payload = json.loads(path.read_text())
        stamp = payload['scraped_at']
        age = (now - datetime.fromisoformat(stamp)).total_seconds()
        if not 0 <= age <= 86400:
            raise ValueError('Cache expired')
        cached_errors = [e for e in payload.get('errors', []) if isinstance(e, str)]
        for item in payload['jobs'][:MAX_JOBS]:
            job = normalize_search_result({'url': item.get('jobUrl'),
                'title': item.get('_search_title'), 'content': item.get('_search_snippet')})
            if job:
                job['_discovered_at'] = stamp
                cached.append(job)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        cached = []
    cache_note = f'Using cached search-index leads discovered at {stamp}; availability needs verification'
    if cached and age < 21600:
        return cached, cached_errors + [cache_note]
    jobs, errors = search_index_jobs()
    if jobs:
        stamp = now.isoformat()
        for job in jobs:
            job['_discovered_at'] = stamp
        temporary = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.ziprecruiter-', delete=False) as handle:
                temporary = handle.name
                json.dump(dict(scraped_at=stamp, jobs=jobs, errors=errors), handle)
            os.replace(temporary, path)
        except OSError:
            errors.append('Could not save search-index cache')
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return jobs, errors
    if cached:
        return cached, errors + cached_errors + [cache_note]
    return [], errors
