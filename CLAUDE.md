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
  - `vector_search.py` — pgvector embedding + synopsis-similarity queries (768-dim, `embeddinggemma-cpu`)
  - `taste_signal.py` — builds the `taste_signal` payload block (neighbour×critic cohort cross-tab + per-attribute add-rates + whole-library cast cross-reference), the cohort cross-tab Redis-cached by era
- `src/` — React/TypeScript frontend (Relay, MUI)
- `alembic/` — DB migrations
- `tests/` — Python unit tests (run against a real pgvector Postgres, see below)
- `e2e/` — Playwright end-to-end tests
- `scripts/postgres-init/` — `create-test-db.sql`, mounted as Postgres `initdb.d` by the compose files to create the test DB

## MCP Server (claude.ai connector)

`indexer_utils/mcp_server.py` exposes a native **MCP** endpoint mounted at `/mcp` in `main.py` (via `mcp.http_app(path="/")` + `combine_lifespans` so FastMCP's session-manager lifespan runs alongside the scheduler's). It's a curated tool surface (read: open/decided candidates, scheduled jobs, `recommend`; write: `add_item`, `ignore_item`, `retry_ai`, `set_recommendation_preference`, `recheck_visible`) wrapping the same helpers the GraphQL resolvers use.

- **Auth**: Authelia acts as the OIDC provider. The connector gets a JWT access token from Authelia and presents it as a bearer token; `JWTVerifier` validates it against Authelia's JWKS (issuer + audience). Config via `MCP_OIDC_ISSUER`, `MCP_OIDC_JWKS_URI`, `MCP_RESOURCE_URL`, `MCP_BASE_URL`; `MCP_AUTH_DISABLED=true` bypasses auth for local/dev.
- **Discovery**: we serve RFC 9728 protected-resource metadata ourselves from `main.py` (`/.well-known/oauth-protected-resource[/mcp]`) because FastMCP mis-derives `resource`/path under an ASGI sub-mount (upstream issue #1348). `PROTECTED_RESOURCE_METADATA.resource` MUST equal the `JWTVerifier` audience **and** the Authelia client's audience, or tokens validate to the wrong `aud` and silently fail.
- **nginx**: `/mcp` and `/.well-known/oauth-protected-resource` must be proxied to the app **without** Authelia forward-auth (`auth_request`) — the JWT is the gate, not the session cookie. These are server-side, gitignored configs.

## Database

Postgres 16 with the **pgvector** extension (`pgvector/pgvector:pg16` image). The app talks to it through **async SQLAlchemy 2.0** over **psycopg 3** (`postgresql+psycopg://…`).

- `indexer_utils/session.py` — `db_session()` returns an `AsyncSession`; always `async with db_session() as session:`. The engine/sessionmaker are cached module-level singletons. Model classmethods (`IgnoreItem.create`, `.filter`, `MovieRecommendationRecord.recent_history`, …) are all `async`.
- `indexer_utils/models.py` — `IgnoreItem` (the catalog row; `attributes` is a Postgres `JSONB` blob, `synopsis_vector` is a **deferred** `Vector(768)` column), `MovieRecommendationRecord` (recommendation history + LIKE/NOT_NOW/NEVER feedback), `FilterRule`.
- **Test DB isolation**: `session.py` checks `sys.argv` for `pytest` and swaps `DB_NAME`→`TEST_DB_NAME`, so the same `.env` serves both the app and the suite without ever pointing tests at the real DB. Don't pass DB env vars on the command line.
- **Don't read/grep `.env`** to discover DB config — reads of it are permission-denied, and you don't need it: `decouple`/`session.py` already load it. Any script using `db_session()` connects to the real DB automatically (and the running container is `mediamanager-db-1`).
- The MySQL + Weaviate → Postgres + pgvector swap is **done** (commit `253e198`), and the one-shot migration tooling has been removed. Don't reintroduce either dependency.

## Recommendation Pipeline

Entry point: `annotate_with_ai_async(item_type, uid, title, attrs)` in `indexer_utils/ai_recs.py`, called during candidate ingest (`vid_utils.py`) and on GraphQL re-annotation (`schema.py`). Per candidate it:

1. Hydrates metadata (TMDB cast/director/release-count via `tmdb.py`).
2. Researches a short synopsis with the synopsis agent (`ai_tools/synopsis.py`: web tools, fed TMDB's overview/franchise/networks via `tmdb.get_title_details`, told never to invent a plot) and embeds `title + synopsis` into the pgvector `synopsis_vector` column (`vector_search.upsert_item_vector`). The title is TMDB's (`tmdb_title`), never a release filename. For brand-new candidates the row doesn't exist yet, so the vector is stashed in `attrs["_synopsis_vector_tmp"]` and attached after insert.
3. Builds a user payload including a pre-computed `library_profile` (aggregate taste, see `library_profile.py`) and a `taste_signal` block (`taste_signal.py`): raw historical add counts over the candidate's decided ±2yr same-type cohort, broken out by the synopsis-neighbour × critic-presence cross-tab and per-attribute (network/language/genre), plus a `cast_xref` counting how many added titles each of the candidate's cast appears in (whole-library, cross-era — not bounded to the cohort window). The model reads counts as rates itself; the cohort cross-tab is Redis-cached by `(item_type, year)`.
4. Runs the recommendation **Agent** and writes a single consolidated `ai` block back onto `attrs` (verdict, score, reason, synopsis, tool log, turn/tool-call counts, failure info).

The agent itself lives in `indexer_utils/ai_tools/` and is built on the **openai-agents SDK** (`openai-agents` package):

- `agent.py` — `build_agent()` wires per-item-type tools and a Pydantic `Recommendation` (`recommend: bool`, `score: 0–1`, `reason`) as the structured `output_type`. `run_recommendation()` drives `Runner.run` with tracing disabled and a per-run `AsyncOpenAI` client (closed on exit to avoid leaking sockets across `asyncio.run` loops). Model failures (turn cap, tool-budget cap, transport error) are captured as `result.failure`, not raised.
- **Tools** (all `@safe_tool`-wrapped so a tool exception comes back to the model as an error payload instead of killing the run):
  - `searches.py` — `search_similar_by_synopsis` (pgvector cosine distance), `search_by_genre`, `search_by_network`. All query *added* library items only; rating filters are per-source (`imdb_min`, `rt_min`, …).
  - `inspections.py` — `get_item_details`, `get_user_history`, `check_added_history` (fan out to DB / Plex / Radarr / Sonarr).
  - `discoveries.py` — `search_recent_releases` (movies only), `search_recent_tv` (TV only), `search_title_buzz`. These are nested subagents that research with the local `brave_search` + `web_fetch` tools from `webtools.py` (static fetch by default; `render=true` runs a headless chromium) instead of OpenAI's hosted WebSearchTool; they return prose dossiers (no JSON schema — the consumer is another LLM) and cache results in Redis.
  - `cast_history.py` — `search_cast_history`, a tool-bearing research subagent. The people-set is the candidate's top-10 cast (billing order) plus director; a deterministic-SQL catalog cross-reference (cast and/or director, leave-one-out, type-scoped) is embedded as a *seed only*. The subagent then establishes each person's actual career (own knowledge + `brave_search`/`web_fetch` — incl. a `web_search` alias, since qwen insists on that name) and verifies a sample of it against the user's Plex with `check_titles`, which delegates matching to Plex's own `/hubs/search` API (via `plex_utils.hub_search`) and only checks title + year ±1 on what Plex returns — no local matching, no watch history. The dossier reports career-relative rates ("X of Y works are in your library") and a per-person pattern label. Cached 6h at `mediamanager:cast_history:v5:{type}:{uid}`; a failed subagent returns `error` (no signal), never a negative.
- `research.py` — `ResearchSpec` + `run_research`, the one runner behind every research subagent (synopsis, buzz, cast history, release windows): per-run client, `TurnBudget`, `AuditHooks` tool log, errors returned not raised.
- `hooks.py` — `AuditHooks` records per-call timing/outcome/arguments and enforces a cumulative tool-call budget (the SDK only caps turns).
- `base.py` — `ToolContext` (item_type + candidate) passed to every tool via `RunContextWrapper`.

Relevant env (via `python-decouple`/`.env`): `OPENAI_BASE_URL` (local OpenAI-compatible chat server), `OPENAI_API_KEY` (fake — the servers are unauthenticated), `OPENAI_MODEL` (default `qwen3.8`; every agent, including the research subagents, runs on it), `OPENAI_EMBEDDING_MODEL` (default `embeddinggemma-cpu:latest`, 768-dim L2-normalized), `OPENAI_EMBEDDING_BASE_URL` (where embeddings are served; defaults to `OPENAI_BASE_URL`), `OPENAI_CONTEXT_TOKENS` (the chat model's window, for the turn budget), `AI_AGENT_MAX_TURNS` (6), `AI_AGENT_MAX_TOOL_CALLS` (16), `BRAVE_API_KEY` (Brave Search API for the discovery subagents). A gateway that serves both chat and `/v1/embeddings` needs only `OPENAI_BASE_URL`; a chat-only server (e.g. Strata) needs `OPENAI_EMBEDDING_BASE_URL` pointed at one that embeds.

### Research harness

`research_harness.py` runs any research subagent live — `synopsis`, `buzz`, `cast`, `recent-releases`, `recent-tv` — through the same prompt builders and real inputs production uses (catalog row hydrated from TMDB; nothing written to the DB), skipping only the Redis caches. `--cases` runs a curated set of real candidates (new releases, sequels, remakes, non-English TV); `--dry-run` prints the input only; records (input, output, tool log with arguments) land in `research_runs/` (gitignored). Run it on the app's network:

```bash
docker run --rm --network container:servermonitor-servermonitor-1 \
    -v "$PWD":/opt/servermonitor servermonitor-servermonitor \
    python research_harness.py synopsis --cases
```

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
- **alembic**: `alembic upgrade head` to apply, `alembic revision --autogenerate -m "…"` to create. pgvector bits are hand-written, not autogenerated — `add_pgvector_synopsis.py` `op.execute`s `CREATE EXTENSION vector` and the HNSW cosine index, and `resize_pgvector_synopsis.py` carries the 1536→768 column migration (old data kept in `synopsis_vector_1536` until `drop_pgvector_synopsis_1536.py`). Mirror that pattern for vector changes.
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
