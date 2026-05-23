"""empty message

Revision ID: initial
Revises:
Create Date: 2025-07-13 18:06:52.643836

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("django_session", if_exists=True)
    op.drop_table("django_admin_log", if_exists=True)
    op.drop_table("auth_user_user_permissions", if_exists=True)
    op.drop_table("django_migrations", if_exists=True)
    op.drop_table("auth_group_permissions", if_exists=True)
    op.drop_table("auth_user_groups", if_exists=True)
    op.drop_table("auth_permission", if_exists=True)
    op.drop_table("django_content_type", if_exists=True)
    op.drop_table("auth_user", if_exists=True)
    op.drop_table("auth_group", if_exists=True)
    op.alter_column(
        "indexer_utils_ignoreitem",
        "id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        autoincrement=True,
    )
    op.alter_column(
        "indexer_utils_ignoreitem",
        "type",
        existing_type=sa.String(length=2),
        type_=sa.Enum("mv", "tv", name="type_choices"),
        existing_nullable=False,
    )
    op.alter_column(
        "indexer_utils_ignoreitem",
        "ignore",
        existing_type=sa.Boolean(),
        nullable=True,
    )
    op.alter_column(
        "indexer_utils_ignoreitem",
        "added",
        existing_type=sa.Boolean(),
        nullable=True,
    )
    op.alter_column(
        "indexer_utils_ignoreitem",
        "title",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema.

    This migration was applied against the original django-managed schema;
    the verbatim downgrade has been retired now that the project is on
    postgres. Re-create the django auth tables manually if you need them.
    """
    raise NotImplementedError(
        "downgrade past the initial revision is unsupported on postgres"
    )
