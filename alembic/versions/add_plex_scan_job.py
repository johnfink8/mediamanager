"""add plex library scan APScheduler job

Creates the ``apscheduler_jobs`` jobstore table and registers the weekly
Plex library scan job (Wednesday 02:00 UTC).

Revision ID: add_plex_scan_job
Revises: add_shown_and_defer_until_ignoreitem
Create Date: 2026-05-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_plex_scan_job"
down_revision: Union[str, Sequence[str], None] = "add_shown_and_defer_until_ignoreitem"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JOBSTORE_TABLE = "apscheduler_jobs"
JOB_ID = "plex_library_scan"
SCAN_FUNC_PATH = "indexer_utils.plex_utils:scan_and_index_plex_library"


def upgrade() -> None:
    bind = op.get_bind()

    # Create the APScheduler jobstore table explicitly so its existence is
    # owned by Alembic rather than implicit on first scheduler boot. The
    # column types must match SQLAlchemyJobStore's expected schema exactly.
    op.create_table(
        JOBSTORE_TABLE,
        sa.Column("id", sa.Unicode(191), primary_key=True),
        sa.Column("next_run_time", sa.Float(25), index=True),
        sa.Column("job_state", sa.LargeBinary, nullable=False),
    )

    # Register the recurring job by piggy-backing on APScheduler's own API
    # so the trigger and func reference get pickled into ``job_state`` in
    # the exact format the runtime scheduler expects.
    jobstore = SQLAlchemyJobStore(engine=bind.engine, tablename=JOBSTORE_TABLE)
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_jobstore(jobstore, alias="default")
    # Start in ``paused`` mode so APScheduler flushes the pending job into
    # the jobstore (writing the pickled ``job_state``) without firing the
    # job's worker thread inside Alembic.
    scheduler.start(paused=True)
    try:
        scheduler.add_job(
            SCAN_FUNC_PATH,
            trigger=CronTrigger(day_of_week="wed", hour=2, minute=0, timezone="UTC"),
            id=JOB_ID,
            name="Weekly Plex library scan",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
    finally:
        scheduler.shutdown(wait=False)


def downgrade() -> None:
    op.drop_table(JOBSTORE_TABLE)
