"""Shared async fixtures for the test suite.

Tests run against a real pgvector postgres (the ``db`` service in
``docker-compose.test.yml``) — there is no longer a SQLite in-memory
shim, because the production code uses pgvector cosine ops and
postgres-specific JSONB operators that SQLite can't compile.

* ``_init_schema`` (session-scoped, autouse): create ``vector`` extension
  and all model tables on first use. Runs once.
* ``_truncate_tables`` (per-test, autouse): wipe every model-managed
  table before each test so they see a clean DB. Faster than dropping
  and recreating.
"""

import pytest_asyncio
from sqlalchemy import text

import indexer_utils.models  # noqa: F401 — register models on Base
from indexer_utils.session import Base, get_engine


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _init_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(_init_schema):
    engine = get_engine()
    table_names = ", ".join(Base.metadata.tables.keys())
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
    yield
