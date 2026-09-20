"""resize synopsis vectors from 1536 to 768

The embedding model moved from OpenAI's ``text-embedding-3-small`` (1536-dim)
to ``embeddinggemma-cpu`` (768-dim, L2-normalized). The old column is renamed
to ``synopsis_vector_1536`` and kept in place (its data is stale but harmless
— it costs ~30MB and a future drop migration removes it once the re-embed
has been validated). A fresh 768-dim ``synopsis_vector`` column with a new
HNSW cosine index takes its place; the catalog is re-embedded by
``backfill_synopsis_vectors.py`` immediately after this migration.

Revision ID: resize_pgvector_synopsis
Revises: jsonb_attributes
Create Date: 2026-09-20 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "resize_pgvector_synopsis"
down_revision: Union[str, Sequence[str], None] = "jsonb_attributes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_DIMS = 768
INDEX_NAME = "indexer_utils_ignoreitem_synopsis_vector_hnsw"


def _create_hnsw() -> None:
    op.execute(
        f"CREATE INDEX {INDEX_NAME} ON indexer_utils_ignoreitem "
        "USING hnsw (synopsis_vector vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 128)"
    )


def upgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.execute(
        "ALTER TABLE indexer_utils_ignoreitem "
        "RENAME COLUMN synopsis_vector TO synopsis_vector_1536"
    )
    op.add_column(
        "indexer_utils_ignoreitem",
        sa.Column("synopsis_vector", Vector(NEW_DIMS), nullable=True),
    )
    _create_hnsw()


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.drop_column("indexer_utils_ignoreitem", "synopsis_vector")
    op.execute(
        "ALTER TABLE indexer_utils_ignoreitem "
        "RENAME COLUMN synopsis_vector_1536 TO synopsis_vector"
    )
    _create_hnsw()
