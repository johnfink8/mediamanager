import sys
from typing import Optional

from decouple import config
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

_engine: Optional[AsyncEngine] = None
_SessionFactory: Optional[async_sessionmaker[AsyncSession]] = None


def _is_test_run() -> bool:
    """True if we look like we're being driven by pytest.

    Used to swap ``DB_NAME`` for ``TEST_DB_NAME`` so the test suite never
    points at the real DB. Detecting via ``sys.argv`` (rather than an
    env var) keeps the decision in code — the same .env serves both
    contexts.
    """
    return any("pytest" in arg for arg in sys.argv)


def get_db_url() -> str:
    db_name = config("TEST_DB_NAME") if _is_test_run() else config("DB_NAME")
    return (
        f"postgresql+psycopg://{config('DB_USER')}:{config('DB_PASSWORD')}"
        + f"@{config('DB_HOST')}/{db_name}"
    )


# pgvector's HNSW index returns only its ``ef_search`` (40) nearest rows
# from the whole table, and WHERE filters run after that: a similarity
# search over added TV items (a few percent of the vectors) came back
# with 3 of 10 rows, and with none once any other filter was added.
# Iterative scan keeps walking the index until the filtered query is
# satisfied; strict order keeps results sorted by distance.
_CONNECT_OPTIONS = "-c hnsw.iterative_scan=strict_order"


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            get_db_url(),
            connect_args={"options": _CONNECT_OPTIONS},
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
    return _engine


def db_session() -> AsyncSession:
    """Return a fresh ``AsyncSession`` — use with ``async with``."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _SessionFactory()
