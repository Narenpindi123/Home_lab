"""Conservative, read-only listing checks using the existing public URL boundary."""
import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import web_research


def check(url, title, fetch=web_research.fetch_page):
    result = {"state": "unconfirmed", "checked_at": datetime.now(timezone.utc).isoformat(),
              "url": url, "evidence": "No current posting evidence available."}
    try:
        final_url, page_title, text = fetch(url)
        result["url"] = final_url
        if re.search(r"captcha|verify you are human|access denied|sign in to continue|log in to continue", text, re.I):
            result["evidence"] = "Login or access challenge; availability could not be confirmed."
            return result
        closed = re.search(r"no longer accepting applications|(?:this|the) (?:job|position|posting) (?:is|has been) (?:closed|filled|removed)|job (?:has )?expired", text, re.I)
        words = set(re.findall(r"[a-z]{3,}", title.lower())) - {"the", "and", "job"}
        title_matches = words and all(re.search(r"\b" + re.escape(w) + r"\b", (page_title + " " + text[:1200]).lower()) for w in words)
        if closed and (title_matches or urlparse(final_url).path == urlparse(url).path):
            result.update(state="closed", evidence=closed.group(0))
        elif title_matches and len(text) >= 250 and re.search(r"apply (?:now|for|to)|submit (?:your |an )?application", text, re.I):
            result.update(state="open", evidence="Role title, substantive page content and application invitation found.")
        else:
            result["evidence"] = "Page loaded, but a current role and application invitation were not both confirmed."
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (404, 410):
            result.update(state="closed", evidence=f"Posting returned HTTP {exc.response.status_code}.")
        else:
            result["evidence"] = "HTTP error; availability could not be confirmed."
    except (requests.RequestException, web_research.ResearchError, ValueError):
        result["evidence"] = "Fetch failed or destination blocked; availability could not be confirmed."
    return result


def get(conn, url):
    conn.execute("CREATE TABLE IF NOT EXISTS listing_checks (job_url TEXT PRIMARY KEY, result_json TEXT NOT NULL)")
    row = conn.execute("SELECT result_json FROM listing_checks WHERE job_url=?", (url,)).fetchone()
    return json.loads(row[0]) if row else None


def verify(conn, url, title, force=False):
    result = get(conn, url)
    if result and not force and datetime.fromisoformat(result["checked_at"]) > datetime.now(timezone.utc) - timedelta(hours=24):
        return result
    result = check(url, title)
    conn.execute("INSERT INTO listing_checks VALUES (?,?) ON CONFLICT(job_url) DO UPDATE SET result_json=excluded.result_json",
                 (url, json.dumps(result)))
    conn.commit()
    return result
