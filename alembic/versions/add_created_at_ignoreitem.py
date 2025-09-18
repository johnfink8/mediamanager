"""add created_at to IgnoreItem

Revision ID: add_created_at_ignoreitem
Revises: f3feb6c5579f
Create Date: 2025-07-13 20:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_created_at_ignoreitem"
down_revision: Union[str, Sequence[str], None] = "f3feb6c5579f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "indexer_utils_ignoreitem", sa.Column("created_at", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("indexer_utils_ignoreitem", "created_at")
