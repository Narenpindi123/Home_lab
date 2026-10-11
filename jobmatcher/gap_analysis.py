"""Candidate-specific gaps from verified scoring quotes, never keyword demand alone."""
import html
import json
import re
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from grounded_scoring import verified_excerpt

SKILLS_MAP = {
    "SIEM / MDR / EDR": [
        "Splunk", "Sentinel", "CrowdStrike", "QRadar",
        "Defender", "Elastic", "Wazuh", "Cortex"
    ],
    "Cloud": ["AWS", "Azure", "GCP", "Kubernetes", "Docker"],
    "Tools & Network": ["Wireshark", "Nmap", "Nessus", "Burp Suite", "Active Directory", "Linux", "Windows"],
    "Languages & Scripting": ["Python", "Bash", "PowerShell", "Go", "JavaScript"],
    "Certifications": ["Security+", "CISSP", "CEH", "OSCP", "CySA+", "GSEC"],
    "GRC / Compliance": ["GRC", "ISO 27001", "NIST", "SOX", "HIPAA", "PCI DSS", "Risk Management", "Compliance", "Audit", "Governance"],
}


def mentions(text, skill):
    return bool(re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", text, re.I))


def track(title):
    if re.search(r"grc|risk|compliance|audit|governance|assurance|privacy", title, re.I):
        return "GRC"
    if re.search(r"support|help.?desk|network|noc|system|cloud", title, re.I):
        return "Support / infrastructure"
    return "SOC / security"


def build_report(conn, candidate, profile, days=14):
    profile = profile.split('CANDIDATE POLICY CONTEXT (not resume evidence):')[0]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("SELECT job_url,job_json,analysis FROM job_candidate_results WHERE candidate=? AND eligibility='eligible' AND match_score IS NOT NULL AND outcome IN ('matched','rejected') AND datetime(evaluated_at)>=datetime(?)", (candidate, cutoff)).fetchall()
    closed = set()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='listing_checks'").fetchone():
        for url, raw in conn.execute('SELECT job_url,result_json FROM listing_checks'):
            if json.loads(raw).get('state') == 'closed':
                closed.add(url)
    gaps, seen_jobs, excluded = {}, set(), set()
    for url, raw, analysis in rows:
        if url in closed or urlparse(url).scheme not in ('https', 'http'):
            continue
        job = json.loads(raw)
        description = job.get('jobDescription') or ''
        title = job.get('jobTitle') or ''
        section = analysis.split('**Requirements to verify', 1)
        if len(section) != 2:
            continue
        seen_jobs.add(url)
        quotes = re.findall(r'- Job: `([^`]+)`', section[1].split('**Resume guidance:', 1)[0])
        for quote in quotes:
            quote = html.unescape(quote)
            original = verified_excerpt(quote, description)
            if not original:
                continue
            for skills in SKILLS_MAP.values():
                for skill in skills:
                    if not mentions(original, skill):
                        continue
                    if mentions(profile, skill):
                        excluded.add(skill)
                        continue
                    # Section headings matter when an individual bullet omits 'preferred'.
                    index = description.find(original)
                    prefix = description[:index] if index >= 0 else ''
                    headings = list(re.finditer(r'preferred qualifications|nice to have|required qualifications|minimum qualifications', prefix, re.I))
                    context = (headings[-1].group(0) if headings else '') + ' ' + original
                    importance = 'preferred' if re.search(r'preferred|nice to have|bonus', context, re.I) else 'required' if re.search(r'required|must|minimum', context, re.I) else 'unspecified'
                    key = (track(title), skill)
                    item = gaps.setdefault(key, {'track': key[0], 'skill': skill, 'required': 0, 'preferred': 0, 'unspecified': 0, 'evidence': []})
                    if any(e['url'] == url for e in item['evidence']):
                        continue
                    item[importance] += 1
                    item['evidence'].append({'url': url, 'title': title, 'quote': original, 'importance': importance})
    return {'candidate': candidate, 'days': days, 'jobs_analyzed': len(seen_jobs), 'generated_at': datetime.now(timezone.utc).isoformat(),
            'gaps': sorted(gaps.values(), key=lambda g: (-g['required'], -len(g['evidence']), g['skill'])),
            'already_evidenced': sorted(excluded),
            'limitations': 'Coverage is limited to the configured skill vocabulary and stored, verified missing-requirement quotes. Not demonstrated is not proof of inability; unspecified importance is not treated as required.'}
