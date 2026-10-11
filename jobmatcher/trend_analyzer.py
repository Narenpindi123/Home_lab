"""Scheduled report shares the dashboard's evidence-backed gap analysis."""
import os
import sqlite3
import job_matcher
from gap_analysis import build_report, SKILLS_MAP

DB_PATH = os.environ.get('DB_PATH', '/app/data/processed_jobs.db')
DAYS_TO_ANALYZE = 14


def analyze_trends():
    profiles = job_matcher.extract_resumes()
    with sqlite3.connect(DB_PATH) as conn:
        for candidate, resumes in profiles.items():
            if not resumes:
                continue
            report = build_report(conn, candidate, '\n\n'.join(resumes.values()), DAYS_TO_ANALYZE)
            lines = [f"Skill gaps for {candidate}: {report['jobs_analyzed']} eligible postings, last {DAYS_TO_ANALYZE} days."]
            for gap in report['gaps'][:8]:
                lines.append(f"{gap['track']} / {gap['skill']}: {gap['required']} required, {gap['preferred']} preferred, {gap['unspecified']} unspecified")
                for evidence in gap['evidence'][:2]:
                    lines.append(evidence['title'] + ': ' + evidence['url'])
            if not report['gaps']:
                lines.append('No verified gaps in the configured vocabulary. This is not a guarantee of fit.')
            lines.append(report['limitations'])
            job_matcher.send_telegram_msg('\n'.join(lines))


if __name__ == '__main__':
    analyze_trends()
