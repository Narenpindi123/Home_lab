import os
import sys
import re
import hashlib
import math
import sqlite3
import time
import requests
import threading
import fitz  # PyMuPDF
import telebot
import concurrent.futures
from fpdf import FPDF
from datetime import datetime, timedelta, timezone
from time_utils import parse_scraped_at, scrape_is_fresh
from matching_rules import candidate_matches_track, required_experience_years
import browser_scraper
import discovery_store
import local_sources_client
import startup_job_sources
import ziprecruiter_scraper
from eligibility import eligibility_decision, outside_discovery_window, freshness_key
from grounded_scoring import scoring_prompt, render_analysis, scoring_payload

PIPELINE_MODULE_DIR = os.environ.get("JOB_PIPELINE_MODULE_DIR", "/pipeline")
if PIPELINE_MODULE_DIR not in sys.path:
    sys.path.insert(0, PIPELINE_MODULE_DIR)
try:
    import application_store
except ImportError:
    application_store = None

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Ollama configuration
from pathlib import Path

def ollama_cloud_key():
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if key:
        return key
    configured_path = os.environ.get("OLLAMA_CLOUD_ENV_FILE", "")
    if not configured_path:
        return ""
    path = Path(configured_path)
    try:
        for line in path.read_text().splitlines():
            if line.startswith("OLLAMA_API_KEY="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""

def ollama_headers(base_url):
    headers = {"Content-Type": "application/json"}
    if base_url == "https://ollama.com":
        headers["Authorization"] = "Bearer " + ollama_cloud_key()
    return headers

MODEL_NAME = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
EMBED_MODEL_NAME = os.environ.get("OLLAMA_EMBED_MODEL", "embeddinggemma")
FORCE_TARGET = os.environ.get("OLLAMA_TARGET", "default")

# Global persistent session for connection pooling
http_session = requests.Session()
ACTIVE_CHAT_ENDPOINT = None
ACTIVE_EMBED_ENDPOINT = None
OLLAMA_FAILURE_COOLDOWN = int(os.environ.get("OLLAMA_FAILURE_COOLDOWN", "600"))
OLLAMA_EMBEDDINGS_DISABLED_UNTIL = 0
_ollama_down_until = {}

def check_ollama_endpoints(require_embeddings=False):
    global ACTIVE_CHAT_ENDPOINT, ACTIVE_EMBED_ENDPOINT, MODEL_NAME, FORCE_TARGET, OLLAMA_EMBEDDINGS_DISABLED_UNTIL

    if ACTIVE_CHAT_ENDPOINT and (not require_embeddings or ACTIVE_EMBED_ENDPOINT or
                                 OLLAMA_EMBEDDINGS_DISABLED_UNTIL > time.time()):
        return

    print("🔍 Performing Pre-Flight Health Check on Ollama Nodes...")

    chat_endpoints = [("http://127.0.0.1:11434", MODEL_NAME, "local")]
    embed_endpoints = [("http://127.0.0.1:11434", EMBED_MODEL_NAME, "local")]

    # Explicit hosts take precedence over saved nodes; embeddings may run separately.
    configured_host = os.environ.get("OLLAMA_PRIMARY_HOST") or os.environ.get("OLLAMA_HOST")
    if configured_host:
        configured_model = os.environ.get("OLLAMA_PRIMARY_MODEL", MODEL_NAME)
        chat_endpoints = [(configured_host, configured_model, "local")]
        embed_host = os.environ.get("OLLAMA_EMBED_HOST", configured_host)
        embed_endpoints = [(embed_host, EMBED_MODEL_NAME, "local")]

    valid_chat = [(b, m) for b, m, t in chat_endpoints if FORCE_TARGET in ("default", t)]
    if ollama_cloud_key():
        # Direct cloud access stays independent of the Windows/local server.
        cloud = ("https://ollama.com", os.environ.get("OLLAMA_CLOUD_MODEL", "gpt-oss:20b"))
        valid_chat.insert(1 if valid_chat else 0, cloud)
    valid_embed = [(b, m) for b, m, t in embed_endpoints if FORCE_TARGET in ("default", t)]

    for base_url, model in ([] if ACTIVE_CHAT_ENDPOINT else valid_chat):
        target_key = ("chat", base_url, model)
        if _ollama_down_until.get(target_key, 0) > time.time():
            continue

        try:
            start = time.time()
            res = http_session.get(f"{base_url}/api/tags", headers=ollama_headers(base_url), timeout=3)
            res.raise_for_status()
            latency = time.time() - start
            print(f"  ✅ [Chat] Node Online: {base_url} ({latency:.2f}s)")
            ACTIVE_CHAT_ENDPOINT = (base_url, model)
            MODEL_NAME = model
            break
        except Exception as e:
            _ollama_down_until[target_key] = time.time() + OLLAMA_FAILURE_COOLDOWN
            print(f"  ❌ [Chat] Node Offline: {base_url} ({e})")

    if require_embeddings and not ACTIVE_EMBED_ENDPOINT and OLLAMA_EMBEDDINGS_DISABLED_UNTIL <= time.time():
        for base_url, model in valid_embed:
            target_key = ("embed", base_url, model)
            if _ollama_down_until.get(target_key, 0) > time.time():
                continue

            try:
                start = time.time()
                res = http_session.post(
                    f"{base_url}/api/embed",
                    headers=ollama_headers(base_url),
                    json={"model": model, "input": "health check"},
                    timeout=(5, 30)
                )
                res.raise_for_status()
                if not res.json().get("embeddings"):
                    raise RuntimeError("embedding response did not include a vector")
                latency = time.time() - start
                print(f"  ✅ [Embed] Node Online: {base_url} ({latency:.2f}s)")
                ACTIVE_EMBED_ENDPOINT = (base_url, model)
                break
            except Exception as e:
                _ollama_down_until[target_key] = time.time() + OLLAMA_FAILURE_COOLDOWN
                print(f"  ❌ [Embed] Node Unavailable: {base_url} ({e})")

        if not ACTIVE_EMBED_ENDPOINT:
            OLLAMA_EMBEDDINGS_DISABLED_UNTIL = time.time() + OLLAMA_FAILURE_COOLDOWN
            print(f"  ⚠️ Embeddings disabled for {OLLAMA_FAILURE_COOLDOWN}s; continuing with LLM scoring only.")

    if not ACTIVE_CHAT_ENDPOINT: print("  ⚠️ CRITICAL: All Chat node fallbacks failed!")
    if require_embeddings and not ACTIVE_EMBED_ENDPOINT and OLLAMA_EMBEDDINGS_DISABLED_UNTIL <= time.time():
        print("  ⚠️ CRITICAL: All Embed node fallbacks failed!")


def get_ollama_response(prompt, *, json_mode=False):
    global ACTIVE_CHAT_ENDPOINT
    for attempt in range(4):
        check_ollama_endpoints()
        if not ACTIVE_CHAT_ENDPOINT:
            return None

        base_url, model = ACTIVE_CHAT_ENDPOINT
        url = f"{base_url}/api/chat" if json_mode else f"{base_url}/v1/chat/completions"
        try:
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
            if json_mode:
                payload = scoring_payload(model, prompt)
            # 5s to connect, 900s to read
            res = http_session.post(
                url,
                headers=ollama_headers(base_url),
                json=payload,
                timeout=(5, 900)
            )
            res.raise_for_status()
            data = res.json()
            return data['message']['content'] if json_mode else data['choices'][0]['message']['content']
        except Exception as e:
            print(f"  ⚠️  AI failed on active node {base_url}: {e}")
            cooldown = OLLAMA_FAILURE_COOLDOWN
            response = getattr(e, "response", None)
            if response is not None and response.status_code == 429:
                # Cloud quotas can remain exhausted for hours. Avoid selecting
                # the same model again during every long-running local analysis.
                cooldown = max(cooldown, 3600)
                try:
                    cooldown = max(cooldown, min(86400, float(response.headers.get("Retry-After", 0))))
                except (TypeError, ValueError):
                    pass
            _ollama_down_until[("chat", base_url, model)] = time.time() + cooldown
            if ACTIVE_CHAT_ENDPOINT == (base_url, model):
                ACTIVE_CHAT_ENDPOINT = None
            time.sleep(2 * (attempt + 1))
    return None

def get_ollama_embedding(text):
    """Generates vector embeddings."""
    global ACTIVE_EMBED_ENDPOINT
    if OLLAMA_EMBEDDINGS_DISABLED_UNTIL > time.time():
        return []

    for attempt in range(3):
        check_ollama_endpoints(require_embeddings=True)
        if not ACTIVE_EMBED_ENDPOINT:
            return []

        base_url, model = ACTIVE_EMBED_ENDPOINT
        url = f"{base_url}/api/embed"
        try:
            res = http_session.post(
                url,
                headers=ollama_headers(base_url),
                json={"model": model, "input": text},
                timeout=(5, 120)
            )
            res.raise_for_status()
            embeddings = res.json().get('embeddings', [])
            return embeddings[0] if embeddings else []
        except Exception as e:
            print(f"  ⚠️  Embedding failed on active node {base_url}: {e}")
            _ollama_down_until[("embed", base_url, model)] = time.time() + OLLAMA_FAILURE_COOLDOWN
            ACTIVE_EMBED_ENDPOINT = None
            time.sleep(2 * (attempt + 1))
    return []

def cosine_similarity(v1, v2):
    """Calculates Cosine Similarity distance without requiring numpy bloat."""
    if not v1 or not v2: return 0.0
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_v1 = math.sqrt(sum(a * a for a in v1))
    norm_v2 = math.sqrt(sum(b * b for b in v2))
    if norm_v1 == 0 or norm_v2 == 0: return 0.0
    return dot_product / (norm_v1 * norm_v2)


bot = telebot.TeleBot(TELEGRAM_TOKEN) if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else None

def _telegram_call(action, description):
    if bot is None or os.environ.get("TELEGRAM_NOTIFICATIONS_ENABLED", "true").lower() != "true":
        return None
    delays = (2, 5, 15)
    for attempt, delay in enumerate(delays, start=1):
        try:
            return action()
        except Exception as e:
            print(f"⚠️ Telegram {description} failed on attempt {attempt}/{len(delays)}: {e}")
            if attempt == len(delays):
                return None
            time.sleep(delay)
    return None

def send_telegram_msg(text):
    ntfy_url = os.environ.get("JOB_NTFY_URL", "").rstrip("/")
    ntfy_topic = os.environ.get("JOB_NTFY_TOPIC", "")
    if ntfy_url and ntfy_topic:
        try:
            response = requests.post(
                f"{ntfy_url}/{ntfy_topic}", data=text.encode("utf-8"),
                headers={"Title": "Job Matcher", "Tags": "briefcase",
                         "Click": os.environ.get("JOB_DASHBOARD_URL", "http://127.0.0.1:5555/")}, timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"ntfy job alert failed: {type(exc).__name__}")
    return _telegram_call(
        lambda: bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown"),
        "message send"
    )

def send_telegram_doc(filepath, caption=""):
    with open(filepath, "rb") as f:
        return _telegram_call(
            lambda: bot.send_document(chat_id=TELEGRAM_CHAT_ID, document=f, caption=caption, parse_mode="Markdown"),
            "document send"
        )

DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "data/processed_jobs.db"))

