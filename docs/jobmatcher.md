# JobMatcher: job searching while learning

JobMatcher is a personal project for collecting job listings, reviewing their requirements, and using AI to help assess fit. It runs alongside the other homelab services. This page describes the local implementation reviewed in October 2026; it does not ship the application or candidate data.

## Collection

The collection layer has moved from paid Apify actors to local collectors. Implemented sources include LinkedIn public endpoints, Dice listings and structured job data, and employer boards using Greenhouse, Lever, and Ashby.

Collection and matching are separate steps. Local source services retain run status and datasets, while the matcher applies eligibility checks and records candidate results in SQLite. A source returning something does not automatically make it a verified or suitable job.

Some sites present browser-verification challenges or rate limits. Search-index discovery can still produce links, but those records may be missing the full description, publication date, or confirmation that the vacancy remains open. They are retained for review rather than reported as complete scrapes.

## Incomplete listings

The matching path handles incomplete evidence explicitly:

1. Check the listing against the configured eligibility and role criteria.
2. Mark missing descriptions and search-snippet-only records `needs_review`, unless the listing has already been rejected.
3. Skip automatic AI analysis while review is required.
4. Preserve the review state and reason in the stored candidate result.

For example, a title and snippet may suggest a relevant security role while omitting a mandatory experience requirement. Showing a match percentage at that point could make the result look more thoroughly checked than it was.

## Freshness and source health

The collectors use source publication dates where available. Fetch times and modification labels are not substituted for a missing publication date. The matcher uses a configured freshness window; an accessible listing can still be too old for that window.

The dashboard exposes source health and collection status. Partial coverage, verification challenges, and empty results have different meanings and should remain distinguishable.

## What this work covers

The project provides practical work with public data sources, API integration, Docker services, persistent state, and AI inputs whose completeness varies. Current development focuses on making the review decisions understandable and keeping collection limitations visible.

It remains a personal tool. This documentation does not claim that every source works on every run, that AI scores are calibrated hiring predictions, or that a collected vacancy is still accepting applications.

## Public scope

This page is an overview, not installation instructions. The application currently shares its local environment with personal resumes, credentials, and notification integrations. Those files have not been copied into this public repository.
