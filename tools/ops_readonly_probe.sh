#!/usr/bin/env bash
set -euo pipefail

readonly N8N_CONTAINER="n8n"
readonly DB_PATH="/home/node/.n8n/database.sqlite"
readonly TARGET_DATE="2026-09-15"

printf '== Companion-Mind read-only VPS probe ==\n'
printf 'utc_now=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'host=%s\n' "$(hostname)"

printf '\n[containers]\n'
docker ps --format '{{.Names}}\t{{.Status}}'

printf '\n[n8n_version]\n'
docker exec "$N8N_CONTAINER" n8n --version

printf '\n[sqlite_file]\n'
docker exec "$N8N_CONTAINER" ls -lh "$DB_PATH"

printf '\n[execution_entity_%s]\n' "$TARGET_DATE"
docker exec "$N8N_CONTAINER" node -e '
const { DatabaseSync } = require("node:sqlite");
const db = new DatabaseSync("/home/node/.n8n/database.sqlite", { readOnly: true });
const rows = db.prepare("select id,workflowId,mode,status,createdAt,startedAt,stoppedAt from execution_entity where createdAt like ? order by createdAt").all("2026-09-15%");
console.log(JSON.stringify(rows, null, 2));
'

printf '\n== END READ-ONLY PROBE ==\n'
