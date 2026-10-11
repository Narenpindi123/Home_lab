"""Public Dice and employer-board collectors. No paid transport or credentials."""
import html
import json
from pathlib import Path
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urljoin

import requests
from bs4 import BeautifulSoup

CONFIG = Path(__file__).with_name('local_job_sources.json')


def settings():
    return json.loads(CONFIG.read_text())


def configured_sources():
    from startup_job_sources import SOURCES
    return [{'name': name, 'enabled': True} for name in
            ['Dice', 'Employer boards', *SOURCES.values()]]


def normalize_input(source, body):
    if not isinstance(body, dict):
        raise ValueError('Input must be an object')
    config = dict(settings()[source])
    allowed = set(config)
    if set(body) - allowed:
        raise ValueError('Unsupported input: ' + ', '.join(sorted(set(body) - allowed)))
    config.update(body)
    for name, low, high in [('limit', 1, 1000), ('maxPagesPerSearch', 1, 10),
                             ('maxResultsPerQuery', 1, 100), ('postedWithinDays', 1, 30)]:
        if name in config and (type(config[name]) is not int or not low <= config[name] <= high):
            raise ValueError(f'{name} must be an integer from {low} to {high}')
    key = 'searchQueries' if source == 'dice' else 'keywords'
    values = config.get(key, [])
    if not isinstance(values, list) or not 1 <= len(values) <= 64 or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in values):
        raise ValueError(f'{key} must contain 1–64 search strings')
    if source == 'ats':
        targets = config['targets']
        if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
            raise ValueError('Provide 1–100 employer targets')
        for target in targets:
            if (not isinstance(target, dict) or set(target) - {'ats', 'slug', 'companyName'}
                    or target.get('ats') not in ('greenhouse', 'lever', 'ashby')
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(target.get('slug', '')))):
                raise ValueError('Targets need a supported ATS and a board slug')
    return config


def text(value):
    if not isinstance(value, str):
        return ''
    value = html.unescape(value)
    return BeautifulSoup(value, 'html.parser').get_text(' ', strip=True) if '<' in value else value.strip()


def safe_url(value, hosts=None):
    try:
        p = urlsplit(value)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
            return ''
        if hosts and p.hostname not in hosts:
            return ''
        return value
    except (ValueError, TypeError):
        return ''


