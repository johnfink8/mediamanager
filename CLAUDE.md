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

Full-stack media manager app: **FastAPI + Strawberry GraphQL** backend (Python), **React + Relay + TypeScript** frontend. The backend manages Plex/Radarr/Sonarr integrations, AI-powered recommendations, and Weaviate vector search.

## Architecture

- `indexer_utils/` — Python backend (FastAPI app, GraphQL schema, models, integrations)
- `src/` — React/TypeScript frontend (Relay, MUI)
- `alembic/` — DB migrations
- `tests/` — Python unit tests
- `e2e/` — Playwright end-to-end tests

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

**PR descriptions**: a little more room, but still restrained. Describe the _problem_ being solved and _why_ — let the code itself answer the "how". Skip the file-by-file walkthrough and the bulleted list of every change. A reviewer reading the diff shouldn't also need a prose narration of it.

## Common Pitfalls

- **Relay-generated files** in `src/__generated__/` are auto-generated — never edit. Re-run `npx relay-compiler` after changing GraphQL queries/mutations or the Strawberry schema in `indexer_utils/schema.py`.
- **alembic**: `alembic upgrade head` to apply, `alembic revision --autogenerate -m "…"` to create.
- **Missing tool**: if a Python tool isn't found, the activate hook didn't fire — check `./venv/bin/` directly (most likely a worktree missing the symlink).

## Dev Server

```bash
bash dev_server.sh   # backend (uvicorn, DEBUG=true, port 8000)
npm run dev          # frontend (relay-compiler + webpack --watch)
```
