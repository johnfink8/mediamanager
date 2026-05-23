"""Create all database tables from SQLAlchemy models.

Used in the test environment in place of alembic migrations, which assume a
pre-existing schema.  The production database is managed separately; fresh
test databases need tables created from scratch before the app can start.
"""

from sqlalchemy import text

import indexer_utils.models  # noqa: F401 — registers all models on Base
from indexer_utils.session import Base, get_engine


def init() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        # pgvector must exist before create_all compiles the vector column.
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    print("Tables created")


if __name__ == "__main__":
    init()
