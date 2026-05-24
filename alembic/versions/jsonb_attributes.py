"""convert IgnoreItem.attributes from json to jsonb

pgloader landed the column as ``json`` (matching the MySQL ``JSON``
shape). We need ``jsonb`` for the postgres-side filter SQL that uses
``attributes[...]`` subscripting and ``@>`` containment — both of which
are jsonb-only operators.

Revision ID: jsonb_attributes
Revises: add_pgvector_synopsis
Create Date: 2026-05-23 23:30:00.000000
"""

from typing import Sequence, Union

from alembic import op

revision: str = "jsonb_attributes"
down_revision: Union[str, Sequence[str], None] = "add_pgvector_synopsis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE indexer_utils_ignoreitem "
        "ALTER COLUMN attributes TYPE jsonb USING attributes::jsonb"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE indexer_utils_ignoreitem "
        "ALTER COLUMN attributes TYPE json USING attributes::json"
    )
