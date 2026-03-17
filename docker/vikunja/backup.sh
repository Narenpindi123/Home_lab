#!/bin/bash
# Vikunja PostgreSQL daily backup script
# Keeps last 7 daily backups with rotation

BACKUP_DIR="/home/naren/homelab/docker/vikunja/backups"
CONTAINER="vikunja_db"
DB_USER="vikunja"
DB_NAME="vikunja"
DATE=$(date +%Y-%m-%d_%H%M)
MAX_BACKUPS=7

# Create backup directory if not exists
mkdir -p "$BACKUP_DIR"

# Dump the database
docker exec "$CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$BACKUP_DIR/vikunja_$DATE.sql.gz"

if [ $? -eq 0 ]; then
    echo "[$(date)] ✅ Vikunja backup created: vikunja_$DATE.sql.gz ($(du -h "$BACKUP_DIR/vikunja_$DATE.sql.gz" | cut -f1))"
else
    echo "[$(date)] ❌ Vikunja backup FAILED"
    exit 1
fi

# Rotate: keep only the last N backups
cd "$BACKUP_DIR" && ls -t vikunja_*.sql.gz | tail -n +$((MAX_BACKUPS + 1)) | xargs -r rm --
echo "[$(date)] 🗂️ Backups in rotation: $(ls vikunja_*.sql.gz 2>/dev/null | wc -l)/$MAX_BACKUPS"