def init_db(db_path=None):
    db_path = db_path or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS processed_jobs (job_url TEXT PRIMARY KEY, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs_history (
        job_url TEXT PRIMARY KEY,
        title TEXT, company TEXT, source TEXT, description TEXT,
        location TEXT, posted_time TEXT, contract_type TEXT,
        candidate TEXT, ai_analysis TEXT, match_score INTEGER,
        applied INTEGER DEFAULT 0,
        starred INTEGER DEFAULT 0,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Migrate existing databases — add new columns if they don't exist
    for col, col_type in [("location", "TEXT"), ("posted_time", "TEXT"), ("contract_type", "TEXT"),
                          ("candidate", "TEXT"), ("ai_analysis", "TEXT"), ("match_score", "INTEGER"),
                          ("applied", "INTEGER DEFAULT 0"), ("starred", "INTEGER DEFAULT 0"),
                          ("skipped", "INTEGER DEFAULT 0"),
                          ("url_hash", "TEXT"), ("notion_page_id", "TEXT"),
                          ("notion_status", "TEXT"), ("notion_synced_at", "TEXT")]:
        try:
            c.execute(f"ALTER TABLE jobs_history ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists
    c.execute("CREATE INDEX IF NOT EXISTS idx_url_hash ON jobs_history(url_hash)")
    c.execute('''CREATE TABLE IF NOT EXISTS job_candidate_results (
        job_url TEXT NOT NULL, candidate TEXT NOT NULL, job_json TEXT NOT NULL,
        analysis TEXT NOT NULL, match_score INTEGER, outcome TEXT NOT NULL,
        evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (job_url, candidate)
    )''')
    conn.commit()
    discovery_store.init_schema(conn)
    import candidate_tracking
    candidate_tracking.init_schema(conn)
    if application_store:
        application_store.init_schema(os.environ.get("JOB_PIPELINE_DB", db_path))
    return conn

def is_processed(conn, url):
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_jobs WHERE job_url = ?", (url,))
    return c.fetchone() is not None

def mark_processed(conn, url, job_obj=None):
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO processed_jobs (job_url) VALUES (?)", (url,))
    if job_obj:
        c.execute("""INSERT OR IGNORE INTO jobs_history
                     (job_url, url_hash, title, company, source, description, location, posted_time, contract_type)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (url, _url_hash(url), job_obj.get('jobTitle', ''), job_obj.get('companyName', ''),
                   job_obj.get('_source', ''), job_obj.get('jobDescription', ''),
                   job_obj.get('location', ''), job_obj.get('postedTime', ''),
                   job_obj.get('contractType', '')))
    conn.commit()


def remove_job_from_history(conn, url):
    """Hide an irrelevant job from the dashboard while keeping it deduped."""
    c = conn.cursor()
    c.execute("DELETE FROM jobs_history WHERE job_url = ?", (url,))
    c.execute("INSERT OR IGNORE INTO processed_jobs (job_url) VALUES (?)", (url,))
    conn.commit()


def ensure_job_in_history(conn, url, job_obj):
    """Ensure a job row exists in jobs_history so subsequent UPDATEs have a target."""
    c = conn.cursor()
    c.execute("""INSERT OR IGNORE INTO jobs_history
                 (job_url, url_hash, title, company, source, description, location, posted_time, contract_type)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (url, _url_hash(url), job_obj.get('jobTitle', ''), job_obj.get('companyName', ''),
               job_obj.get('_source', ''), job_obj.get('jobDescription', ''),
               job_obj.get('location', ''), job_obj.get('postedTime', ''),
               job_obj.get('contractType', '')))
    conn.commit()

def save_job_analysis(conn, url, candidate, analysis, score):
    """Store AI analysis results for a job+candidate pair."""
    c = conn.cursor()
    c.execute("""UPDATE jobs_history SET candidate = ?, ai_analysis = ?, match_score = ? WHERE job_url = ?""",
              (candidate, analysis, score, url))
    conn.commit()
    if application_store:
        row = c.execute(
            "SELECT title,company,source,location,notion_page_id,applied FROM jobs_history WHERE job_url=?",
            (url,),
        ).fetchone()
        if row:
            application_store.ensure_application(
                db_path=os.environ.get("JOB_PIPELINE_DB", DB_PATH), job_url=url,
                candidate=candidate, title=row[0] or "", company=row[1] or "",
                source=row[2] or "", location=row[3] or "", match_score=score,
                notion_page_id=row[4] or "", status="applied" if row[5] else "discovered",
            )

from candidate_resumes import CANDIDATE_RESUMES

def _url_hash(url):
    return hashlib.md5((url or "").encode()).hexdigest()[:12]

def extract_resumes():
    candidates = {"candidate_a": {}, "candidate_b": {}}
    for candidate, paths in CANDIDATE_RESUMES.items():
        for path in paths:
            if not os.path.exists(path):
                print(f"  ⚠️  Resume not found: {path}")
                continue
            try:
                content = "".join(page.get_text() for page in fitz.open(path))
                candidates[candidate][os.path.basename(path)] = content
            except Exception as e:
                print(f"  ⚠️  Skipping {path}: {e}")
    return candidates

import json

def _load_host_jobs(source, conn):
    path = os.path.join(os.path.dirname(DB_PATH), source.lower() + "_jobs.json")
    try:
        with open(path) as handle:
            data = json.load(handle)
        stamp = parse_scraped_at(data['scraped_at'])
        jobs = data.get('jobs', [])
        errors = data.get('errors', [])
        status = data.get('status') or 'unknown'
        if source == 'Indeed':
            for job in jobs:
                job['publishedAt'] = job.get('postedTime', '')
        elif source == 'Glassdoor' and 'status' not in data:
            for job in jobs:
                job['publishedAt'] = ''
        if not scrape_is_fresh(stamp):
            discovery_store.record_run(conn, source, [], errors=['Host snapshot is older than 24 hours'], status='stale')
            return []
        discovery_store.record_run(conn, source, jobs, stamp.isoformat(), errors, status)
        if source == 'Glassdoor':
            complete = [job for job in jobs if len((job.get('jobDescription') or '').strip()) > 20]
            if len(complete) != len(jobs):
                print(f"⚠️ Glassdoor: excluded {len(jobs) - len(complete)} jobs with missing descriptions")
            return complete
        return jobs
    except Exception as exc:
        discovery_store.record_run(conn, source, [], errors=[f'Host snapshot unavailable: {type(exc).__name__}'])
        return []


def scrape_jobs(processed_urls=None, on_ready=None):
    """Publish ready snapshots while slower sources run in their own threads."""
    def browser_fetch():
        conn = init_db()
        try:
            try:
                if os.environ.get('LINKEDIN_API_URL'):
                    import linkedin_api_client
                    jobs = linkedin_api_client.scrape_jobs(browser_scraper.LINKEDIN_SEARCH_URLS)
                    health = linkedin_api_client.LAST_HEALTH
                else:
                    jobs = browser_scraper.scrape_all_jobs(processed_urls)
                    health = getattr(browser_scraper, 'LAST_HEALTH', {})
                discovery_store.record_run(conn, 'LinkedIn', jobs,
                    errors=health.get('errors', []), status=health.get('status', 'unknown'))
                return jobs
            except Exception as exc:
                discovery_store.record_run(conn, 'LinkedIn', [], errors=[type(exc).__name__])
                return []
        finally:
            conn.close()

    def local_fetch(source, name, fetch=None):
        conn = init_db()
        try:
            jobs, health = (fetch or local_sources_client.scrape_source)(source)
            discovery_store.record_run(conn, name, jobs, errors=health['errors'], status=health['status'])
            return jobs
        except Exception as exc:
            discovery_store.record_run(conn, name, [], errors=['Collector: ' + type(exc).__name__])
            return []
        finally:
            conn.close()

    def ziprecruiter_fetch():
        conn = init_db()
        try:
            jobs = ziprecruiter_scraper.scrape_jobs()
            health = ziprecruiter_scraper.LAST_HEALTH
            discovery_store.record_run(conn, 'ZipRecruiter', jobs,
                errors=health['errors'], status=health['status'])
            return jobs
        except Exception as exc:
            discovery_store.record_run(conn, 'ZipRecruiter', [], errors=[f'Browser: {type(exc).__name__}'])
            return []
        finally:
            conn.close()

    combined = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(browser_fetch), pool.submit(local_fetch, 'ats', 'Employer boards'),
                   pool.submit(local_fetch, 'dice', 'Dice'), pool.submit(ziprecruiter_fetch)]
        futures.extend(pool.submit(local_fetch, source, name, startup_job_sources.scrape_source)
                       for source, name in startup_job_sources.SOURCES.items())
        conn = init_db()
        try:
            ready = _load_host_jobs('Indeed', conn) + _load_host_jobs('Glassdoor', conn)
        finally:
            conn.close()
        combined.extend(ready)
        if on_ready:
            on_ready(ready)
        for future in concurrent.futures.as_completed(futures):
            batch = future.result()
            combined.extend(batch)
            if on_ready:
                on_ready(batch)
    return combined


CANDIDATE_B_ENTRY_LEVEL_CONTEXT = "Use only the supplied resume as evidence. Candidate B targets GRC roles."

CANDIDATE_A_ENTRY_LEVEL_CONTEXT = "Use only the supplied resume as evidence. Candidate A targets security and IT roles."

def summarize_resumes(resumes, candidate_name=""):
    # Preserve original resume evidence instead of scoring an AI paraphrase.
    profile = "\n\n".join(f"--- {name} ---\n{content}" for name, content in resumes.items())
    context = CANDIDATE_A_ENTRY_LEVEL_CONTEXT if candidate_name.lower() == 'candidate_a' else CANDIDATE_B_ENTRY_LEVEL_CONTEXT
    return profile + "\n\nCANDIDATE POLICY CONTEXT (not resume evidence):\n" + context


def analyze_job(title, company, desc, location, resume_summary):
    print(f"🤖 Analyzing: {title} at {company}...")
    prompt = scoring_prompt(title, company, desc or '', location, resume_summary)
    analysis = ''
    for attempt in range(2):
        retry_prompt = prompt
        if attempt:
            retry_prompt += ("\nVALIDATION RETRY: Return valid JSON and copy every evidence quote "
                             "exactly and contiguously from the supplied job/profile text.")
        analysis = render_analysis(
            get_ollama_response(retry_prompt, json_mode=True), desc or '', resume_summary
        )
        if not analysis.startswith('Analysis unavailable'):
            return analysis
    return analysis


def generate_cover_letter(title, company, desc, resumes):
    print(f"📝 Generating cover letter for: {title} at {company}...")
    prompt = f"""Write a professional, compelling, and tailored cover letter for the '{title}' position at '{company}'.

**Job Description:**
{desc}

**Candidate Resumes (Use the most relevant experience):**
"""
    for name, content in resumes.items():
        prompt += f"\n--- {name} ---\n{content[:2000]}\n"

    prompt += "\nOutput ONLY the cover letter text, no explanations. Do not include a date or placeholders for a date. Limit the length to 3-4 concise paragraphs so it fits on exactly one page. Use standard ASCII characters, without emojis or markdown."

    response = get_ollama_response(prompt)
    if response:
        return response
    else:
        print("  ⚠️  Cover letter generation failed: All endpoints unreachable.")
        return ""

def generate_cover_letters_bg(winners, resumes, candidate_name):
    """Background thread function to silently generate cover letters without blocking scraper results."""
    if not winners: return
    print(f"🔄 Starting background task to generate {len(winners)} cover letters for {candidate_name}...")

    for res_dict, job_obj in winners:
        title = res_dict.get('title')
        company = res_dict.get('company')
        desc = job_obj.get('jobDescription', '')

        cl_text = generate_cover_letter(title, company, desc, resumes)
        if cl_text:
            safe_company = "".join(c for c in company if c.isalnum() or c in " _-").strip()
            safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()
            cl_filename = f"/app/cover_letters/{candidate_name}_{safe_company}_{safe_title}.pdf".replace(" ", "_").replace("__", "_")
            os.makedirs(os.path.dirname(cl_filename), exist_ok=True)

            cleaned_cl_text = clean_markdown_for_pdf(cl_text)
            create_pdf(cleaned_cl_text, cl_filename)
            print(f"  💾 Saved background cover letter to {cl_filename}")


def build_report(results, candidate_name):
    """Build a formatted markdown report."""
    today = datetime.now().strftime("%B %d, %Y")

    report = f"""# 🔒 Cybersecurity Job Match Report ({candidate_name})
**Generated:** {today}
**Jobs Analyzed:** {len(results)}
**Model:** {MODEL_NAME}

---

"""
    for i, r in enumerate(results, 1):
        source = r.get('source', 'Unknown')
        source_emoji = {'LinkedIn': '🔗', 'Indeed': '🟦', 'Glassdoor': '🟩', 'Google Jobs': '🔍'}.get(source, '📋')
        report += f"""## {i}. {r['title']} — {r['company']}

{source_emoji} **Source:** {r.get('source', 'Unknown')}
📍 **Location:** {r['location']}
🔗 **Apply:** {r['link']}
📅 **Posted:** {r['posted']}
💼 **Type:** {r['contract']}

### AI Match Analysis

{r['analysis']}

---

"""

    skipped = sum(1 for r in results if "unavailable" in r['analysis'])
    report += f"""
## Summary
- **Total jobs analyzed:** {len(results)}
- **Successfully analyzed:** {len(results) - skipped}
- **Analysis unavailable:** {skipped}

_Report generated automatically by Job Matcher + Ollama ({MODEL_NAME})_
"""
    return report

def get_score(result_or_analysis):
    # Works for both a result dict or raw analysis string
    analysis = result_or_analysis.get("analysis", "") if isinstance(result_or_analysis, dict) else result_or_analysis

    # Remove "(out of 100)" text to prevent regex from erroneously matching "100" as the score
    cleaned = re.sub(r'(?i)\(?out\s+of\s+100\)?', '', analysis)

    match = re.search(r'\b(\d{1,3})\s*/\s*100\b', cleaned)
    if match: return int(match.group(1))

    match = re.search(r'\b(\d{1,3})\s+out\s+of\s+100\b', cleaned)
    if match: return int(match.group(1))

    match = re.search(r'(?i)score[^\d]*(\d{1,3})', cleaned)
    if match: return int(match.group(1))

    return 0


def is_irrelevant_analysis(analysis, score):
    """Return True when a processed job should be hidden from the dashboard."""
    analysis_lower = (analysis or "").lower()
    if ("skipped via embeddings" in analysis_lower or "mathematically incompatible" in analysis_lower
            or "skipped via hard filter" in analysis_lower):
        return True
    return (score > 0 and score < 55) or bool(re.search(r"Match score:\s*0/100", analysis, re.I))


def text_overlap_similarity(s1, s2):
    """Calculates Jaccard-like overlap coefficient based on words."""
    s1_words = set(re.findall(r'\w+', (s1 or "").lower()))
    s2_words = set(re.findall(r'\w+', (s2 or "").lower()))
    if not s1_words or not s2_words: return 0.0
    return len(s1_words.intersection(s2_words)) / min(len(s1_words), len(s2_words))

def clean_markdown_for_pdf(text):
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'__(.*?)__', r'\1', text)
    text = re.sub(r'#+\s*', '', text)
    text = text.replace('* ', '- ')
    # Fix common unicode punctuation that breaks FPDF
    text = text.replace('`', "'").replace('‘', "'").replace('’', "'")
    text = text.replace('“', '"').replace('”', '"')
    text = text.replace('—', '-').replace('–', '-')
    return text.strip()

def create_pdf(text, filename):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    # Ignore unencodable characters to avoid weird ? symbols
    clean_text = text.encode('latin-1', 'ignore').decode('latin-1')
    pdf.multi_cell(0, 5, clean_text)
    pdf.output(filename)

def get_recent_jobs(conn, days=7):
    """Fetch recent valid jobs to prevent deduplication across scrapes limits."""
    try:
        c = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        c.execute("SELECT title, company FROM jobs_history WHERE processed_at >= ?", (cutoff,))
        return c.fetchall()
    except sqlite3.Error:
        return []


def hard_filter_job(job, candidate="candidate_a"):
    """Cheap, explainable gates that run before embeddings or generative AI."""
    title = (job.get("jobTitle") or "").lower()
    description = (job.get("jobDescription") or "").lower()
    text = f" {title} {description} "

    state, reason = eligibility_decision(job)
    if state == 'rejected':
        return False, reason

    unrelated_titles = (
        "attorney", "lawyer", "paralegal", "registered nurse", "physician",
        "pharmacist", "dentist", "physical therapist", "clinical", "accountant",
        "tax analyst", "mechanical engineer", "civil engineer", "sales representative",
        "marketing manager", "warehouse", "driver", "teacher", "social worker",
    )
    if any(term in title for term in unrelated_titles):
        return False, "unrelated profession"

    mandatory_license_patterns = (
        r"(?:must|required to) (?:hold|have|possess).{0,35}\b(?:rn|jd|cpa|pe|medical|nursing|bar)\b",
        r"\b(?:active|valid|current) (?:rn|nursing|medical|bar|cpa|professional engineer) licen[cs]e required",
        r"\bbar admission required\b",
    )
    if any(re.search(pattern, text) for pattern in mandatory_license_patterns):
        return False, "mandatory professional license"

    senior_title = re.search(
        r"\b(senior|sr\.?|lead|manager|director|principal|staff|architect|head|vp|vice president)\b",
        title,
    )
    if senior_title:
        return False, f"seniority mismatch ({senior_title.group(1)})"

    numbered_level = re.search(
        r"\b(?:analyst|engineer|specialist|administrator|technician)\s+"
        r"(ii|iii|iv|v|2|3|4|5)\b|\btier\s*(3|iii)\b",
        title,
    )
    if numbered_level:
        return False, f"seniority mismatch (level {numbered_level.group(1) or numbered_level.group(2)})"

    # Aggregators occasionally omit the level from the title while the opening
    # description contains the actual senior role name.
    senior_intro = re.search(
        r"(?:^|\bjob\s+(?:description|title)\s*:?[\s-]*|\bthe\s+)"
        r"(?:[a-z&/-]+\s+){0,6}(senior|sr\.?)\s+"
        r"(?:[a-z&/-]+\s+){0,4}(?:analyst|engineer|specialist|consultant|administrator)\b"
        r"(?:\s+role)?\s+(?:will|reports?|is responsible)",
        description[:700],
    )
    if senior_intro:
        return False, f"seniority mismatch ({senior_intro.group(1)})"

    if any(phrase in description for phrase in (
        "not looking for candidates whose primary background is cybersecurity",
        "not seeking candidates whose primary background is cybersecurity",
    )):
        return False, "posting explicitly excludes a cybersecurity-primary background"

    years = required_experience_years(description)
    if years is not None and years >= 2:
        return False, f"requires {years}+ years experience"

    sponsorship_required = os.environ.get(
        f"{candidate.upper()}_REQUIRES_SPONSORSHIP", "false"
    ).lower() in {"1", "true", "yes"}
    no_sponsorship = any(phrase in text for phrase in (
        "will not sponsor", "no visa sponsorship", "unable to sponsor",
        "without current or future sponsorship", "sponsorship is not available",
    ))
    if sponsorship_required and no_sponsorship:
        return False, "work authorization/sponsorship mismatch"

    if candidate.lower() == "candidate_b":
        grc_terms = (
            "grc", "governance", "risk", "compliance", "audit", "privacy",
            "assurance", "controls", "third party", "vendor risk", "policy",
        )
        if not any(term in f"{title} {description[:700]}" for term in grc_terms):
            return False, "outside candidate GRC track"

    return True, reason if state == 'needs_review' else 'passed'


def evaluate_candidate_job(job, candidate, resume_summary, override_eligibility=False, analyzer=None):
    """Evaluate actual eligibility and fit, without an uncalibrated embedding gate."""
    eligible, reason = hard_filter_job(job, candidate)
    if not candidate_matches_track(job, candidate):
        eligible, reason = False, "outside candidate target track"
    state, eligibility_reason = eligibility_decision(job)
    if override_eligibility:
        eligible, reason, state = True, 'User restored for consideration', 'eligible'
    elif not eligible:
        state = 'rejected'
    elif len((job.get('jobDescription') or '').strip()) <= 20:
        state, reason = 'needs_review', 'Missing or incomplete job description'
    elif state == 'needs_review':
        reason = eligibility_reason
    if job.get('_description_status') == 'search_snippet_only' and state != 'rejected':
        state, reason = 'needs_review', 'Search-index listing: full job description, posting date and availability need verification'
    if eligible and state != 'needs_review':
        analysis = (analyzer or analyze_job)(job.get('jobTitle', ''), job.get('companyName', ''),
                               job.get('jobDescription', ''), job.get('location', ''),
                               resume_summary)
    elif state == "needs_review":
        analysis = f"Needs review: {reason}"
    else:
        analysis = f"❌ Skipped via hard filter: {reason}."
        print(f"  🚫 Skipped {job.get('jobTitle', '')} via hard filter ({reason})")
    return {
        'title': job.get('jobTitle', ''), 'company': job.get('companyName', ''),
        'link': job.get('jobUrl') or job.get('applyUrl') or '',
        'location': job.get('location', ''), 'posted': job.get('postedTime', ''),
        'contract': job.get('contractType', ''), 'source': job.get('_source', ''),
        'analysis': analysis, 'eligibility': state, 'reason': reason,
    }


def persist_candidate_result(conn, job, candidate, result):
    """Audit every result; keep the strongest match without erasing user history."""
    link = result['link']
    score = get_score(result['analysis'])
    outcome = ('rejected' if is_irrelevant_analysis(result['analysis'], score)
               else 'matched' if 55 <= score <= 100 else 'pending_analysis')
    if result.get('eligibility') == 'needs_review':
        outcome = 'needs_review'
    conn.execute('''INSERT INTO job_candidate_results
        (job_url,candidate,job_json,analysis,match_score,outcome,reason,eligibility)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_url,candidate) DO UPDATE SET
        job_json=excluded.job_json,analysis=excluded.analysis,
        match_score=excluded.match_score,outcome=excluded.outcome,
        reason=excluded.reason,eligibility=excluded.eligibility,
        evaluated_at=CURRENT_TIMESTAMP''',
        (link, candidate, json.dumps(job), result['analysis'], score, outcome,
         result.get('reason', '') if outcome != 'rejected' or result.get('eligibility') == 'rejected' else 'Match score below 55; see supporting evidence',
         result.get('eligibility', 'unknown')))
    conn.commit()
    import candidate_tracking
    candidate_tracking.save_result(conn, job, candidate, result['analysis'], score, outcome)
    conn.commit()
    if outcome == 'matched' and application_store:
        state = conn.execute('SELECT applied,notion_page_id FROM candidate_jobs WHERE job_url=? AND candidate=?', (link,candidate)).fetchone()
        if state:
            application_store.ensure_application(
                db_path=os.environ.get('JOB_PIPELINE_DB',DB_PATH), job_url=link, candidate=candidate,
                title=job.get('jobTitle',''), company=job.get('companyName',''), source=job.get('_source',''),
                location=job.get('location',''), match_score=score, notion_page_id=state[1] or '',
                status='applied' if state[0] else 'discovered')
    if outcome != 'matched':
        # jobs_history powers the recommendation dashboard. A later failed
        # rescore should demote the recommendation, while preserving anything
        # the user applied to, starred, skipped, or synced to Notion.
        current = conn.execute('''SELECT candidate,applied,starred,skipped,notion_page_id
                                  FROM jobs_history WHERE job_url=?''', (link,)).fetchone()
        if current and current[0] == candidate and not any(current[1:]):
            conn.execute('DELETE FROM jobs_history WHERE job_url=?', (link,))
            conn.commit()
        return outcome
    discovery_store.record_match(conn, job, candidate)
    current = conn.execute('''SELECT candidate,match_score,applied,starred,skipped,notion_page_id
                              FROM jobs_history WHERE job_url=?''', (link,)).fetchone()
    if current and (any(current[2:]) or (current[1] or 0) >= score):
        return outcome
    ensure_job_in_history(conn, link, job)
    from eligibility import posted_datetime
    posted = posted_datetime(job)
    conn.execute('UPDATE jobs_history SET published_at=? WHERE job_url=?',
                 (posted.isoformat() if posted else None, link))
    conn.commit()
    save_job_analysis(conn, link, candidate, result['analysis'], score)
    return outcome

def _process_job_batch(jobs, all_candidates, reports, include_pending=True):

    # Store winners globally so they don't starve the analysis thread
    global_cover_letter_queue = []

    conn = init_db()
    from job_retention import purge_expired_jobs
    purge_expired_jobs(conn)
    c = conn.cursor()
    c.execute("SELECT job_url FROM processed_jobs")
    processed_urls = {browser_scraper.canonical_linkedin_url(row[0]) for row in c.fetchall()}

    # Resume recent AI failures from saved descriptions without fetching them
    # again. Older runs marked these URLs processed even when scoring failed.
    pending_jobs = {}
    for candidate in (all_candidates if include_pending else {}):
        for (job_json,) in conn.execute("""SELECT job_json FROM job_candidate_results
                WHERE candidate=? AND outcome='pending_analysis'
                  AND evaluated_at>=datetime('now','-7 days')
                ORDER BY evaluated_at DESC LIMIT 20""", (candidate,)):
            job = json.loads(job_json)
            link = job.get('jobUrl') or job.get('applyUrl')
            if link:
                pending_jobs[link] = job
    pending_urls = set(pending_jobs)
    jobs = list(pending_jobs.values()) + [job for job in jobs if (job.get('jobUrl') or job.get('applyUrl')) not in pending_urls]
    if not jobs:
        conn.close()
        return

    recent_db_jobs = get_recent_jobs(conn, days=7)
    accepted_in_memory = []

    valid_jobs = []
    for job in sorted(jobs, key=freshness_key, reverse=True):
        link = job.get('jobUrl') or job.get('applyUrl') or ''
        if not link or (browser_scraper.canonical_linkedin_url(link) in processed_urls and link not in pending_urls):
            continue
        title, company = job.get('jobTitle', ''), job.get('companyName', '')
        duplicate = any(company and ex_company and
            text_overlap_similarity(company, ex_company) >= 0.8 and
            text_overlap_similarity(title, ex_title) >= 0.75
            for ex_title, ex_company in recent_db_jobs + accepted_in_memory)
        if duplicate and link not in pending_urls:
            continue
        if outside_discovery_window(job):
            for candidate in all_candidates:
                persist_candidate_result(conn, job, candidate, {
                    'link': link, 'analysis': '❌ Skipped via hard filter: Posted outside the seven-day discovery window.',
                    'eligibility': 'rejected', 'reason': 'Posted outside the seven-day discovery window'})
            mark_processed(conn, link)
            continue
        # Deduplicate only after basic validation: a description-less copy must not
        # suppress a complete posting from another source.
        if len((job.get('jobDescription') or '').strip()) > 20:
            accepted_in_memory.append((title, company))
        valid_jobs.append(job)

    print(f"📋 {len(valid_jobs)} unique jobs from the last seven days (unknown dates retained for review).")
    if not valid_jobs:
        conn.close()
        return

    # Send the smaller GRC report first so a long Candidate A batch cannot starve it.
    for candidate in ("candidate_b", "candidate_a"):
        resumes = all_candidates.get(candidate, {})
        if not resumes:
            warning = f"⚠️ Job Matcher skipped {candidate.capitalize()}: configured resume is missing or unreadable."
            print(warning)
            send_telegram_msg(warning)
            continue

        send_telegram_msg(f"──────────────\n🤖 **Processing pipeline for: {candidate.upper()}**\nSummarizing candidate profile...")
        resume_summary = summarize_resumes(resumes, candidate_name=candidate)

        update_progress(phase=f"Matching {candidate.capitalize()}")
        candidate_jobs = [job for job in valid_jobs if candidate_matches_track(job, candidate)]
        completed = {row[0] for row in conn.execute(
            "SELECT job_url FROM job_candidate_results WHERE candidate=? AND outcome!='pending_analysis'", (candidate,))}
        candidate_jobs = [job for job in candidate_jobs
                          if (job.get('jobUrl') or job.get('applyUrl')) not in pending_urls
                          or (job.get('jobUrl') or job.get('applyUrl')) not in completed]
        print(f"📋 {candidate.capitalize()}: kept {len(candidate_jobs)} target roles, "
              f"filtered {len(valid_jobs) - len(candidate_jobs)} non-target titles")
        if not candidate_jobs:
            continue

        send_telegram_msg(f"⚡ Processing {len(candidate_jobs)} jobs for {candidate.capitalize()}...")

        # Threaded processing
        results = []
        winners = []

        def process_job(job):
            result = evaluate_candidate_job(job, candidate, resume_summary)
            return result, result['link'], job

        # One chat request at a time avoids exhausting the cloud model's
        # request limit and overloading the local fallback model.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            futures = {executor.submit(process_job, job): job for job in candidate_jobs}
            for future in concurrent.futures.as_completed(futures):
                try:
                    res_dict, link, job_obj = future.result()
                    results.append(res_dict)

                    # Queue winners for background cover letter generation
                    score = get_score(res_dict['analysis'])
                    if score > 80:
                        winners.append((res_dict, job_obj))

                    outcome = persist_candidate_result(conn, job_obj, candidate, res_dict)
                    update_progress(completed=RUN_PROGRESS['completed'] + 1,
                                    matches=RUN_PROGRESS['matches'] + int(outcome == 'matched'))
                    print(f"  📋 {candidate}: {res_dict['title']} — {outcome} ({score}/100)")
                except Exception as e:
                    print(f"  ⚠️ Error processing job in thread: {e}")

        reports.setdefault(candidate, []).extend(results)
        report = build_report(sorted(reports[candidate], key=get_score, reverse=True), candidate.capitalize())
        report_dir = os.environ.get("JOB_REPORT_DIR", str(Path(DB_PATH).parent / "reports"))
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, f"job_report_{candidate}.md"), "w") as handle:
            handle.write(report)

    # Mark SQLite after BOTH batches are processed completely
    for job in valid_jobs:
        link = job.get('jobUrl') or job.get('applyUrl')
        if link and "No link available" not in link:
            pending = conn.execute("SELECT 1 FROM job_candidate_results WHERE job_url=? AND outcome='pending_analysis' LIMIT 1", (link,)).fetchone()
            if not pending and all(all_candidates.values()):
                mark_processed(conn, link)

    conn.close()

