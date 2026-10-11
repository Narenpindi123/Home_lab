"""Read-only, citation-preserving public web research for interview preparation."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


SEARCH_API_URL = os.environ.get("SEARCH_API_URL", "http://127.0.0.1:8888/search")
MAX_SOURCES = max(3, min(int(os.environ.get("WEB_RESEARCH_MAX_SOURCES", "8")), 15))
MAX_PAGE_BYTES = 1_500_000
USER_AGENT = "JobResearchCopilot/1.0 (+local read-only research)"


class ResearchError(RuntimeError):
    pass


def _is_public_host(hostname):
    """Reject internal names and every non-public resolved address."""
    if not hostname:
        return False
    lowered = hostname.rstrip(".").lower()
    if lowered in {"localhost", "host.docker.internal"} or lowered.endswith((".local", ".internal", ".lan")):
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(lowered, None)}
    except socket.gaierror:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return False
    return True


def _validate_public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResearchError("Only public HTTP(S) URLs are allowed")
    if parsed.username or parsed.password or (parsed.port and parsed.port not in {80, 443}):
        raise ResearchError("URL contains disallowed credentials or port")
    if not _is_public_host(parsed.hostname):
        raise ResearchError("Private or unresolvable destination blocked")
    return url


def search(query, count=5):
    response = requests.get(
        SEARCH_API_URL,
        params={"q": query, "format": "json", "safesearch": 1, "language": "en-US"},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=(4, 20),
    )
    response.raise_for_status()
    items = []
    for result in response.json().get("results", []):
        url = str(result.get("url") or "").strip()
        try:
            _validate_public_url(url)
        except ResearchError:
            continue
        items.append({
            "url": url,
            "title": re.sub(r"\s+", " ", str(result.get("title") or "")).strip()[:300],
            "search_snippet": re.sub(r"\s+", " ", str(result.get("content") or "")).strip()[:1000],
            "engine": str(result.get("engine") or ""),
        })
        if len(items) >= count:
            break
    return items


def fetch_page(url):
    """Fetch with redirect validation to avoid SSRF through redirect chains."""
    session = requests.Session()
    current = url
    for _ in range(4):
        _validate_public_url(current)
        response = session.get(
            current, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=(5, 15), stream=True, allow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ResearchError("Redirect has no destination")
            current = urljoin(current, location)
            continue
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "html" not in content_type and "text/plain" not in content_type:
            response.close()
            raise ResearchError("Unsupported page type")
        chunks, total = [], 0
        for chunk in response.iter_content(32768):
            total += len(chunk)
            if total > MAX_PAGE_BYTES:
                break
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        response.close()
        raw = b"".join(chunks).decode(encoding, errors="replace")
        soup = BeautifulSoup(raw, "html.parser")
        for node in soup(["script", "style", "nav", "footer", "form", "noscript", "svg"]):
            node.decompose()
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        return current, title[:300], text[:9000]
    raise ResearchError("Too many redirects")


def research_company(company, job_title="", interviewer_name=""):
    year = datetime.now(timezone.utc).year
    query_specs = [
        ("company", f'"{company}" official company products services about'),
        ("news", f'"{company}" news {year} OR {year - 1}'),
        ("security", f'"{company}" cybersecurity incident breach security {year} OR {year - 1}'),
        ("competitors", f'"{company}" competitors industry market'),
    ]
    if interviewer_name:
        query_specs.append(
            ("interviewer", f'"{interviewer_name}" "{company}" {job_title} interview'),
        )

    candidates = []
    search_errors = []
    for category, query in query_specs:
        try:
            for item in search(query, count=4):
                item.update({"category": category, "query": query})
                candidates.append(item)
        except Exception as exc:
            search_errors.append(f"{category}: {exc}")

    sources, seen = [], set()
    # Preserve category diversity before filling remaining slots.
    grouped = [[item for item in candidates if item["category"] == category] for category, _ in query_specs]
    ordered = []
    for index in range(max((len(group) for group in grouped), default=0)):
        ordered.extend(group[index] for group in grouped if index < len(group))
    for item in ordered:
        canonical = item["url"].split("#", 1)[0]
        if canonical in seen:
            continue
        seen.add(canonical)
        try:
            final_url, fetched_title, page_text = fetch_page(canonical)
            excerpt = page_text
            fetch_status = "fetched"
        except Exception as exc:
            final_url, fetched_title = canonical, item["title"]
            excerpt = item["search_snippet"]
            fetch_status = f"search snippet only: {type(exc).__name__}"
        if not excerpt:
            continue
        sources.append({
            "source_id": f"S{len(sources) + 1}",
            "category": item["category"],
            "title": fetched_title or item["title"],
            "url": final_url,
            "excerpt": excerpt[:4500],
            "retrieval_status": fetch_status,
        })
        if len(sources) >= MAX_SOURCES:
            break

    return {
        "researched_at": datetime.now(timezone.utc).isoformat(),
        "company": company,
        "job_title": job_title,
        "interviewer_name": interviewer_name or None,
        "sources": sources,
        "search_errors": search_errors,
        "status": "complete" if sources else "unavailable",
    }
