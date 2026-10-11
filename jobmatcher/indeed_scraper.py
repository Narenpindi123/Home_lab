#!/usr/bin/env python3
"""
Indeed scraper using python-jobspy (no API key needed).
Writes scraped jobs to data/indeed_jobs.json for the Docker pipeline to consume.
"""

import os
import sys
import json
import time

LAST_HEALTH = {"errors": [], "status": "unknown"}
from time_utils import local_now
from pathlib import Path

try:
    from jobspy import scrape_jobs
except ImportError:
    os.system(f"{sys.executable} -m pip install python-jobspy -q")
    from jobspy import scrape_jobs

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = os.environ.get("INDEED_OUTPUT_FILE", str(SCRIPT_DIR / "data" / "indeed_jobs.json"))

INDEED_SEARCH_QUERIES = [
    # Candidate A targets — entry-level security + IT ops
    "SOC Analyst entry level",
    "Cybersecurity Analyst entry level",
    "Information Security Analyst",
    "IT Security Analyst",
    "Security Operations Center Analyst",
    "IT Support Specialist",
    "Help Desk Analyst",
    "IT Technician",
    "Systems Administrator entry level",
    "Network Administrator entry level",
    "IAM Analyst entry level",
    "Identity and Access Management Analyst",
    "Vulnerability Management Analyst entry level",
    "Security Awareness Analyst",
    "NOC Analyst entry level",
    "Network Support Specialist",
    "Technical Support Engineer entry level",
    "Cloud Support Associate",
    # Candidate B targets — GRC/compliance
    "IT GRC Analyst",
    "Cyber Compliance Analyst",
    "Risk and Compliance Analyst",
    "Third Party Risk Analyst entry level",
    "Vendor Risk Analyst entry level",
    "Technology Controls Analyst",
    "IT Auditor entry level",
    # Shared
    "Cloud Security Engineer entry level",
    "Penetration Tester junior",
]

SENIOR_KEYWORDS = ["senior", "lead", "manager", "director", "principal", "staff", "vp", "architect"]


def scrape_indeed():
    print("🕷️  Indeed scraper starting (jobspy)...")
    LAST_HEALTH.update(errors=[], status="unknown")
    all_jobs = []
    seen_urls = set()

    for i, query in enumerate(INDEED_SEARCH_QUERIES):
        print(f"\n  📋 Search {i+1}/{len(INDEED_SEARCH_QUERIES)}: {query}")
        try:
            df = scrape_jobs(
                site_name=["indeed"],
                search_term=query,
                location="United States",
                results_wanted=100,
                hours_old=168,
                country_indeed="USA",
            )

            if df is None or df.empty:
                print(f"    ⚠️  No results returned")
                continue

            count = 0
            for _, row in df.iterrows():
                try:
                    title = str(row.get("title", "") or "")
                    if not title:
                        continue


                    url = str(row.get("job_url", "") or "")
                    if not url or url in seen_urls:
                        continue

                    description = str(row.get("description", "") or "")
                    seen_urls.add(url)
                    all_jobs.append({
                        "jobTitle": title,
                        "companyName": str(row.get("company", "Unknown") or "Unknown"),
                        "jobDescription": description,
                        "location": str(row.get("location", "Not specified") or "Not specified"),
                        "jobUrl": url,
                        "applyUrl": url,
                        "publishedAt": str(row.get("date_posted", "") or ""),
                        "postedTime": str(row.get("date_posted", "Recently") or "Recently"),
                        "contractType": str(row.get("job_type", "Unknown") or "Unknown"),
                        "_source": "Indeed",
                    })
                    count += 1
                except Exception:
                    continue

            print(f"    ✅ Kept {count} jobs after filtering")
            time.sleep(2)  # be polite between searches

        except Exception as e:
            LAST_HEALTH["errors"].append(f"Search {query}: {type(e).__name__}")
            print(f"    ❌ Search '{query}' failed: {e}")

    missing = sum(len((job.get("jobDescription") or "").strip()) <= 20 for job in all_jobs)
    if missing:
        LAST_HEALTH["errors"].append(f"{missing} jobs have missing descriptions")
    LAST_HEALTH["status"] = "partial" if all_jobs and LAST_HEALTH["errors"] else "error" if LAST_HEALTH["errors"] else "success"
    return all_jobs


def save_jobs(jobs):
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    output = {
        "scraped_at": local_now().isoformat(),
        "count": len(jobs),
        "errors": LAST_HEALTH["errors"],
        "status": LAST_HEALTH["status"],
        "jobs": jobs,
    }
    tmp_file = OUTPUT_FILE + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(output, f, indent=2)
    os.replace(tmp_file, OUTPUT_FILE)
    from discovery_store import record_host_snapshot
    record_host_snapshot(OUTPUT_FILE, 'Indeed', output)
    print(f"\n💾 Saved {len(jobs)} jobs to {OUTPUT_FILE}")


def main():
    start = time.time()
    try:
        jobs = scrape_indeed()
    except Exception as e:
        LAST_HEALTH.update(errors=[f"Scraper: {type(e).__name__}"], status="error")
        print(f"⚠️  Error in main: {e}")
        jobs = []

    elapsed = time.time() - start
    print(f"\n📊 Indeed scrape complete: {len(jobs)} jobs in {elapsed:.1f}s")

    if jobs:
        with_desc = [j for j in jobs if len(j.get("jobDescription", "").strip()) > 20]
        print(f"  📋 {len(with_desc)}/{len(jobs)} jobs have descriptions")
        save_jobs(jobs)
        print(f"\n📝 Sample jobs:")
        for j in jobs[:5]:
            print(f"   • {j['jobTitle']} at {j['companyName']} ({j['location']}) [desc: {len(j.get('jobDescription',''))} chars]")
    else:
        print("⚠️  No jobs found.")
        save_jobs(jobs)

    return len(jobs)


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result > 0 else 1)
