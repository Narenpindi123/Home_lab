# 🚀 Self-Hosted Homelab Infrastructure

Welcome to my personal homelab repository! This project serves as a living portfolio of my skills in **Systems Administration, DevOps, and Infrastructure as Code (IaC)**. 

What started as a personal project to regain control over my data has evolved into a fully automated, containerized infrastructure environment that securely hosts multiple production-grade services on my local hardware.

## 🛠️ Core Technologies
- **Containerization Engine:** Docker & Docker Compose
- **Host OS:** Linux (Ubuntu/Debian)
- **Networking & Access:** Tailscale (WireGuard-based Mesh VPN)
- **DNS & Ad-Blocking:** AdGuard Home
- **Reverse Proxy / Routing:** Tailscale MagicDNS
- **Automation:** Python & Bash scripts
- **Version Control:** Git

---

## 🏗️ Architecture & Deployment Strategy

The environment is designed with modularity, security, and ease of deployment in mind. Services are logically grouped into functional stacks using `docker-compose.yml` files. This IaC approach ensures that any service—or the entire server—can be torn down and reliably spun back up in minutes.

### 🌐 Networking & Security
A major focus of this architecture is secure, remote access without exposing services to the public internet:
- **Tailscale Overlay Network:** Instead of opening ports on a traditional firewall, all devices and services communicate via a secure, encrypted Tailscale mesh network. This provides Zero-Trust remote access from anywhere in the world.
- **AdGuard Home:** Deployed as the primary DNS server on the network, it actively filters out tracking domains and malicious advertisements at the network level, improving privacy and reducing bandwidth usage across all connected devices.

### 📊 Infrastructure Management & Observability
To ensure 99.9% uptime and prevent data loss, I implemented automated management toolsets:
- **Duplicati:** Handles automated, encrypted, and compressed backups of all critical application data and databases. Backups are scheduled and run independently of the services.
- **Watchtower:** Automates the lifecycle management of Docker containers by monitoring upstream Docker Hub registries and automatically pulling/restarting containers when base images are updated.
- **Uptime Kuma:** Provides proactive, real-time monitoring of all internal services, with instant alerts configured for any downtime anomalies.

---

## 💻 Deployed Services Overview

### Productivity & Personal Organization
- **[Vikunja](docker/vikunja/):** A robust, self-hosted task management application backed by a PostgreSQL database. It handles project organization, kanban boards, and task delegation.
- **[Matter](docker/matter/):** A highly aesthetic read-it-later application used to consolidate articles, newsletters, and RSS feeds into a single distraction-free reading queue.

### Media & Content Delivery
- **[Navidrome](docker/navidrome/):** A lightweight, fast, and fully-featured self-hosted music streaming server to serve FLAC and high-fidelity audio to remote clients.
- **[Komga](docker/komga/):** A dedicated media server tailored for comics, mangas, and magazines, acting as a personal digital library with cross-device sync.
- **[Romm](docker/romm/):** A modern retro game library manager designed to catalog and organize ROM files with rich metadata scraping.

---

## 🔒 Security Notice & Secrets Management
> **Note to Reviewers:** To adhere to DevOps best practices for public repositories, all sensitive data has been programmatically redacted from these configuration files using a custom Python deployment script prior to pushing to GitHub.
> 
> Variables such as `POSTGRES_PASSWORD`, `JWT_SECRET`, local IP mappings, and API Authentication Tokens have been replaced with `<REDACTED>`. Therefore, these configurations act as architectural blueprints and cannot be run locally without injecting the corresponding `.env` secrets.