RUN_PROGRESS = {"running": False, "completed": 0, "matches": 0, "phase": "Idle"}


def update_progress(**changes):
    RUN_PROGRESS.update(changes)
    import run_status
    try:
        run_status.save(DB_PATH, RUN_PROGRESS)
    except sqlite3.Error as exc:
        print(f'Could not save run progress: {type(exc).__name__}')


def run_job_matcher():
    import fcntl
    lock_path = DB_PATH + '.matcher.lock'
    with open(lock_path, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('A matcher run is already active; duplicate run skipped.')
            return
        try:
            update_progress(running=True, completed=0, matches=0, phase='Loading ready sources',
                            started_at=datetime.now(timezone.utc).isoformat(), target=FORCE_TARGET, error=None)
            all_candidates = extract_resumes()
            reports = {}
            conn = init_db()
            try:
                processed = {browser_scraper.canonical_linkedin_url(row[0]) for row in conn.execute('SELECT job_url FROM processed_jobs')}
            finally:
                conn.close()
            batches = 0
            def process(batch):
                nonlocal batches
                _process_job_batch(batch, all_candidates, reports, include_pending=batches == 0)
                batches += 1
                update_progress(phase='Waiting for remaining sources')
            jobs = scrape_jobs(processed, on_ready=process)
            if not batches:
                process(jobs)
            for candidate, results in reports.items():
                path = os.path.join(os.environ.get("JOB_REPORT_DIR", str(Path(DB_PATH).parent / "reports")), f"job_report_{candidate}.md")
                send_telegram_msg(f"✅ {candidate.capitalize()}'s report ready: {len(results)} roles evaluated.")
                send_telegram_doc(path, caption=f"{candidate.capitalize()} Job Report")
        except BaseException as exc:
            update_progress(running=False, phase='Failed', error=type(exc).__name__ + ': run stopped; saved results are retained.')
            raise
        else:
            update_progress(running=False, phase='Complete')


def handle_help(message):
    bot.reply_to(message, (
        "🤖 *Job Matcher Bot Commands*\n\n"
        "`/scrape` — Scrape new jobs & generate match reports\n"
        "`/scrape local` — Force local Ollama\n"
        "`/scrape windows` — Force Windows Ollama\n"
        "`/trends` — AI trend analysis from recent postings\n"
        "`/jobs [n]` — Show top N recent matches (default 5)\n"
        "`/jobs candidate_a [n]` — Show top matches for Candidate A\n"
        "`/jobs candidate_b [n]` — Show top matches for Candidate B\n"
        "`/status` — Check Ollama & system health\n"
        "`/start` — Welcome message"
    ), parse_mode="Markdown")

def handle_status(message):
    lines = ["📊 *Job Matcher Status*\n"]

    # Ollama endpoints
    for label, url in [("Windows", "http://127.0.0.1:11434"), ("Local", "http://127.0.0.1:11434")]:
        try:
            r = http_session.get(url, timeout=3)
            lines.append(f"✅ Ollama {label}: online")
        except Exception:
            lines.append(f"❌ Ollama {label}: offline")

    # Active endpoint
    if ACTIVE_CHAT_ENDPOINT:
        lines.append(f"🎯 Active model: `{ACTIVE_CHAT_ENDPOINT[1]}`")
    else:
        lines.append("🎯 Active model: not selected yet")

    # Job DB stats
    try:
        conn = init_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM candidate_jobs")
        total = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM candidate_jobs WHERE applied = 1")
        applied = c.fetchone()[0]
        c.execute("SELECT MAX(processed_at) FROM candidate_jobs")
        last = c.fetchone()[0]
        conn.close()
        lines.append(f"\n📋 Total jobs in DB: {total}")
        lines.append(f"✉️ Applied: {applied}")
        lines.append(f"🕒 Last processed: {last[:16] if last else 'never'}")
    except Exception as e:
        lines.append(f"⚠️ DB error: {e}")

    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")

def handle_jobs(message):
    parts = message.text.strip().split()
    candidate_filter = None
    n = 5
    for p in parts[1:]:
        if p.isdigit():
            n = min(int(p), 20)
        elif p.lower() in ("candidate_a", "candidate_b"):
            candidate_filter = p.lower()

    try:
        conn = init_db()
        c = conn.cursor()
        query = "SELECT title, company, candidate, match_score, applied, job_url FROM candidate_jobs WHERE match_score IS NOT NULL AND COALESCE(skipped,0)=0"
        params = []
        if candidate_filter:
            query = ("SELECT title,company,candidate,match_score,applied,job_url FROM candidate_jobs "
                     "WHERE candidate=? AND COALESCE(skipped,0)=0")
            params.append(candidate_filter)
        query += " ORDER BY match_score DESC LIMIT ?"
        params.append(n)
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()

        if not rows:
            bot.reply_to(message, "No scored jobs found yet. Run `/scrape` first.", parse_mode="Markdown")
            return

        label = f" ({candidate_filter.capitalize()})" if candidate_filter else ""
        lines = [f"🏆 *Top {len(rows)} Job Matches{label}*\n"]
        for title, company, cand, score, applied, url in rows:
            status = "✉️" if applied else "🔵"
            cand_tag = f" [{cand}]" if not candidate_filter else ""
            lines.append(f"{status} *{score}%* — {title} @ {company}{cand_tag}")
        bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"⚠️ Error fetching jobs: {e}")

