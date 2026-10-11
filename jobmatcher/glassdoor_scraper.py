#!/usr/bin/env python3
"""
Host-native Glassdoor scraper.
Runs outside Docker using the real google-chrome binary + nodriver to bypass
Cloudflare Turnstile / DataDome anti-bot protections.

Writes scraped jobs to data/glassdoor_jobs.json for the Docker pipeline to consume.
"""

import os
import sys
import json
import asyncio
import random
import re
import time

LAST_HEALTH = {"errors": [], "status": "unknown"}
from time_utils import local_now
from pathlib import Path

import sys
def quiet_unraisable(unraisable):
    if unraisable.exc_type == RuntimeError and "Event loop is closed" in str(unraisable.exc_value):
        return
    sys.__unraisablehook__(unraisable)
sys.unraisablehook = quiet_unraisable

import nodriver as uc
from bs4 import BeautifulSoup

# ── Configuration ──────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CHROME_PROFILE = os.environ.get("GLASSDOOR_PROFILE_DIR", str(SCRIPT_DIR / "chrome-profile"))
OUTPUT_FILE = os.environ.get("GLASSDOOR_OUTPUT_FILE", str(SCRIPT_DIR / "data" / "glassdoor_jobs.json"))

GLASSDOOR_QUERIES = [
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Cybersecurity+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=SOC+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Information+Security+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=IT+GRC+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Penetration+Tester&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=IT+Support+Specialist&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Help+Desk+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Security+Operations+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=IT+Security+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=IAM+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Vulnerability+Management+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Third+Party+Risk+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Technology+Controls+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=NOC+Analyst&fromAge=7",
    "https://www.glassdoor.com/Job/jobs.htm?sc.keyword=Network+Support+Specialist&fromAge=7",
]

SENIOR_KEYWORDS = ["senior", "lead", "manager", "director", "principal", "staff", "vp"]


# ── Browser Helpers ────────────────────────────────────────────────────────────

def _find_xauthority() -> str | None:
    """Return the XAUTHORITY path for the current DISPLAY by inspecting the Xvfb process."""
    import subprocess
    display = os.environ.get("DISPLAY", ":0")
    display_num = display.lstrip(":").split(".")[0]
    try:
        out = subprocess.check_output(["pgrep", "-a", "Xvfb"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if f":{display_num} " in line or line.rstrip().endswith(f":{display_num}"):
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == "-auth" and i + 1 < len(parts):
                        return parts[i + 1]
    except Exception:
        pass
    return None


async def create_browser():
    """Launch google-chrome with the persistent profile."""
    if not os.environ.get("XAUTHORITY"):
        auth = _find_xauthority()
        if auth:
            os.environ["XAUTHORITY"] = auth

    config = uc.Config()
    config.headless = False          # nodriver manages this; xvfb provides display
    config.no_sandbox = True
    if os.environ.get("CHROME_PATH"):
        config.browser_executable_path = os.environ["CHROME_PATH"]
    config.user_data_dir = CHROME_PROFILE
    config.add_argument("--disable-dev-shm-usage")
    config.add_argument("--disable-gpu")
    config.add_argument("--disable-software-rasterizer")
    config.add_argument("--window-size=1920,1080")
    config.add_argument("--blink-settings=imagesEnabled=false")
    return await uc.start(config)


async def random_delay(lo=2, hi=4):
    await asyncio.sleep(random.uniform(lo, hi))


async def scroll_page(tab, times=3, distance=600):
    for _ in range(times):
        try:
            await tab.evaluate(f"window.scrollBy(0, {distance});")
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.8, 1.5))


async def safe_get_content(tab, retries=3):
    for attempt in range(retries):
        try:
            content = await asyncio.wait_for(tab.get_content(), timeout=10)
            if content and len(content) > 500:
                return content
        except Exception:
            pass
        await asyncio.sleep(1)
    return None


async def goto(tab, url, wait=5):
    try:
        await tab.get(url)
        await asyncio.sleep(wait)
        return True
    except Exception as e:
        print(f"  ⚠️  Navigation failed: {e}")
        return False


def description_url(job_url):
    """Use Glassdoor's stable listing-id route instead of brittle SEO slugs."""
    match = re.search(r'[?&]jl=(\d+)', job_url or '')
    return f"https://www.glassdoor.com/job-listing/j?jl={match.group(1)}" if match else job_url


def listing_id(job_url):
    match = re.search(r'[?&]jl=(\d+)', job_url or '')
    return match.group(1) if match else ''


