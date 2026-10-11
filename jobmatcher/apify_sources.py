"""Compatibility entrypoint for retired scripts. All collection is local now."""
from local_job_sources import configured_sources
from local_sources_client import scrape_source
import discovery_store


def scrape_jobs(conn):
    jobs = []
    for source, name in [('ats', 'Employer boards'), ('dice', 'Dice')]:
        try:
            found, health = scrape_source(source)
            discovery_store.record_run(conn, name, found, errors=health['errors'], status=health['status'])
            jobs.extend(found)
        except Exception as exc:
            discovery_store.record_run(conn, name, [], errors=['Local API: ' + type(exc).__name__])
    return jobs
