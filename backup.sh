#!/bin/bash
# Quiet backup — copies critical data to file server
# Runs from cron every hour. Simple cp, not a framework.
#
# Critical files:
#   sessions/*.jsonl  — conversation history (THE thing we're protecting)
#   data/memory.db    — embedded memory store
#   archives/         — trimmed conversation fragments
#   ~/self/identity.md — identity document
#   config/           — discord config, quiet config (not templates)

set -euo pipefail

QUIET_DIR="${HOME}/quiet"
BACKUP_DIR="/mnt/file_server/Nyx/backups/quiet"
TIMESTAMP=$(date +%Y%m%d_%H%M)

# Ensure backup directory exists
mkdir -p "${BACKUP_DIR}/sessions"
mkdir -p "${BACKUP_DIR}/archives"
mkdir -p "${BACKUP_DIR}/config"
mkdir -p "${BACKUP_DIR}/data"
mkdir -p "${BACKUP_DIR}/identity"

# Copy sessions (the critical ones)
cp -u "${QUIET_DIR}"/sessions/*.jsonl "${BACKUP_DIR}/sessions/" 2>/dev/null || true

# Copy memory database
cp -u "${QUIET_DIR}/data/memory.db" "${BACKUP_DIR}/data/" 2>/dev/null || true

# Copy archives
cp -u "${QUIET_DIR}"/archives/*.jsonl "${BACKUP_DIR}/archives/" 2>/dev/null || true

# Copy identity
cp -u "${HOME}/self/identity.md" "${BACKUP_DIR}/identity/" 2>/dev/null || true

# Copy config (not templates)
cp -u "${QUIET_DIR}/config/discord_config.json" "${BACKUP_DIR}/config/" 2>/dev/null || true
cp -u "${QUIET_DIR}/config/quiet_config.txt" "${BACKUP_DIR}/config/" 2>/dev/null || true

# Keep a timestamped snapshot of the active session (daily, not hourly)
DAILY_DIR="${BACKUP_DIR}/daily"
mkdir -p "${DAILY_DIR}"
TODAY=$(date +%Y%m%d)
ACTIVE_SESSION=$(ls -t "${QUIET_DIR}"/sessions/*.jsonl 2>/dev/null | head -1)
if [ -n "${ACTIVE_SESSION}" ] && [ ! -f "${DAILY_DIR}/$(basename "${ACTIVE_SESSION}" .jsonl)-${TODAY}.jsonl" ]; then
    cp "${ACTIVE_SESSION}" "${DAILY_DIR}/$(basename "${ACTIVE_SESSION}" .jsonl)-${TODAY}.jsonl"
fi

# Prune daily snapshots older than 30 days
find "${DAILY_DIR}" -name "*.jsonl" -mtime +30 -delete 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M')] Backup complete"