def request(url, params=None):
    with requests.get(url, params=params, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US,en;q=0.9'},
                      timeout=(10, 35), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise ValueError(f'HTTP {response.status_code}')
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 20_000_000:
                raise ValueError('Response exceeds 20 MB limit')
            chunks.append(chunk)
        return b''.join(chunks).decode('utf-8', errors='replace')


def record(title, company, url, location, description, published, contract, source, **extra):
    url = safe_url(url)
    if not title or not url:
        return None
    return dict(jobTitle=text(title), companyName=text(company), jobUrl=url, applyUrl=url,
                location=text(location), jobDescription=text(description), publishedAt=published or '',
                postedTime=published or 'Unknown', contractType=text(contract), _source=source, **extra)


def old(job, days):
    try:
        value = datetime.fromisoformat(job['publishedAt'].replace('Z', '+00:00'))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value < datetime.now(timezone.utc) - timedelta(days=days)
    except (ValueError, TypeError):
        return False  # Unknown publication dates remain explicit for matcher review.


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def posting_detail(page, url, source):
    soup = BeautifulSoup(page, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except ValueError:
            continue
        for item in walk(data):
            types = item.get('@type', [])
            if not (types == 'JobPosting' or isinstance(types, list) and 'JobPosting' in types):
                continue
            company = item.get('hiringOrganization') or {}
            locations = item.get('jobLocation') or []
            if not isinstance(locations, list):
                locations = [locations]
            parts = []
            for location in locations:
                address = location.get('address', {}) if isinstance(location, dict) else {}
                if isinstance(address, dict):
                    country = address.get('addressCountry', '')
                    if isinstance(country, dict):
                        country = country.get('name', '')
                    parts.append(', '.join(str(x) for x in [address.get('addressLocality'), address.get('addressRegion'), country] if x))
            if item.get('jobLocationType') == 'TELECOMMUTE':
                parts.insert(0, 'Remote')
                regions = item.get('applicantLocationRequirements') or []
                if isinstance(regions, dict):
                    regions = [regions]
                parts.extend(x.get('name', '') for x in regions if isinstance(x, dict))
            contract = item.get('employmentType') or ''
            return record(item.get('title'), company.get('name', '') if isinstance(company, dict) else company,
                          url, '; '.join(parts), item.get('description'), item.get('datePosted'),
                          ', '.join(contract) if isinstance(contract, list) else contract, source)
    raise ValueError('No public JobPosting structured data')


def dice_detail(page, url):
    return posting_detail(page, url, 'Dice')


def scrape_dice(config, progress):
    jobs, errors, seen = [], [], set()
    for query in config['searchQueries']:
        count = 0
        for page in range(1, config['maxPagesPerSearch'] + 1):
            try:
                markup = request('https://www.dice.com/jobs', {'q': query, 'countryCode': 'US', 'page': page,
                    'pageSize': 20, 'language': 'en',
                    'filters.postedDate': 'SEVEN' if config['postedWithinDays'] <= 7 else None, 'sortBy': 'date'})
                soup = BeautifulSoup(markup, 'html.parser')
                links = list(dict.fromkeys(urljoin('https://www.dice.com', a['href']) for a in soup.select('a[href*="/job-detail/"]')))
                links = [u for u in links if safe_url(u, {'www.dice.com'}) and re.fullmatch(r'/job-detail/[a-zA-Z0-9-]+', urlsplit(u).path)]
                if not links:
                    if not re.search(r'no (?:matching )?(?:jobs|results)|0 jobs', soup.get_text(' ', strip=True), re.I):
                        errors.append(f'{query}: unrecognized search page')
                    break
                new = [u for u in links if u not in seen]
                if not new:
                    break
                for url in new:
                    seen.add(url)
                    try:
                        time.sleep(.7)
                        job = dice_detail(request(url), url)
                        if job and not old(job, config['postedWithinDays']):
                            jobs.append(job)
                            count += 1
                            progress(jobs, errors)
                    except (requests.RequestException, ValueError) as exc:
                        errors.append(f'Dice detail: {type(exc).__name__}: {exc}')
                        if 'HTTP 403' in str(exc) or 'HTTP 429' in str(exc):
                            return jobs, errors
                    if len(jobs) >= config['limit'] or count >= config['maxResultsPerQuery']:
                        break
                if len(jobs) >= config['limit'] or count >= config['maxResultsPerQuery']:
                    break
            except (requests.RequestException, ValueError) as exc:
                errors.append(f'{query}: {type(exc).__name__}: {exc}')
                if 'HTTP 403' in str(exc) or 'HTTP 429' in str(exc):
                    return jobs, errors
                break
        if len(jobs) >= config['limit']:
            break
    return jobs, errors


def board_jobs(target):
    ats, slug = target['ats'], target['slug']
    company = target.get('companyName') or slug
    if ats == 'greenhouse':
        data = json.loads(request(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs', {'content': 'true'}))
        items = data['jobs']
    elif ats == 'ashby':
        data = json.loads(request(f'https://api.ashbyhq.com/posting-api/job-board/{slug}'))
        items = data['jobs']
    else:
        items = []
        for skip in range(0, 2000, 100):
            batch = json.loads(request(f'https://api.lever.co/v0/postings/{slug}', {'mode': 'json', 'limit': 100, 'skip': skip}))
            if not isinstance(batch, list):
                raise ValueError('Unexpected Lever response')
            items.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ValueError('Lever pagination exceeded 2000 jobs; narrow the board')
    if not isinstance(items, list):
        raise ValueError('Unexpected board response')
    jobs = []
    for item in items:
        if ats == 'greenhouse':
            job = record(item.get('title'), item.get('company_name') or company, item.get('absolute_url'),
                         (item.get('location') or {}).get('name', ''), item.get('content'),
                         item.get('first_published'), '', 'Employer boards')
        elif ats == 'ashby':
            if item.get('isListed') is False:
                continue
            location = item.get('location') or ''
            country = (item.get('address') or {}).get('postalAddress', {}).get('addressCountry', '')
            job = record(item.get('title'), company, item.get('jobUrl'), ', '.join(filter(None, [location, country])),
                         item.get('descriptionHtml') or item.get('descriptionPlain'), item.get('publishedAt'),
                         item.get('employmentType'), 'Employer boards')
        else:
            categories = item.get('categories') or {}
            description = '\n'.join([item.get('description', ''),
                *[str(x.get('text', '')) + '\n' + str(x.get('content', '')) for x in item.get('lists', [])], item.get('additional', '')])
            created = item.get('createdAt')
            published = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat() if isinstance(created, (int, float)) else ''
            job = record(item.get('text'), company, item.get('hostedUrl'),
                         ', '.join(filter(None, [categories.get('location', ''), item.get('country', '')])),
                         description, published, categories.get('commitment'), 'Employer boards')
        if job:
            job.update(_ats_platform=ats, _ats_board=slug)
            jobs.append(job)
    return jobs


def scrape_ats(config, progress):
    jobs, errors, seen = [], [], set()
    def fetch(target):
        try:
            return board_jobs(target), None
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            return [], f"{target['ats']}/{target['slug']}: {type(exc).__name__}: {exc}"
    with ThreadPoolExecutor(max_workers=4) as pool:
        for found, error in pool.map(fetch, config['targets']):
            if error:
                errors.append(error)
            for job in found:
                haystack = (job['jobTitle'] + ' ' + job['jobDescription']).lower()
                if (job['jobUrl'] in seen or old(job, config['postedWithinDays'])
                        or not any(k.lower() in haystack for k in config['keywords'])):
                    continue
                seen.add(job['jobUrl'])
                jobs.append(job)
            jobs.sort(key=lambda j: j.get('publishedAt', ''), reverse=True)
            progress(jobs[:config['limit']], errors)
    return jobs[:config['limit']], errors


def scrape_source(source, config, progress):
    if source == 'dice':
        jobs, errors = scrape_dice(config, progress)
    elif source == 'ats':
        jobs, errors = scrape_ats(config, progress)
    else:
        import ziprecruiter_scraper
        ziprecruiter_scraper.QUERIES = tuple(config['keywords'])
        ziprecruiter_scraper.MAX_JOBS = config['limit']
        jobs = ziprecruiter_scraper.scrape_jobs()
        errors = ziprecruiter_scraper.LAST_HEALTH['errors']
    progress(jobs, errors)
    return jobs, errors
