"""
Browser-based job scraper using nodriver (undetected Chrome).
Replaces paid Apify API scrapers with free, local browser automation.

Sources:
  - LinkedIn (public job search + optional persistent login)
  - Indeed (handles CloudFlare automatically)
  - Google Jobs (best-effort, complex SPA)
"""

import os
import asyncio
import random
import re
from urllib.parse import parse_qs, urlsplit, urlencode

LAST_HEALTH = {"errors": [], "status": "unknown"}
import nodriver as uc
from bs4 import BeautifulSoup
from datetime import datetime

import sys
def quiet_unraisable(unraisable):
    if unraisable.exc_type == RuntimeError and "Event loop is closed" in str(unraisable.exc_value):
        return
    sys.__unraisablehook__(unraisable)
sys.unraisablehook = quiet_unraisable

# ── Configuration ──────────────────────────────────────────────────────────────
CHROME_PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", "/app/chrome-profile")
CHROME_PATH = os.environ.get("CHROME_PATH", "/usr/bin/chromium")
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"

# Rate limiting — randomized delays to mimic human behavior
MIN_DELAY = 2
MAX_DELAY = 4
PAGE_LOAD_WAIT = 2
# Bounded nationwide coverage: inspect deeper results without returning to
# the former 4,000-card sweeps that exceeded the six-hour cron window.
MAX_JOBS_PER_SEARCH = 60
MAX_PAGES_PER_SEARCH = 3


# ── Browser Lifecycle ──────────────────────────────────────────────────────────

async def _create_browser(anonymous=False):
    """Create a configured browser instance with anti-detection settings."""
    config = uc.Config()
    config.headless = HEADLESS
    config.no_sandbox = True
    if not anonymous:
        config.user_data_dir = CHROME_PROFILE_DIR
    config.browser_executable_path = CHROME_PATH
    config.add_argument("--disable-dev-shm-usage")
    config.add_argument("--disable-gpu")
    config.add_argument("--disable-software-rasterizer")
    config.add_argument("--disable-features=VizDisplayCompositor")
    config.add_argument("--disable-extensions")
    config.add_argument("--window-size=1920,1080")
    config.add_argument("--blink-settings=imagesEnabled=false")

    browser = await uc.start(config)
    return browser


async def _random_delay(min_s=None, max_s=None):
    """Human-like random delay between actions."""
    await asyncio.sleep(random.uniform(min_s or MIN_DELAY, max_s or MAX_DELAY))


async def _simulate_human(tab):
    """Simulate human interaction (scrolling and mouse movement) to bypass basic bot detectors."""
    try:
        # Move mouse via JS and Scroll
        for _ in range(3):
            x = random.randint(100, 800)
            y = random.randint(100, 800)

            # Simulate mouse movement via JS to avoid nodriver API errors
            js_mouse = f"""
                var ev = new MouseEvent('mousemove', {{
                    'view': window, 'bubbles': true, 'cancelable': true,
                    'clientX': {x}, 'clientY': {y}
                }});
                document.dispatchEvent(ev);
            """
            try:
                await tab.evaluate(js_mouse)
            except Exception:
                pass

            await asyncio.sleep(0.5)
            # Scroll down
            scroll_amt = random.randint(300, 800)
            try:
                await tab.evaluate(f"window.scrollBy(0, {scroll_amt});")
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.5, 1.5))
    except Exception as e:
        print(f"  ⚠️ Human simulation error: {e}")


async def _safe_get_content(tab, retries=3):
    """Get page HTML with strict timeouts to avoid nodriver hangs."""
    for attempt in range(retries):
        try:
            # Add strict timeout to prevent infinite freezing
            content = await asyncio.wait_for(tab.get_content(), timeout=10)
            if content:
                return content
        except asyncio.TimeoutError:
            print(f"  ⏳ Page content timed out attempt {attempt + 1}/{retries}...")
        except Exception as e:
            if attempt < retries - 1:
                print(f"  ⏳ Page content error {attempt + 1}/{retries}...")
                await asyncio.sleep(3)
            else:
                print(f"  ⚠️  Failed to get page content after {retries} attempts: {e}")
    return ""


async def _goto(tab, url, wait=None):
    """Navigate an existing tab to a new URL via JavaScript (avoids creating new tabs)."""
    try:
        import json
        js_url = json.dumps(url)  # Safe escaping for JS
        await tab.evaluate(f'window.location.href = {js_url}')
        await asyncio.sleep(wait or PAGE_LOAD_WAIT)
        return True
    except Exception as e:
        print(f"  ⚠️  Navigation failed for {url}: {e}")
        return False


