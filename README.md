# Homelab Infrastructure

This repository contains my personal homelab Docker Compose configurations. It serves as an off-site backup of my infrastructure as code.

## Architecture & Services
My self-hosted environment includes:

- **Media & Reading**: Komga, Navidrome, Romm
- **Productivity & Notes**: Vikunja, Matter
- **Networking & Access**: Tailscale, AdGuard Home
- **Management & Backup**: Duplicati, Watchtower, Uptime Kuma

## Note
> **⚠️ Security Warning:** This is an exported configuration repository. All sensitive data such as API keys, passwords, and tokens have been automatically redacted (`<REDACTED>`) to prevent secret leakage. Due to this, these files **cannot be cloned and run as-is**. You will need to supply your own passwords and environmental variables to successfully spin up these services.
