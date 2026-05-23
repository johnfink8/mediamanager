"""add pgvector synopsis embeddings

Enables the pgvector extension, adds a ``synopsis_vector`` column on
``indexer_utils_ignoreitem`` (1536-dim, matching OpenAI's
``text-embedding-3-small``), and creates an HNSW cosine index.

Revision ID: add_pgvector_synopsis
Revises: add_ingest_and_titles_jobs
Create Date: 2026-05-23 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "add_pgvector_synopsis"
down_revision: Union[str, Sequence[str], None] = "add_ingest_and_titles_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SYNOPSIS_DIMS = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "indexer_utils_ignoreitem",
        sa.Column("synopsis_vector", Vector(SYNOPSIS_DIMS), nullable=True),
    )
    op.execute(
        "CREATE INDEX indexer_utils_ignoreitem_synopsis_vector_hnsw "
        "ON indexer_utils_ignoreitem "
        "USING hnsw (synopsis_vector vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 128)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS indexer_utils_ignoreitem_synopsis_vector_hnsw")
    op.drop_column("indexer_utils_ignoreitem", "synopsis_vector")
