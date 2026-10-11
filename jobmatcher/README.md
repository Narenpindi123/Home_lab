# JobMatcher

A sanitized source snapshot of a personal job-search tool. It collects public listings, applies eligibility checks, and uses Ollama to help assess fit against locally supplied resumes. The Flask dashboard supports review, application tracking, and evidence-based preparation drafts.

This is the application source behind the [project notes](../docs/jobmatcher.md), adapted for a fresh installation. The private deployment has not been changed.

## Included

- Local LinkedIn, Dice, and employer-board collectors, plus additional bounded discovery collectors.
- A token-protected local source API, SQLite storage, and per-source health records.
- Candidate matching, incomplete-listing review, resume export, and application tracking.
- The dashboard, Docker setup, and regression tests using synthetic data.

Snippet-only listings remain `needs_review` and bypass automatic AI scoring. Browser challenges and partial results remain visible. Source availability can change independently of this code.

## Start with Docker Compose

Requires Docker with Compose. Run these commands from this directory:

```bash
cp .env.example .env
mkdir -p data/reports resumes
docker compose up -d --build
docker compose exec ollama ollama pull qwen2.5:7b
```

Open **http://127.0.0.1:5555**. If you change `OLLAMA_MODEL` in `.env`, pull that model instead and recreate the application container with `docker compose up -d`.

The setup runs the dashboard, one shared source API, and a local Ollama instance. It starts with an empty database and does not scrape or send messages on startup. Use the dashboard to start collection after adding a resume. The shared source API serializes its queued collection runs.

The dashboard has no built-in account login. Its host port is intentionally bound to loopback; do not expose it directly to the internet. For remote access, use a private authenticated connection or an authenticated reverse proxy.

## Candidates and resumes

This snapshot preserves two generic profiles from the original workflow:

| Candidate | Default track | Local PDF |
| --- | --- | --- |
| `candidate_a` | Entry-level security and IT | `resumes/candidate_a.pdf` |
| `candidate_b` | Entry-level GRC | `resumes/candidate_b.pdf` |

Supply your own PDFs. A missing resume causes that candidate to be skipped; a single candidate can be used without filling both slots. Names, employment histories, education, and certifications are not supplied by the public configuration.

The current role filters target early-career roles and use specific experience, location, and clearance rules. Review `matching_rules.py`, `eligibility.py`, and `job_matcher.py` before relying on the results for a different search. Resume ownership is defined in `candidate_resumes.py`.

Keep resumes and generated outputs private. Git and Docker ignore rules exclude them, but review staged changes before publishing any future modifications.

## Optional Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in your local `.env`, then recreate the application container. When either value is missing, the dashboard works without Telegram. Bot commands are restricted to the configured chat.

No token is shipped with this code. Do not use a real bot token in a source file or commit `.env`.

## Other integrations and limits

- Ollama runs locally in the example. Cloud access is opt-in through `OLLAMA_API_KEY` or an explicitly configured `OLLAMA_CLOUD_ENV_FILE`; choosing cloud access can transmit resume and listing content to that provider.
- The source API creates its own token under `data/sources/`. It has no published host port, and the dashboard reads the token through the shared data volume.
- SearXNG discovery is optional. Set `SEARCH_API_URL` for the relevant containers if you operate a compatible search endpoint; some fallback sources will report unavailable without it.
- Indeed and Glassdoor scripts are included, but scheduled collection/browser sessions are not configured by this Compose file. Their missing snapshots are reported in source health.
- Email sorting and Notion integrations from the private environment are not configured here. The dashboard may show empty or unavailable integration panels until those services are supplied.
- No auto-application or automatic LinkedIn publishing is provided.

## Tests

Using Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s . -p 'test_*.py'
```

The tests use temporary databases and synthetic candidate/job data. PDF checks need a serif font such as Liberation Serif; the Docker image includes the font package and Poppler tools.

## Excluded from this export

Credentials and real `.env` files; resumes and candidate histories; job reports and application records; databases; email contents; browser profiles/cookies; screenshots; cached pages; deployment backups; local agent settings; and the private email/Notion configuration.

The original application was tailored to a personal workflow. This export makes the core runnable with generic settings; it is not a claim of production readiness or hiring-score accuracy.
