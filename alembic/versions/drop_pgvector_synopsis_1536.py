"""drop the stale 1536-dim synopsis vector column

Follow-up to ``resize_pgvector_synopsis``: the catalog has been re-embedded
to 768 dims and the old ``synopsis_vector_1536`` data (preserved as a
rollback point during the cutover) is no longer needed.

Run only after the qwen3.8/embeddinggemma quality gate has been signed off.

Revision ID: drop_pgvector_synopsis_1536
Revises: resize_pgvector_synopsis
Create Date: 2026-09-20 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "drop_pgvector_synopsis_1536"
down_revision: Union[str, Sequence[str], None] = "resize_pgvector_synopsis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("indexer_utils_ignoreitem", "synopsis_vector_1536")


def downgrade() -> None:
    # Data loss: the original 1536-dim vectors cannot be recovered.
    op.add_column(
        "indexer_utils_ignoreitem",
        sa.Column("synopsis_vector_1536", Vector(1536), nullable=True),
    )