async def _scroll_page(tab, times=3, distance=800, pause=0.5):
    """Scroll the page to trigger lazy-loading."""
    for _ in range(times):
        try:
            await tab.evaluate(f"window.scrollBy(0, {distance})")
        except Exception:
            pass
        await asyncio.sleep(pause)


# ── LinkedIn Scraper ───────────────────────────────────────────────────────────

LINKEDIN_SEARCH_URLS = [
    # Cybersecurity / IT Security
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22Cybersecurity%22%20OR%20%22IT%20Security%22%20OR%20%22Information%20Security%22&location=United%20States",
    # SOC Analyst / Cyber Security Operations / Incident Response
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22SOC%20Analyst%22%20OR%20%22Cyber%20Security%20Operations%22%20OR%20%22Cyber%20Incident%20Response%22%20OR%20%22Cyber%20Threat%20Analyst%22&location=United%20States",
    # Penetration Tester / Vulnerability / Security Engineer
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22Penetration%20Tester%22%20OR%20%22Vulnerability%20Analyst%22%20OR%20%22Cloud%20Security%20Engineer%22%20OR%20%22AppSec%22&location=United%20States",
    # IAM / IT GRC / Cyber Compliance / Risk
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22IT%20GRC%22%20OR%20%22Cyber%20Compliance%22%20OR%20%22IT%20Risk%20Analyst%22%20OR%20%22GRC%20Analyst%22%20OR%20%22Risk%20and%20Compliance%22&location=United%20States",
    # Network Security / SIEM / Security Analyst
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22Network%20Security%22%20OR%20%22Cloud%20Security%22%20OR%20%22Cyber%20Security%20Analyst%22%20OR%20%22SIEM%22&location=United%20States",
    # Digital Forensics / Threat Intelligence / Blue Team
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22Digital%20Forensics%22%20OR%20%22Threat%20Intelligence%22%20OR%20%22Blue%20Team%22%20OR%20%22Malware%20Analyst%22%20OR%20%22DFIR%22&location=United%20States",
    # IT Support / Help Desk / Systems Admin (entry-level IT pathway)
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22IT%20Support%22%20OR%20%22IT%20Help%20Desk%22%20OR%20%22Desktop%20Support%22%20OR%20%22Systems%20Administrator%22&location=United%20States",
    # IAM / identity governance / access administration
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22IAM%20Analyst%22%20OR%20%22Identity%20and%20Access%20Management%22%20OR%20%22Identity%20Governance%20Analyst%22%20OR%20%22Access%20Management%20Analyst%22&location=United%20States",
    # Controls / assurance / vendor risk / awareness
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22Technology%20Controls%20Analyst%22%20OR%20%22Security%20Assurance%20Analyst%22%20OR%20%22Third%20Party%20Risk%20Analyst%22%20OR%20%22Security%20Awareness%20Analyst%22&location=United%20States",
    # NOC / network, cloud, and technical support entry routes
    "https://www.linkedin.com/jobs/search/?f_E=2%2C3&f_TPR=r604800&sortBy=DD&keywords=%22NOC%20Analyst%22%20OR%20%22Network%20Support%20Specialist%22%20OR%20%22Cloud%20Support%20Associate%22%20OR%20%22Technical%20Support%20Engineer%22&location=United%20States",
]

# Focused entry routes also find postings whose employers omit experience-level
# tags. Existing searches retain their entry/associate filters.
for keywords in (
    '"junior security analyst" OR "SOC analyst I" OR "security operations analyst"',
    '"help desk technician" OR "service desk analyst" OR "desktop support technician"',
    '"NOC technician" OR "network operations technician" OR "data center technician"',
    '"junior GRC analyst" OR "IT audit associate" OR "access provisioning analyst"',
):
    LINKEDIN_SEARCH_URLS.append('https://www.linkedin.com/jobs/search/?' + urlencode({
        'keywords': keywords, 'location': 'United States',
        'f_TPR': 'r604800', 'sortBy': 'DD',
    }))


def linkedin_job_id(url):
    parsed = urlsplit(url or "")
    if parsed.hostname and not (parsed.hostname == "linkedin.com" or parsed.hostname.endswith(".linkedin.com")):
        return None
    match = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)(?:/|$)", parsed.path)
    return match.group(1) if match else parse_qs(parsed.query).get("currentJobId", [None])[0]


def canonical_linkedin_url(url):
    job_id = linkedin_job_id(url)
    return f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else url