def handle_start(message):
    bot.reply_to(message, "👋 Welcome to the Job Matcher Bot!\nSend `/help` to see all available commands.", parse_mode="Markdown")

def handle_scrape(message):
    global FORCE_TARGET, ACTIVE_CHAT_ENDPOINT, ACTIVE_EMBED_ENDPOINT
    text = message.text.lower()
    if 'local' in text:
        FORCE_TARGET = "local"
        ACTIVE_CHAT_ENDPOINT = ACTIVE_EMBED_ENDPOINT = None
        bot.reply_to(message, "🚀 Scraper starting! [Target: Ollama Cloud (gpt-oss:20b-cloud)]")
    elif 'windows' in text:
        FORCE_TARGET = os.environ.get("OLLAMA_TARGET", "default")
        ACTIVE_CHAT_ENDPOINT = ACTIVE_EMBED_ENDPOINT = None
        bot.reply_to(message, "🚀 Scraper starting! [Target: Windows Ollama (gpt-oss:20b)]")
    else:
        FORCE_TARGET = os.environ.get("OLLAMA_TARGET", "default")
        ACTIVE_CHAT_ENDPOINT = ACTIVE_EMBED_ENDPOINT = None
        bot.reply_to(message, "🚀 Scraper starting! [Target: Auto-Fallback Model Mode]")

    # Run the scraper in a background thread so the bot remains responsive
    threading.Thread(target=run_job_matcher).start()