def parse_job_details(payload, expected_id):
    """Never attach a stale pane or another listing's description to this job."""
    if not isinstance(payload, dict) or str(payload.get('listingId')) != str(expected_id):
        raise ValueError('Glassdoor detail listing ID mismatch')
    description = payload.get('jobDescription') or ''
    if not isinstance(description, str):
        raise ValueError('Glassdoor detail description is not text')
    return BeautifulSoup(description, 'lxml').get_text(separator="\n", strip=True)


def search_page_descriptions(soup):
    """Read only full detail panes carrying their own listing identity."""
    descriptions = {}
    for node in soup.select('[class*="JobDetails_jobDescription"]'):
        identity = node.get('data-brandviews', '') or node.parent.get('data-brandviews', '')
        match = re.search(r'jlid=(\d+)', identity)
        text = node.get_text(separator='\n', strip=True)
        if match and len(text) > 20:
            descriptions[match.group(1)] = text
    return descriptions


def load_description_cache():
    """Reuse at most 24-hour-old full descriptions without refreshing their age."""
    from time_utils import parse_scraped_at, scrape_is_fresh
    descriptions, timestamps = {}, {}
    try:
        snapshot = json.loads(Path(OUTPUT_FILE).read_text())
        for job in snapshot.get('jobs', []):
            stamp = job.get('_description_fetched_at') or snapshot['scraped_at']
            job_id = listing_id(job.get('jobUrl', ''))
            description = job.get('jobDescription') or ''
            if job_id and len(description) > 20 and scrape_is_fresh(parse_scraped_at(stamp)):
                descriptions[job_id], timestamps[job_id] = description, stamp
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return descriptions, timestamps


class GlassdoorThrottleError(RuntimeError):
    """The source requires a cooldown before any more detail requests."""


async def fetch_job_description(tab, job_url):
    job_id = listing_id(job_url)
    if not job_id:
        raise ValueError('Glassdoor listing has no job ID')
    # Use the same public endpoint and parameters as the search-page client.
    # Keep the search page open; standalone SEO detail pages can be challenged.
    params = json.dumps(dict(jobListingId=job_id, pageTypeEnum='SERP',
                             queryString=job_url, countryId='1'))
    script = """(async () => {
        const params = new URLSearchParams(PARAMS);
        const response = await fetch('/job-listing/api/job-details?' + params,
                                     {signal: AbortSignal.timeout(15000)});
        return JSON.stringify({status: response.status,
                               retryAfter: response.headers.get("Retry-After"),
                               data: response.ok ? await response.json() : null});
    })()""".replace('PARAMS', params)
    for attempt in range(3):
        raw = await asyncio.wait_for(tab.evaluate(script, await_promise=True), timeout=20)
        raw = raw.value if hasattr(raw, 'value') else raw
        result = json.loads(raw)
        if result['status'] == 200:
            return parse_job_details(result['data'], job_id)
        if result['status'] == 429 and attempt < 2:
            try:
                delay = max(60, float(result.get('retryAfter') or 60))
            except (TypeError, ValueError):
                delay = 60
            # Long server cooldowns are left for the next scheduled run.
            if delay > 300:
                raise GlassdoorThrottleError(f'Glassdoor rate limited; retry after {delay:.0f}s')
            print(f'      ⏳ Glassdoor rate limit; waiting {delay:.0f}s', flush=True)
            await asyncio.sleep(delay)
            continue
        if result['status'] == 429:
            raise GlassdoorThrottleError('Glassdoor detail API returned HTTP 429 after bounded retries')
        raise RuntimeError(f"Glassdoor detail API returned HTTP {result['status']}")



# ── Scraping Logic ─────────────────────────────────────────────────────────────

