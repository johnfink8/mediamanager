# mediamanager — Claude Code Guide

## Project Overview

Full-stack media manager app: **FastAPI + Strawberry GraphQL** backend (Python), **React + Relay + TypeScript** frontend. The backend manages Plex/Radarr/Sonarr integrations, AI-powered recommendations, and Weaviate vector search.

## Architecture

- `indexer_utils/` — Python backend (FastAPI app, GraphQL schema, models, integrations)
- `src/` — React/TypeScript frontend (Relay, MUI)
- `alembic/` — DB migrations
- `tests/` — Python unit tests
- `e2e/` — Playwright end-to-end tests

## ⚠️ Executable Paths — Critical

All Python tools live in the **local virtualenv**, not the system PATH. Always use the `./venv/bin/` prefix:

```bash
# Python
./venv/bin/python

# Linting & formatting (ruff handles both)
./venv/bin/ruff check .
./venv/bin/ruff format .

# Type checking
./venv/bin/mypy indexer_utils

# Running the dev server
./venv/bin/uvicorn indexer_utils.main:app --reload --host 0.0.0.0 --port 8000

# Running Python tests
./venv/bin/pytest tests/
```

Never run `python`, `ruff`, `mypy`, or `uvicorn` without the `./venv/bin/` prefix — they will resolve to wrong system versions or fail entirely.

For JS/TS tools, use `npx` or the `npm run` scripts:

```bash
# Preferred: use npm scripts (these handle relay-compiler ordering)
npm run lint        # relay-compiler + tsc + prettier + eslint
npm run format      # prettier --write

# Or directly via npx
npx relay-compiler
npx tsc --noEmit
npx prettier --check "src/**/*.{js,jsx,ts,tsx,less}"
npx eslint "src/**/*.{js,jsx,ts,tsx}"
```

## Code Style & Linting

### Python

- **Formatter/linter**: `ruff` (configured in `pyproject.toml`) — replaces black + isort + flake8
- **Type checker**: `mypy` in strict mode (`mypy.ini`)
- Line length: 88, Python 3.9 target
- Always run `./venv/bin/ruff format .` before `./venv/bin/ruff check .`
- Fix lint errors automatically: `./venv/bin/ruff check --fix .`

### TypeScript / React

- **Formatter**: `prettier` (v2)
- **Linter**: `eslint` with TypeScript, React, import, jsx-a11y plugins
- **Type checker**: `tsc --noEmit`
- **Important**: `relay-compiler` must run before `tsc` or `eslint` because it generates types in `src/__generated__/`
- Run `npm run lint` to do all of the above in the correct order

## Before Committing

Always run both:

```bash
./venv/bin/ruff format . && ./venv/bin/ruff check .
npm run lint
```

## Common Pitfalls

- **Relay-generated files**: Files in `src/__generated__/` are auto-generated — never edit them manually. Run `npx relay-compiler` to regenerate after changing GraphQL queries/mutations.
- **GraphQL schema**: Defined via Strawberry in `indexer_utils/schema.py`. After schema changes, re-run `relay-compiler`.
- **alembic migrations**: Use `./venv/bin/alembic upgrade head` to apply migrations. Generate new ones with `./venv/bin/alembic revision --autogenerate -m "description"`.
- **mypy strict mode**: The codebase uses `strict = True`. Add proper type annotations to all new functions. Use `Optional[X]` or `X | None` for nullable types.
- **venv not activated**: If a tool is missing, check `./venv/bin/` before assuming it's not installed.

## Dev Server

```bash
# Backend only
bash dev_server.sh
# (runs: DEBUG=true uvicorn indexer_utils.main:app --reload --host 0.0.0.0 --port 8000)

# Frontend (in a separate terminal)
npm run dev
# (runs: relay-compiler && webpack --watch --mode development)
```