def handle_trends(message):
    global FORCE_TARGET, ACTIVE_CHAT_ENDPOINT, ACTIVE_EMBED_ENDPOINT
    text = message.text.lower()
    if 'local' in text:
        FORCE_TARGET = "local"
        ACTIVE_CHAT_ENDPOINT = ACTIVE_EMBED_ENDPOINT = None
        bot.reply_to(message, "📊 Starting AI Trend Analysis! [Target: Ollama Cloud (gpt-oss:20b-cloud)]")
    elif 'windows' in text:
        FORCE_TARGET = os.environ.get("OLLAMA_TARGET", "default")
        ACTIVE_CHAT_ENDPOINT = ACTIVE_EMBED_ENDPOINT = None
        bot.reply_to(message, "📊 Starting AI Trend Analysis! [Target: Windows Ollama (gpt-oss:20b)]")
    else:
        FORCE_TARGET = os.environ.get("OLLAMA_TARGET", "default")
        ACTIVE_CHAT_ENDPOINT = ACTIVE_EMBED_ENDPOINT = None
        bot.reply_to(message, "📊 Starting AI Trend Analysis! [Target: Auto-Fallback Mode]")

    def run_analyzer():
        try:
            import trend_analyzer
            trend_analyzer.analyze_trends()
        except Exception as e:
            send_telegram_msg(f"⚠️ Error running trend analyzer: {e}")

    threading.Thread(target=run_analyzer).start()