def _parse_linkedin_card(card):
    """Parse a LinkedIn job card from HTML (handles both public and logged-in views)."""
    # Title — try multiple selectors
    title_el = (
        card.select_one("h3.base-search-card__title")
        or card.select_one("span.job-card-list__title")
        or card.select_one("a.job-card-container__link span")
        or card.select_one("[class*='job-title']")
        or card.select_one("h3")
    )
    title = title_el.get_text(strip=True) if title_el else None
    if not title:
        return None

    # Company
    company_el = (
        card.select_one("h4.base-search-card__subtitle a")
        or card.select_one("h4.base-search-card__subtitle")
        or card.select_one("span.job-card-container__primary-description")
        or card.select_one("[class*='company']")
        or card.select_one("h4")
    )
    company = company_el.get_text(strip=True) if company_el else "Unknown"

    # Location
    location_el = (
        card.select_one("span.job-search-card__location")
        or card.select_one("li.job-card-container__metadata-item")
        or card.select_one("[class*='location']")
    )
    location = location_el.get_text(strip=True) if location_el else "Not specified"

    # Link
    link_el = (
        card.select_one("a.base-card__full-link")
        or card.select_one("a.job-card-container__link")
        or card.select_one("a[href*='/jobs/view/']")
    )
    link = ""
    if link_el:
        link = canonical_linkedin_url(link_el.get("href", ""))

    # Date
    date_el = card.select_one("time.job-search-card__listdate") or card.select_one("time")
    posted_dt = ""
    posted_text = "Unknown"
    if date_el:
        posted_dt = date_el.get("datetime", "")
        posted_text = date_el.get_text(strip=True)

    return {
        "jobTitle": title,
        "companyName": company,
        "jobDescription": "",
        "location": location,
        "jobUrl": link,
        "applyUrl": link,
        "publishedAt": posted_dt,
        "postedTime": posted_text,
        "contractType": "Unknown",
        "_source": "LinkedIn",
    }


