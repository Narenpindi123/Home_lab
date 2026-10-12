# Naren's homelab

A personal Linux and Docker homelab for self-hosted applications, automation, and hands-on learning. This repository contains public documentation, a sanitized JobMatcher source snapshot, and an older, redacted configuration export.

I'm exploring opportunities in software engineering, applied AI/ML, cloud/DevOps, security, and IT. This homelab gives me a place to build Python applications, integrate local LLMs, and work with Linux infrastructure.

## Current setup

Reviewed against the local deployment configuration in October 2026. This is a configuration inventory, not a claim that every service has been tested or is currently healthy.

| Area | Configured services |
| --- | --- |
| Home and personal organization | Home Assistant, Grocy, Homebox, Mealie |
| Media and libraries | Jellyfin, Navidrome, Komga, RomM |
| Documentation | BookStack |
| Management and monitoring | Homepage, Portainer, Uptime Kuma |
| DNS, notifications, and backups | AdGuard Home, ntfy, Duplicati |
| Local AI and search | Ollama, Open WebUI, SearXNG |
| Custom automation | JobMatcher, local job-source APIs, scheduled collectors, email sorting |

The current deployment uses a consolidated Compose stack with persistent application data. Many service images are pinned by digest. The older files under [`docker/`](docker/) show a previous layout and should not be treated as the current deployment or a ready-to-run installer.

## Recent work: JobMatcher

JobMatcher brings job listings together for review and AI-assisted matching. Recent work includes local collectors for public job sources and a review state for incomplete listings.

A search snippet can help discover a role, but it does not provide enough information for a reliable match score. Snippet-only listings remain marked `needs_review` and bypass automatic AI scoring. Source failures and partial results remain visible.

[Browse the JobMatcher source and setup instructions](jobmatcher/) or [read the project notes](docs/jobmatcher.md).

## Repository boundaries

- `docker/` is a historical, redacted export. It requires review and real configuration before use.
- `jobmatcher/` is a sanitized source snapshot with generic candidate profiles and environment-based credentials. The live deployment and personal data remain private.
- Credentials, resumes, account settings, application databases, browser profiles, and runtime data are not part of this documentation update.
- Monitoring and backup services being configured does not establish an uptime percentage or prove that a restore will succeed.

## Related projects

- [Scam Shield](https://github.com/Narenpindi123/Scam_msg_finder): Android app with local, rule-based checks for suspicious messages.
- [Enterprise network lab](https://github.com/Narenpindi123/enterprise-network-lab): Linux namespaces, routing, DNS, and firewall exercises.
- [Enterprise SOC lab](https://github.com/Narenpindi123/enterprise-soc-lab): simulated attack telemetry, detection rules, and an analyst dashboard.
- [IRAF](https://github.com/Narenpindi123/IRAF): Linux investigation data collection and reporting.

These are personal and educational projects. Lab behavior and repository documentation should not be read as production performance guarantees.