async def scrape_glassdoor():
    """Scrape Glassdoor job listings and return a list of job dicts."""
    print("🕷️  Glassdoor Host-Native Scraper starting...")
    print(f"   Chrome profile: {CHROME_PROFILE}")

    browser = await create_browser()
    tab = await browser.get("about:blank")
    await asyncio.sleep(2)

    LAST_HEALTH.update(errors=[], status="unknown")
    all_jobs = []
    seen_urls = set()
    description_access_blocked = False
    descriptions_by_id, description_timestamps = load_description_cache()

    for i, url in enumerate(GLASSDOOR_QUERIES):
        try:
            print(f"\n  📋 Search {i+1}/{len(GLASSDOOR_QUERIES)}: {url.split('keyword=')[1].split('&')[0]}")
            ok = await goto(tab, url, wait=6)
            if not ok:
                continue

            # Remove login modals if they pop up
            try:
                await tab.evaluate("""
                    const modal = document.querySelector('#LoginModal') || document.querySelector('.LoginModal');
                    if(modal) modal.remove();
                    document.body.style.overflow = 'auto';
                """)
            except Exception:
                pass

            await scroll_page(tab, times=8, distance=700)
            html = await safe_get_content(tab)
            if not html:
                LAST_HEALTH["errors"].append(f"Search {i+1}: empty page")
                print("    ❌ Could not get page content")
                continue

            # Check if we hit a challenge page
            if "Just a moment..." in html[:500] or "Help Us Protect Glassdoor" in html[:2000]:
                print("    ⚠️  Cloudflare challenge detected, waiting 10s and retrying...")
                await asyncio.sleep(10)
                html = await safe_get_content(tab)
                if not html or "Just a moment..." in html[:500]:
                    LAST_HEALTH["errors"].append(f"Search {i+1}: blocked page")
                    print("    ❌ Still blocked by Cloudflare, skipping this query")
                    continue

            soup = BeautifulSoup(html, "lxml")
            page_descriptions = search_page_descriptions(soup)
            descriptions_by_id.update(page_descriptions)
            description_timestamps.update({key: local_now().isoformat() for key in page_descriptions})

            # Try multiple selector strategies
            cards = soup.select("li[data-test='jobListing']")
            if not cards:
                # Fallback: look for job card containers
                cards = soup.select("li[class*='JobsList_jobListItem']")
            if not cards:
                cards = soup.select("ul[data-test='jlGrid'] > li")

            if not cards and not re.search(r"no (?:matching )?jobs|0 jobs|no results", soup.get_text(), re.I):
                LAST_HEALTH["errors"].append(f"Search {i+1}: unrecognized page")
            page_jobs = []
            for card in cards:
                try:
                    # Extract title
                    title_el = (
                        card.select_one("a[data-test='job-title']") or
                        card.select_one("a[class*='JobCard_jobTitle']") or
                        card.select_one("a[class*='jobTitle']")
                    )
                    if not title_el:
                        continue

                    title = title_el.get_text(strip=True)

                    link = title_el.get("href", "")
                    if link.startswith("/"):
                        link = "https://www.glassdoor.com" + link

                    # Extract company
                    company_el = (
                        card.select_one("span[class*='EmployerProfile_employerName']") or
                        card.select_one("div[class*='EmployerProfile']") or
                        card.select_one("span[class*='employer']")
                    )
                    company = company_el.get_text(strip=True) if company_el else "Unknown"
                    company = re.sub(r'\d\.\d$', '', company).strip()

                    # Extract location
                    location_el = card.select_one("div[data-test='emp-location']")
                    location = location_el.get_text(strip=True) if location_el else "Not specified"

                    age_el = card.select_one("[data-test='job-age']") or card.select_one("[class*='JobCard_listingAge']")
                    age_text = age_el.get_text(strip=True) if age_el else "Unknown"
                    posted = ""
                    age_match = re.fullmatch(r"(\d+)\s*([dh])(?:[a-z]*)(?: ago)?", age_text, re.I)
                    if age_match:
                        from datetime import timedelta
                        delta = timedelta(days=int(age_match[1])) if age_match[2].lower() == 'd' else timedelta(hours=int(age_match[1]))
                        posted = (local_now() - delta).isoformat()
                    page_jobs.append({
                        "jobTitle": title,
                        "companyName": company,
                        "jobDescription": "",
                        "location": location,
                        "jobUrl": link,
                        "applyUrl": link,
                        "publishedAt": posted,
                        "postedTime": age_text,
                        "contractType": "Unknown",
                        "_source": "Glassdoor",
                    })
                except Exception:
                    continue


            print(f"    ✅ Found {len(page_jobs)} jobs")

            # Fetch identified details without navigating away from search.
            desc_success = 0
            fetch_limit = len(page_jobs)
            print(f"    🔍 Fetching descriptions for {fetch_limit} jobs via search detail API...")
            consecutive_missing = 0
            for idx, job in enumerate(page_jobs):
                job_id = listing_id(job.get('jobUrl', ''))
                if job_id in descriptions_by_id:
                    job['jobDescription'] = descriptions_by_id[job_id]
                    job['_description_fetched_at'] = description_timestamps[job_id]
                    desc_success += 1
                    continue
                if description_access_blocked:
                    continue
                try:
                    desc = await fetch_job_description(tab, job['jobUrl'])
                    if len(desc) > 20:
                        job['jobDescription'] = desc
                        descriptions_by_id[job_id] = desc
                        job['_description_fetched_at'] = local_now().isoformat()
                        description_timestamps[job_id] = job['_description_fetched_at']
                        desc_success += 1
                        consecutive_missing = 0
                        print(f"      ✅ [{idx+1}] {len(desc)} chars")
                    else:
                        consecutive_missing += 1
                        print(f"      ⚠️ [{idx+1}] empty detail description")
                except GlassdoorThrottleError as exc:
                    description_access_blocked = True
                    LAST_HEALTH['errors'].append(str(exc))
                    print(f'    ⚠️ {exc}; leaving remaining details for the next run')
                except Exception as exc:
                    consecutive_missing += 1
                    # Exceptions include only HTTP status or validation diagnosis.
                    print(f"      ⚠️ [{idx+1}] detail request failed: {exc}")
                if consecutive_missing >= 5:
                    description_access_blocked = True
                    LAST_HEALTH['errors'].append('Detail API unavailable after 5 consecutive failed or empty responses')
                    print('    ⚠️ Stopping failed detail requests for this run')
                await random_delay(5.0, 7.0)

            print(f"    📋 Got {desc_success}/{fetch_limit} descriptions")

            # Filter un-wanted and dupes before appending to all_jobs
            count = 0
            for pj in page_jobs:
                if not pj["jobUrl"] or pj["jobUrl"] in seen_urls:
                    continue

                seen_urls.add(pj["jobUrl"])
                all_jobs.append(pj)
                count += 1

            print(f"    ✅ Added {count} valid jobs")
            # Preserve completed queries if a later query stalls or is throttled.
            LAST_HEALTH["status"] = "partial"
            save_jobs(all_jobs)

            await random_delay(3, 6)

        except Exception as e:
            LAST_HEALTH["errors"].append(f"Search {i+1}: {type(e).__name__}")
            print(f"    ❌ Search {i+1} failed: {e}")

    try:
        browser.stop()
    except Exception:
        pass
    missing = sum(len((job.get("jobDescription") or "").strip()) <= 20 for job in all_jobs)
    if missing:
        LAST_HEALTH["errors"].append(f"{missing} jobs have missing descriptions")
    LAST_HEALTH["status"] = "partial" if all_jobs and LAST_HEALTH["errors"] else "error" if LAST_HEALTH["errors"] else "success"
    return all_jobs


