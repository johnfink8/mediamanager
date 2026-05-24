# mediamanager

A self-hosted media manager I built and use daily. Pulls new releases through Radarr and Sonarr, filters them against my taste rules, and uses an LLM to pick what to watch next from what's already in the library.

## What's interesting

- **Agentic recommender** — [`ai_recs.py`](indexer_utils/ai_recs.py) drives a recommendation Agent built on the [openai-agents SDK](https://github.com/openai/openai-agents-python) ([`ai_tools/`](indexer_utils/ai_tools/)) over a set of inspection / search / discovery tools. The model iterates — looks up a candidate, checks prior feedback, searches the library for taste-matched neighbours, web-searches current releases — then returns a structured verdict.
- **Vector recall on synopses.** Each item's synopsis is embedded into a pgvector column; cosine similarity surfaces taste-matched library examples and a nearest-neighbour accept-rate signal for the recommender.
- **GraphQL + Relay with live subscriptions.** Strawberry on the server, Relay on the client, `graphql-ws` so background ingest jobs push UI updates.
- **Scheduler-driven ingest.** APScheduler runs the indexer checks on a schedule and writes through a Redis cache; the GraphQL layer reads through the cache, not the upstream APIs.

## Stack

| Layer        | Stack                                                                      |
| ------------ | -------------------------------------------------------------------------- |
| Backend      | Python 3.11, FastAPI, Strawberry GraphQL, SQLAlchemy, Alembic, APScheduler |
| Frontend     | TypeScript, React, Relay, MUI                                              |
| Data         | Postgres + pgvector (primary + vectors), Redis (cache)                     |
| Integrations | Plex, Radarr, Sonarr, TMDB, OpenAI                                         |
| Tests        | pytest, Playwright                                                         |
| Infra        | Docker, Docker Compose, GitHub Actions                                     |

## Running it

Needs a Plex server, Radarr, Sonarr, and API keys for them plus TMDB and OpenAI.

```sh
cp .env.sample .env
docker compose up -d
```

Dev loop without containers:

```sh
bash dev_server.sh   # FastAPI, port 8000
npm run dev          # webpack + relay-compiler, watching
```

GraphQL at `/graphql`. Frontend served by FastAPI at root.

## Scope

Tailored to my setup. Auth is handled at the reverse proxy in front. Some pieces (the feedback loop, the agent tool set) are shaped by what I want from the tool.

## License

MIT — see [LICENSE](LICENSE).
