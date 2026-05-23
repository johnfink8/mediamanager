# One-time MySQL → postgres data move

Recipe used once per environment (dev, then prod) to move existing data
off MySQL onto the postgres + pgvector stack. Designed around a
``mysqldump`` of the old DB — no live MySQL endpoint required, since the
old service is already gone from ``docker-compose.yml``.

## TL;DR

1. Take a ``mysqldump`` from prod → ``./dump.sql``.
2. ``bash scripts/run_migration.sh`` → ``./pg_dump.sql``.
3. On the target host: ``docker compose up -d db``, restore ``pg_dump.sql``
   into it, ``alembic upgrade head``, run the backfill.

Steps 1–2 happen wherever ``docker`` and the dump live (your laptop is
fine — the migration stack is fully ephemeral). Only ``pg_dump.sql``
needs to reach the target.

## Step 0 — dump prod

```sh
mysqldump --single-transaction --routines --triggers \
  -u indexer_utils -p indexer_utils > dump.sql
```

Stop writes (gunicorn, scheduler, backfills) before dumping so the
snapshot is consistent.

## Step 1 — convert dump.sql → pg_dump.sql

``docker-compose.migration.yml`` plus ``scripts/run_migration.sh`` boot
an ephemeral mysql:8.0 (loaded from ``dump.sql`` via the entrypoint),
an ephemeral pgvector postgres, run pgloader between them, ``pg_dump``
the result, and tear everything down.

```sh
bash scripts/run_migration.sh
# … wrote pg_dump.sql (~27MB)
```

Why mysql:**8.0** and not 9.x (prod): pgloader's MySQL auth client only
speaks the legacy ``mysql_native_password`` plugin, which 9.x removed
outright. The dump itself is plain DDL + ``INSERT`` and restores cleanly
across the version gap. The ``alembic_version`` row carries over.

## Step 2 — restore on the target

```sh
docker compose up -d db
docker exec -i <db-container-name> psql -U indexer_utils indexer_utils < pg_dump.sql
```

Spot-check row counts:

```sh
docker exec <db-container-name> psql -U indexer_utils indexer_utils -c \
  "SELECT count(*) FROM indexer_utils_ignoreitem; SELECT version_num FROM alembic_version;"
```

The version_num should be ``add_ingest_and_titles_jobs`` — the prior head.

## Step 3 — apply the pgvector migration

```sh
DB_HOST=127.0.0.1 alembic upgrade head
```

Adds the ``vector`` extension, the ``synopsis_vector`` column, and the
HNSW cosine index. ``version_num`` advances to ``add_pgvector_synopsis``.

## Step 4 — backfill embeddings

``search_similar_by_synopsis`` filters to ``added=True`` post-fetch, so
vectors on rejected rows are dead weight at query time. ``--added-only``
targets exactly what the tool surfaces and skips the chat-completion
cost of synthesising new synopses.

```sh
DB_HOST=127.0.0.1 OPENAI_API_KEY=... \
  python backfill_synopsis_vectors.py --type mv --added-only --skip-rules
DB_HOST=127.0.0.1 OPENAI_API_KEY=... \
  python backfill_synopsis_vectors.py --type tv --added-only --skip-rules
```

Cost: one ``text-embedding-3-small`` call per added-library item —
pennies for a typical library.

## Step 5 — smoke test

```sh
docker exec <db-container-name> psql -U indexer_utils indexer_utils -c \
  "SELECT item_type,
          count(*) FILTER (WHERE synopsis_vector IS NOT NULL) AS embedded,
          count(*) FILTER (WHERE added) AS added,
          count(*) AS total
   FROM indexer_utils_ignoreitem GROUP BY item_type;"
```

Bring up the app and trigger a ``retry_ai`` mutation; the agent log
should show a ``search_similar_by_synopsis`` tool call returning real
neighbors.

## Rolling back

The pgvector migration's ``downgrade()`` drops the column and index but
not the extension. There is no automatic path back to MySQL — the old
service is gone from compose. Keep ``dump.sql`` until you've used the
new stack for a few days.