# ── Output ─────────────────────────────────────────────────────────────────────

def save_jobs(jobs):
    """Write jobs to JSON atomically."""
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    output = {
        "scraped_at": local_now().isoformat(),
        "count": len(jobs),
        "errors": LAST_HEALTH["errors"],
        "status": LAST_HEALTH["status"],
        "jobs": jobs,
    }

    # Atomic write: write to temp, then rename
    # A failed refresh must not erase the last usable feed.
    target_file = OUTPUT_FILE
    if not jobs and LAST_HEALTH.get("errors"):
        target_file = OUTPUT_FILE + ".last-attempt.json"
    tmp_file = target_file + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(output, f, indent=2)
    os.replace(tmp_file, target_file)
    from discovery_store import record_host_snapshot
    record_host_snapshot(target_file, 'Glassdoor', output)

    print(f"\n💾 Saved {len(jobs)} jobs to {OUTPUT_FILE}")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    start = time.time()
    try:
        jobs = await scrape_glassdoor()
    except Exception as e:
        LAST_HEALTH.update(errors=[f"Scraper: {type(e).__name__}"], status="error")
        print(f"⚠️  Error in main: {e}")
        jobs = []

    elapsed = time.time() - start

    print(f"\n📊 Glassdoor scrape complete: {len(jobs)} jobs in {elapsed:.1f}s")

    if jobs:
        with_desc = [j for j in jobs if len(j.get("jobDescription", "").strip()) > 20]
        print(f"  📋 {len(with_desc)}/{len(jobs)} jobs have descriptions")
        save_jobs(jobs)
        print(f"\n📝 Sample jobs:")
        for j in jobs[:5]:
            desc_len = len(j.get('jobDescription', ''))
            print(f"   • {j['jobTitle']} at {j['companyName']} ({j['location']}) [desc: {desc_len} chars]")
    else:
        print("⚠️  No jobs found — Glassdoor may still be blocking. Try running setup_glassdoor.py to refresh session.")
        save_jobs(jobs)

    return len(jobs)


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result > 0 else 1)
