# My Self-Hosted Homelab Infrastructure

Welcome to my homelab repository! This project serves as a showcase of my ability to design, deploy, and manage a self-hosted infrastructure environment using Infrastructure as Code (IaC) principles. 

All of the services defined here are actively running on my local server, securely managing my media, productivity tools, and networking.

## Architecture & Deployed Services
My environment is containerized using Docker and Docker Compose, organized into the following functional stacks:

- **Media & Content**: [Komga] (Comics/Manga reading), [Navidrome] (Music streaming), [Romm] (Retro game library management)
- **Productivity & Notes**: [Vikunja] (Task management), [Matter] (Read-it-later application)
- **Networking & Security**: [Tailscale] (Zero-trust VPN overlay), [AdGuard Home] (Network-wide ad and tracker blocking)
- **Infrastructure Management**: [Duplicati] (Automated backups), [Watchtower] (Automated container updates), [Uptime Kuma] (Service monitoring and alerting)

## Security Notice
> **🔒 Secrets Management:** To adhere to best practices for public repositories, all sensitive data (passwords, JWT secrets, API tokens, and local IP addresses) have been systematically redacted (`<REDACTED>`) from these Compose files.
