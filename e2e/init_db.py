"""Create all database tables from SQLAlchemy models.

Used in the test environment in place of alembic migrations, which assume a
pre-existing schema.  The production database is managed separately; fresh
test databases need tables created from scratch before the app can start.
"""

import indexer_utils.models  # noqa: F401 — registers all models on Base
from indexer_utils.session import Base, get_engine


def init() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("Tables created")


if __name__ == "__main__":
    init()
