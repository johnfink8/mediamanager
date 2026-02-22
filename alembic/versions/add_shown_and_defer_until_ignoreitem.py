"""add shown and defer_until to IgnoreItem

Revision ID: add_shown_and_defer_until_ignoreitem
Revises: add_movie_recommendations
Create Date: 2026-02-22 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_shown_and_defer_until_ignoreitem"
down_revision: Union[str, Sequence[str], None] = "add_movie_recommendations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "indexer_utils_ignoreitem",
        sa.Column("shown", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "indexer_utils_ignoreitem",
        sa.Column("defer_until", sa.DateTime(), nullable=True),
    )
    op.execute("UPDATE indexer_utils_ignoreitem SET shown = 1 WHERE added = 1")


def downgrade() -> None:
    op.drop_column("indexer_utils_ignoreitem", "defer_until")
    op.drop_column("indexer_utils_ignoreitem", "shown")
