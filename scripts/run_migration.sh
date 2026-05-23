#!/usr/bin/env bash
# Drive the one-shot MySQL → pgvector migration end-to-end.
#
# Inputs:  ./dump.sql  (mysqldump from prod)
# Outputs: ./pgdump.sql (postgres-format dump, restore into the prod `db`
#                       service with `psql < pgdump.sql`)
#
# Side effects: brings up `docker-compose.migration.yml` (ephemeral mysql +
# postgres), runs pgloader inside the same docker network, runs pg_dump,
# then tears the stack down (`down -v` — volumes are wiped).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILE=docker-compose.migration.yml
OUTPUT=pgdump.sql

if [[ ! -f dump.sql ]]; then
  echo "missing ./dump.sql — take a mysqldump from prod first:" >&2
  echo "  mysqldump --single-transaction --routines --triggers \\" >&2
  echo "    -u indexer_utils -p indexer_utils > dump.sql" >&2
  exit 1
fi

cleanup() {
  echo "==> tearing down migration stack"
  docker compose -f "$COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> bringing up ephemeral mysql + pg (dump auto-loads via initdb.d)"
docker compose -f "$COMPOSE_FILE" up -d --wait

NETWORK="$(docker compose -f "$COMPOSE_FILE" ps --format '{{.Name}}' | head -1 \
  | xargs -I{} docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' {})"
MYSQL_CT="$(docker compose -f "$COMPOSE_FILE" ps -q mysql)"
PG_CT="$(docker compose -f "$COMPOSE_FILE" ps -q pg)"
MYSQL_NAME="$(docker inspect -f '{{.Name}}' "$MYSQL_CT" | sed 's|^/||')"
PG_NAME="$(docker inspect -f '{{.Name}}' "$PG_CT" | sed 's|^/||')"

echo "==> running pgloader $MYSQL_NAME -> $PG_NAME"
# --platform linux/amd64: pgloader image is amd64-only; runs via qemu on
# Apple Silicon. Still finishes in seconds at this row count.
docker run --rm --platform linux/amd64 --network "$NETWORK" \
  ghcr.io/dimitri/pgloader:latest \
  pgloader \
    --with "include drop" \
    --with "preserve index names" \
    "mysql://root:passw0rd@${MYSQL_NAME}/indexer_utils" \
    "postgresql://indexer_utils:passw0rd@${PG_NAME}/indexer_utils"

echo "==> pg_dump -> $OUTPUT"
docker exec "$PG_CT" pg_dump \
  -U indexer_utils \
  --clean --if-exists \
  --no-owner --no-privileges \
  indexer_utils > "$OUTPUT"

echo "==> wrote $OUTPUT ($(wc -c < "$OUTPUT" | tr -d ' ') bytes)"
echo
echo "next:"
echo "  docker compose up -d db"
echo "  psql -h 127.0.0.1 -U indexer_utils indexer_utils < $OUTPUT"
echo "  DB_HOST=127.0.0.1 alembic upgrade head"
echo "  DB_HOST=127.0.0.1 OPENAI_API_KEY=... \\"
echo "    python backfill_synopsis_vectors.py --type mv --added-only --skip-rules"
echo "  DB_HOST=127.0.0.1 OPENAI_API_KEY=... \\"
echo "    python backfill_synopsis_vectors.py --type tv --added-only --skip-rules"
