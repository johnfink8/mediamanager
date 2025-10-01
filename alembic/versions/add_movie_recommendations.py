"""add movie recommendations history

Revision ID: add_movie_recommendations
Revises: add_created_at_ignoreitem
Create Date: 2025-07-14 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_movie_recommendations"
down_revision: Union[str, Sequence[str], None] = "add_created_at_ignoreitem"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "movie_recommendations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("recommended_imdb_id", sa.String(length=32), nullable=True),
        sa.Column("recommended_title", sa.String(length=255), nullable=True),
        sa.Column("recommended_reason", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column(
            "preference",
            sa.Enum(
                "LIKE",
                "NOT_NOW",
                "NEVER",
                name="movie_recommendation_preference",
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("movie_recommendations")
