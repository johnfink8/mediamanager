# mediamanager — Claude Code Guide

## ⚠️ Session Start Checklist

Before doing any work, always run this to orient yourself:

```bash
pwd && which ruff >/dev/null 2>&1 && echo "venv OK" || echo "venv MISSING — see Worktree Setup below"
```

The Claude Code shell sources `venv/bin/activate` on startup, so `python`, `ruff`, `mypy`, `pytest`, `uvicorn`, `alembic`, etc. are on PATH directly — no `./venv/bin/` prefix needed.

**If running inside a git worktree** (i.e. `pwd` shows a path like `.git/worktrees/...` or a sibling directory, and `venv/` is missing), symlink the shared artifacts from the main project rather than reinstalling:

```bash
# Find the main project root (adjust path if needed)
MAIN=$(git worktree list | head -1 | awk '{print $1}')

ln -s "$MAIN/venv" ./venv
ln -s "$MAIN/node_modules" ./node_modules
```

After symlinking, verify: `ruff --version && npx relay-compiler --version` (you may need to start a new shell so the activate hook re-runs).

## Project Overview

Full-stack media manager app: **FastAPI + Strawberry GraphQL** backend (Python), **React + Relay + TypeScript** frontend. The backend manages Plex/Radarr/Sonarr integrations and an agentic, AI-powered recommendation pipeline backed by **Postgres + pgvector** for semantic search.

## Architecture

- `indexer_utils/` — Python backend (FastAPI app, GraphQL schema, async SQLAlchemy models, integrations)
  - `ai_recs.py` — orchestrates the per-candidate recommendation flow
  - `ai_tools/` — the openai-agents-SDK recommendation Agent and its tools
  - `prompts/` — system prompts for the recommendation agent + discovery subagents (`.md`)
  - `vector_search.py` — pgvector embedding + synopsis-similarity queries
  - `taste_signal.py` — builds the `taste_signal` payload block (neighbour×critic cohort cross-tab + per-attribute add-rates + whole-library cast cross-reference), the cohort cross-tab Redis-cached by era
- `src/` — React/TypeScript frontend (Relay, MUI)
- `alembic/` — DB migrations
- `tests/` — Python unit tests (run against a real pgvector Postgres, see below)
- `e2e/` — Playwright end-to-end tests
- `scripts/postgres-init/` — `create-test-db.sql`, mounted as Postgres `initdb.d` by the compose files to create the test DB
- `backfill_synopsis_vectors.py` (repo root) — (re)embed `synopsis_vector` across the catalog; `--reindex-all`, `--check-vectors`, `--added-only`, etc.

## MCP Server (claude.ai connector)

`indexer_utils/mcp_server.py` exposes a native **MCP** endpoint mounted at `/mcp` in `main.py` (via `mcp.http_app(path="/")` + `combine_lifespans` so FastMCP's session-manager lifespan runs alongside the scheduler's). It's a curated tool surface (read: open/decided candidates, scheduled jobs, `recommend`; write: `add_item`, `ignore_item`, `retry_ai`, `set_recommendation_preference`, `recheck_visible`) wrapping the same helpers the GraphQL resolvers use.

