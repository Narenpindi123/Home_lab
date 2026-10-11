"""Retry recent AI failures with the installed local model, one job at a time."""

import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import job_matcher as matcher


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", choices=("candidate_b", "candidate_a"), required=True)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    resumes = matcher.extract_resumes().get(args.candidate, {})
    if not resumes:
        raise RuntimeError(f"No resume available for {args.candidate}")
    profile = matcher.summarize_resumes(resumes, args.candidate)
    matcher.FORCE_TARGET = "local"
    matcher.ACTIVE_CHAT_ENDPOINT = ("http://127.0.0.1:11434", "gemma4:e2b")
    matcher.MODEL_NAME = "gemma4:e2b"
    matcher.OLLAMA_EMBEDDINGS_DISABLED_UNTIL = time.time() + 86400

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = matcher.init_db()
    rows = conn.execute(
        """SELECT job_json FROM job_candidate_results
           WHERE candidate=? AND outcome='pending_analysis' AND evaluated_at>=?
           ORDER BY evaluated_at DESC LIMIT ?""",
        (args.candidate, cutoff, args.limit),
    ).fetchall()
    print(f"Retrying {len(rows)} {args.candidate} pending analyses", flush=True)
    matches = []
    for index, (job_json,) in enumerate(rows, 1):
        job = json.loads(job_json)
        result = matcher.evaluate_candidate_job(job, args.candidate, profile)
        outcome = matcher.persist_candidate_result(conn, job, args.candidate, result)
        print(f"{index}/{len(rows)} {outcome}: {job.get('jobTitle', '')}", flush=True)
        if outcome == "matched":
            matches.append((job.get("jobTitle", ""), job.get("companyName", ""), result["link"]))
    conn.close()
    print(f"New matches: {len(matches)}", flush=True)


if __name__ == "__main__":
    main()