def authorized_chat(message):
    return bool(TELEGRAM_CHAT_ID) and str(message.chat.id) == TELEGRAM_CHAT_ID


if bot is not None:
    bot.message_handler(commands=['help'], func=authorized_chat)(handle_help)
    bot.message_handler(commands=['status'], func=authorized_chat)(handle_status)
    bot.message_handler(commands=['jobs'], func=authorized_chat)(handle_jobs)
    bot.message_handler(commands=['start'], func=authorized_chat)(handle_start)
    bot.message_handler(commands=['scrap', 'scrape'], func=authorized_chat)(handle_scrape)
    bot.message_handler(commands=['trends'], func=authorized_chat)(handle_trends)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--scrape-now":
        print("🤖 Running on-demand scrape via cron...")
        run_job_matcher()
        print("🤖 Cron scrape complete. Exiting.")
        sys.exit(0)

    if bot is None:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or run launcher.py for the dashboard.")

    print("🤖 Job Matcher Bot is online and listening for commands...")
    try:
        bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    polling_backoff = 30
    max_polling_backoff = 300
    while True:
        try:
            bot.polling(non_stop=True, timeout=60, long_polling_timeout=30, allowed_updates=["message"])
            polling_backoff = 30
        except Exception as e:
            err_str = str(e)
            print(f"⚠️ Telegram polling exception: {err_str}")
            if "NameResolutionError" in err_str or "Temporary failure in name resolution" in err_str:
                print("💥 CRITICAL DNS FAILURE. Exiting to allow Docker auto-heal restart.")
                sys.exit(1)
            print(f"🔄 Reconnecting in {polling_backoff}s...")
            time.sleep(polling_backoff)
            polling_backoff = min(max_polling_backoff, polling_backoff * 2)