- **Auth**: Authelia acts as the OIDC provider. The connector gets a JWT access token from Authelia and presents it as a bearer token; `JWTVerifier` validates it against Authelia's JWKS (issuer + audience). Config via `MCP_OIDC_ISSUER`, `MCP_OIDC_JWKS_URI`, `MCP_RESOURCE_URL`, `MCP_BASE_URL`; `MCP_AUTH_DISABLED=true` bypasses auth for local/dev.
- **Discovery**: we serve RFC 9728 protected-resource metadata ourselves from `main.py` (`/.well-known/oauth-protected-resource[/mcp]`) because FastMCP mis-derives `resource`/path under an ASGI sub-mount (upstream issue #1348). `PROTECTED_RESOURCE_METADATA.resource` MUST equal the `JWTVerifier` audience **and** the Authelia client's audience, or tokens validate to the wrong `aud` and silently fail.
- **nginx**: `/mcp` and `/.well-known/oauth-protected-resource` must be proxied to the app **without** Authelia forward-auth (`auth_request`) — the JWT is the gate, not the session cookie. These are server-side, gitignored configs.

## Database

Postgres 16 with the **pgvector** extension (`pgvector/pgvector:pg16` image). The app talks to it through **async SQLAlchemy 2.0** over **psycopg 3** (`postgresql+psycopg://…`).

- `indexer_utils/session.py` — `db_session()` returns an `AsyncSession`; always `async with db_session() as session:`. The engine/sessionmaker are cached module-level singletons. Model classmethods (`IgnoreItem.create`, `.filter`, `MovieRecommendationRecord.recent_history`, …) are all `async`.
- `indexer_utils/models.py` — `IgnoreItem` (the catalog row; `attributes` is a Postgres `JSONB` blob, `synopsis_vector` is a **deferred** `Vector(1536)` column), `MovieRecommendationRecord` (recommendation history + LIKE/NOT_NOW/NEVER feedback), `FilterRule`.
- **Test DB isolation**: `session.py` checks `sys.argv` for `pytest` and swaps `DB_NAME`→`TEST_DB_NAME`, so the same `.env` serves both the app and the suite without ever pointing tests at the real DB. Don't pass DB env vars on the command line.
- **Don't read/grep `.env`** to discover DB config — reads of it are permission-denied, and you don't need it: `decouple`/`session.py` already load it. Any script using `db_session()` connects to the real DB automatically (and the running container is `mediamanager-db-1`).
- The MySQL + Weaviate → Postgres + pgvector swap is **done** (commit `253e198`), and the one-shot migration tooling has been removed. Don't reintroduce either dependency.

## Recommendation Pipeline

Entry point: `annotate_with_ai_async(item_type, uid, title, attrs)` in `indexer_utils/ai_recs.py`, called during candidate ingest (`vid_utils.py`) and on GraphQL re-annotation (`schema.py`). Per candidate it:

1. Hydrates metadata (TMDB cast/director/release-count via `tmdb.py`).
2. Generates a short synopsis (plain OpenAI JSON call) and embeds `title + synopsis` into the pgvector `synopsis_vector` column (`vector_search.upsert_item_vector`). For brand-new candidates the row doesn't exist yet, so the vector is stashed in `attrs["_synopsis_vector_tmp"]` and attached after insert.
3. Builds a user payload including a pre-computed `library_profile` (aggregate taste, see `library_profile.py`) and a `taste_signal` block (`taste_signal.py`): raw historical add counts over the candidate's decided ±2yr same-type cohort, broken out by the synopsis-neighbour × critic-presence cross-tab and per-attribute (network/language/genre), plus a `cast_xref` counting how many added titles each of the candidate's cast appears in (whole-library, cross-era — not bounded to the cohort window). The model reads counts as rates itself; the cohort cross-tab is Redis-cached by `(item_type, year)`.
4. Runs the recommendation **Agent** and writes a single consolidated `ai` block back onto `attrs` (verdict, score, reason, synopsis, tool log, turn/tool-call counts, failure info).

The agent itself lives in `indexer_utils/ai_tools/` and is built on the **openai-agents SDK** (`openai-agents` package):

- `agent.py` — `build_agent()` wires per-item-type tools and a Pydantic `Recommendation` (`recommend: bool`, `score: 0–1`, `reason`) as the structured `output_type`. `run_recommendation()` drives `Runner.run` with tracing disabled and a per-run `AsyncOpenAI` client (closed on exit to avoid leaking sockets across `asyncio.run` loops). Model failures (turn cap, tool-budget cap, transport error) are captured as `result.failure`, not raised.
- **Tools** (all `@safe_tool`-wrapped so a tool exception comes back to the model as an error payload instead of killing the run):
  - `searches.py` — `search_similar_by_synopsis` (pgvector cosine distance), `search_by_genre`, `search_by_network`. All query *added* library items only; rating filters are per-source (`imdb_min`, `rt_min`, …).
  - `inspections.py` — `get_item_details`, `get_user_history`, `check_added_history` (fan out to DB / Plex / Radarr / Sonarr).
  - `discoveries.py` — `search_recent_releases` (movies only), `search_recent_tv` (TV only), `search_title_buzz`. These are **nested subagents** with the hosted `WebSearchTool`; they return prose dossiers (no JSON schema — the consumer is another LLM) and cache results in Redis.
- `hooks.py` — `AuditHooks` records per-call timing/outcome and enforces a cumulative tool-call budget (the SDK only caps turns).
- `base.py` — `ToolContext` (item_type + candidate) passed to every tool via `RunContextWrapper`.

Relevant env (via `python-decouple`/`.env`): `OPENAI_API_KEY`, `OPENAI_MODEL` (default `gpt-5.5`), `OPENAI_EMBEDDING_MODEL` (default `text-embedding-3-small`), `AI_AGENT_MAX_TURNS` (6), `AI_AGENT_MAX_TOOL_CALLS` (16). Discovery subagents are pinned to `gpt-5.4-mini` in `discoveries.py`.

## Code Style & Tooling

### Python

- **Formatter/linter**: `ruff` (config in `pyproject.toml`) — replaces black + isort + flake8. Line length 88, Python 3.9 target.
- **Type checker**: `mypy` in strict mode (`mypy.ini`). Annotate all new functions; use `Optional[X]` / `X | None` for nullables.
- Auto-fix: `ruff check --fix .`

### TypeScript / React

- **Formatter**: `prettier` (v2). **Linter**: `eslint`. **Type checker**: `tsc --noEmit`.
- `relay-compiler` must run **before** `tsc`/`eslint` because it generates types in `src/__generated__/`. The `npm run lint` script handles this ordering.

## Before Committing

```bash
ruff format . && ruff check .
npm run lint        # relay-compiler + tsc + prettier + eslint
```

## Commits & PRs

**Commit messages**: short and concise — 12 words max. State the central point of the change in one line. No bullet lists, no feature breakdowns, no "and also" addenda.

**PR descriptions**: a little more room, but still restrained. Describe the _problem_ being solved and _why_ — let the code itself answer the "how". Skip the file-by-file walkthrough and the bulleted list of every change. Two or three sentences. A reviewer reading the diff shouldn't also need a prose narration of it.

**🚫 NO Claude attribution. Ever.** Do not append `Co-Authored-By: Claude …`, `🤖 Generated with [Claude Code]`, or any variant of those trailers/footers to commit messages or PR descriptions. This applies even if the default Claude Code commit/PR templates suggest them — strip them out before running `git commit` or `gh pr create`. The commit body ends at the last real line of the message; the PR body ends at the end of the human-written description. No exceptions.

## Common Pitfalls

- **Relay-generated files** in `src/__generated__/` are auto-generated — never edit. Re-run `npx relay-compiler` after changing GraphQL queries/mutations or the Strawberry schema in `indexer_utils/schema.py`.
- **alembic**: `alembic upgrade head` to apply, `alembic revision --autogenerate -m "…"` to create. pgvector bits are hand-written, not autogenerated — `add_pgvector_synopsis.py` `op.execute`s `CREATE EXTENSION vector` and the HNSW cosine index, and imports `Vector` from `pgvector.sqlalchemy`. Mirror that pattern for vector changes.
- **Async DB**: the whole DB layer is async — `db_session()` yields an `AsyncSession` and must be used with `async with`/`await`. Don't reintroduce sync `Session` calls.
- **Missing tool**: if a Python tool isn't found, the activate hook didn't fire — check `./venv/bin/` directly (most likely a worktree missing the symlink).

## Dev Server

```bash
bash dev_server.sh   # backend (uvicorn, DEBUG=true, port 8000)
npm run dev          # frontend (relay-compiler + webpack --watch)
```

## Testing

Unit tests run **against a real pgvector Postgres** (the cosine-distance / JSONB SQL paths need it), in Docker:

```bash
docker compose -f docker-compose.test.yml run --build --rm pytest
```

Run this locally before committing test changes rather than push-and-watch-CI. `pytest-asyncio` is in `asyncio_mode=auto` (see `pyproject.toml`). The same compose file also wires the Playwright e2e stack (`db_init` → `app` → `seeder` → `playwright`).