async def _scrape_linkedin(browser, tab, processed_urls=None):
    """Scrape LinkedIn job listings — reuses a single tab."""
    print("🕷️  Scraping LinkedIn Cybersecurity Jobs (browser)...")
    LAST_HEALTH.update(errors=[], status="unknown")
    all_jobs = []
    seen_urls = set()
    if processed_urls is None:
        processed_urls = set()
    processed_urls = {canonical_linkedin_url(url) for url in processed_urls}

    for i, search_url in enumerate(LINKEDIN_SEARCH_URLS):
        try:
            search_count = 0
            # Paginate: LinkedIn returns 25 jobs per page via &start=N
            for page_start in range(0, MAX_PAGES_PER_SEARCH * 25, 25):
                paged_url = search_url + f"&start={page_start}" if page_start > 0 else search_url
                ok = await _goto(tab, paged_url)
                if not ok:
                    LAST_HEALTH["errors"].append(f"Search {i+1}: navigation failed")
                    break

                # Scroll to trigger lazy-loaded cards
                await _scroll_page(tab, times=4, distance=800)

                html = await _safe_get_content(tab)
                if not html:
                    LAST_HEALTH["errors"].append(f"Search {i+1}: empty page")
                    break

                soup = BeautifulSoup(html, "lxml")

                cards = (
                    soup.select("div.base-card")
                    or soup.select("li.jobs-search-results__list-item")
                    or soup.select("div.job-card-container")
                    or soup.select("[class*='job-card']")
                )

                if not cards:
                    if page_start == 0 and not re.search(r"no (?:matching )?jobs|0 jobs|no results", soup.get_text(), re.I):
                        LAST_HEALTH["errors"].append(f"Search {i+1}: unrecognized or blocked page")
                    break  # No more results on this page

                page_new = 0
                for card in cards:
                    try:
                        job = _parse_linkedin_card(card)
                        if job and job["jobUrl"] and job["jobUrl"] not in seen_urls:
                            job["_already_processed"] = job["jobUrl"] in processed_urls
                            seen_urls.add(job["jobUrl"])
                            all_jobs.append(job)
                            search_count += 1
                            page_new += 1
                            if search_count >= MAX_JOBS_PER_SEARCH:
                                break
                    except Exception:
                        continue

                # A page can overlap another query completely. Continue to the
                # next bounded page so that overlap does not hide deeper jobs.
                if search_count >= MAX_JOBS_PER_SEARCH:
                    break

                await _random_delay(2, 4)

            print(f"  📋 LinkedIn search {i+1}/{len(LINKEDIN_SEARCH_URLS)}: found {search_count} jobs")
            await _random_delay(2, 4)

        except Exception as e:
            LAST_HEALTH["errors"].append(f"Search {i+1}: {type(e).__name__}")
            print(f"  ⚠️  LinkedIn search {i+1} failed: {e}")

    # Fetch full descriptions (reuse the same tab)
    max_fetches = len(all_jobs)
    if max_fetches > 0:
        print(f"  📄 Fetching descriptions for {max_fetches} LinkedIn jobs...")
        api_failures = 0
        for j, job in enumerate(all_jobs[:max_fetches]):
            if job.get("_already_processed"):
                continue
            if job.get("jobUrl") and "linkedin.com" in job["jobUrl"]:
                try:
                    # Extract 10-digit job ID for fast API fetch
                    job_id = linkedin_job_id(job["jobUrl"])

                    if job_id and api_failures < 3:
                        api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
                        ok = await _goto(tab, api_url, wait=1)
                        if ok:
                            html = await _safe_get_content(tab, retries=1)
                            if html:
                                soup = BeautifulSoup(html, "lxml")
                                desc_el = soup.select_one("div.show-more-less-html__markup") or soup.select_one("div.description__text")
                                if desc_el:
                                    job["jobDescription"] = desc_el.get_text(separator=" ", strip=True)
                                    print(f"    ✓ {j+1}/{max_fetches} got description (API-Fast)")
                                    api_failures = 0
                                    await asyncio.sleep(0.5)
                                    continue
                        api_failures += 1
                        if api_failures == 3:
                            print("    ⏭️ LinkedIn guest API unavailable; using job pages for remaining descriptions")

                    # Fallback to UI "Show more" click if API fails
                    ok = await _goto(tab, job["jobUrl"])
                    if not ok:
                        continue

                    # Click "Show more" via JavaScript
                    try:
                        await tab.evaluate("""
                            const btn = document.querySelector('button.show-more-less-html__button');
                            if (btn) btn.click();
                        """)
                        await asyncio.sleep(1)
                    except Exception:
                        pass

                    html = await _safe_get_content(tab)
                    if html:
                        soup = BeautifulSoup(html, "lxml")
                        desc_el = (
                            soup.select_one("div.show-more-less-html__markup")
                            or soup.select_one("div.description__text")
                            or soup.select_one("div.jobs-description__content")
                            or soup.select_one("div#job-details")
                            or soup.select_one("section.description")
                        )
                        if desc_el:
                            job["jobDescription"] = desc_el.get_text(separator=" ", strip=True)
                            print(f"    ✓ {j+1}/{max_fetches} got description (UI)")
                        else:
                            print(f"    ✗ {j+1}/{max_fetches} no description element found")
                    await _random_delay(1, 3)
                except Exception as e:
                    LAST_HEALTH["errors"].append(f"Description {j+1}: {type(e).__name__}")
                    print(f"    ✗ {j+1}/{max_fetches} error: {e}")

    missing = sum(len((job.get("jobDescription") or "").strip()) <= 20 for job in all_jobs if not job.get("_already_processed"))
    if missing:
        LAST_HEALTH["errors"].append(f"{missing} jobs have missing descriptions")
    print(f"  ✅ LinkedIn returned {len(all_jobs)} jobs")
    return all_jobs


async def _scrape_all(processed_urls=None):
    """Main async scraping orchestrator."""
    print("🚀 Launching Scrapers (LinkedIn via Browser)...")

    LAST_HEALTH.update(errors=[], status="unknown")
    linkedin_jobs = []

    browser = None
    try:
        # Launching one central browser with the persistent profile to use user's active logins
        browser = await _create_browser(anonymous=False)
        tab = await browser.get("about:blank")
        await asyncio.sleep(2)

        # Run them sequentially on the same browser profile to avoid lock contention
        linkedin_jobs = await _scrape_linkedin(browser, tab, processed_urls)

    except Exception as e:
        LAST_HEALTH["errors"].append(f"Browser: {type(e).__name__}")
        print(f"⚠️  Browser scraper crashed: {e}")
    finally:
        if browser:
            try: browser.stop()
            except: pass

    print(f"📊 Total scraped: {len(linkedin_jobs)} jobs (LinkedIn only)")
    LAST_HEALTH["status"] = "partial" if linkedin_jobs and LAST_HEALTH["errors"] else "error" if LAST_HEALTH["errors"] else "success"
    return linkedin_jobs


def scrape_all_jobs(processed_urls=None):
    """Synchronous entry point — call this from job_matcher.py."""
    try:
        return asyncio.run(_scrape_all(processed_urls))
    except RuntimeError as e:
        if "Event loop is closed" in str(e):
            pass  # Harmless cleanup error
        else:
            LAST_HEALTH.update(errors=[f"Runtime: {type(e).__name__}"], status="error")
            print(f"⚠️  Runtime error: {e}")
        return []
