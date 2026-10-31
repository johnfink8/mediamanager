# AGENTS.md

## What this is

**mediamanager** — a personal media-queue app: FastAPI + Strawberry GraphQL backend,
React/Relay frontend, Postgres 16 + pgvector. It ingests new movie/TV candidates from
indexers, runs an agentic AI recommendation pipeline (openai-agents SDK) over each
candidate, and shows the user a ranked queue to add or ignore. Plex/Radarr/Sonarr
integrations track what's already owned.

The deep guide for working in this repo is **CLAUDE.md** (architecture, the
recommendation pipeline, DB conventions, tooling, testing). Read it first; this file
only covers what the review panel needs up front.

## Who uses it

One user (the repo owner) on a home LAN. No multi-tenancy, no public exposure: the app
sits behind Authelia forward-auth; the only machine-to-machine surface is an MCP
endpoint for the owner's own AI connector.

## Trust model

- **Model server**: a self-hosted OpenAI-compatible gateway (vLLM/sFNX on LAN GPUs).
  Unauthenticated by design on the LAN; it is *expected* to blip (502s, restarts).
  Output from it is guided into Pydantic/JSON-schema shapes before any code consumes
  it; tool arguments are validated and clipped (`ai_tools/shared.py`, `safe_tool.py`).
- **External metadata** (TMDB, Radarr/Sonarr, Plex, Brave Search, web pages the agent
  fetches): untrusted content. It is treated as text fed to the model or clipped into
  JSONB attributes — never executed, and DB access from tools is read-only via
  parameterized SQLAlchemy.
- **Writes**: the agent's tools only read; state changes (add/ignore/retry) come from
  the authenticated GraphQL UI or the owner's MCP calls.

## Stage

Personal production: continuously deployed (git pull → docker compose build on the
home server; alembic migrations run at container start). Reliability matters more than
feature velocity; anything that silently loses a user decision or queues garbage is a
bug, while transient model/gateway failures are expected and must self-heal.
