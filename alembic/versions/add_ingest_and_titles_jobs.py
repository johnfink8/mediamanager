"""register check_new and check_titles APScheduler jobs

Adds two more APScheduler jobs to the persistent jobstore:

* ``check_new_items`` — replaces the legacy ``GET /check_new/`` cron-curl.
* ``check_titles`` — replaces the legacy ``GET /check_titles/`` cron-curl.

Also re-points the existing ``plex_library_scan`` job at the unified
``indexer_utils.jobs:run_plex_library_scan`` wrapper so all three jobs
live in one module.

Revision ID: add_ingest_and_titles_jobs
Revises: add_plex_scan_job
Create Date: 2026-05-01 00:00:01.000000
"""

from typing import Sequence, Union

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_ingest_and_titles_jobs"
down_revision: Union[str, Sequence[str], None] = "add_plex_scan_job"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JOBSTORE_TABLE = "apscheduler_jobs"

PLEX_SCAN_JOB_ID = "plex_library_scan"
CHECK_NEW_JOB_ID = "check_new_items"
CHECK_TITLES_JOB_ID = "check_titles"

PLEX_SCAN_FUNC = "indexer_utils.jobs:run_plex_library_scan"
CHECK_NEW_FUNC = "indexer_utils.jobs:run_check_new_items"
CHECK_TITLES_FUNC = "indexer_utils.jobs:run_check_titles"


def _make_scheduler() -> BackgroundScheduler:
    bind = op.get_bind()
    jobstore = SQLAlchemyJobStore(engine=bind.engine, tablename=JOBSTORE_TABLE)
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_jobstore(jobstore, alias="default")
    return scheduler


def upgrade() -> None:
    scheduler = _make_scheduler()
    # Start paused so add_job actually flushes to the jobstore (writing
    # the pickled job_state) without firing the worker thread.
    scheduler.start(paused=True)
    try:
        scheduler.add_job(
            PLEX_SCAN_FUNC,
            trigger=CronTrigger(day_of_week="wed", hour=2, minute=0, timezone="UTC"),
            id=PLEX_SCAN_JOB_ID,
            name="Weekly Plex library scan",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            CHECK_NEW_FUNC,
            trigger=IntervalTrigger(minutes=30, timezone="UTC"),
            id=CHECK_NEW_JOB_ID,
            name="Ingest new items",
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            CHECK_TITLES_FUNC,
            trigger=IntervalTrigger(minutes=15, timezone="UTC"),
            id=CHECK_TITLES_JOB_ID,
            name="Refresh titles & thumbnails",
            replace_existing=True,
            misfire_grace_time=600,
            coalesce=True,
            max_instances=1,
        )
    finally:
        scheduler.shutdown(wait=False)


def downgrade() -> None:
    scheduler = _make_scheduler()
    scheduler.start(paused=True)
    try:
        for job_id in (CHECK_TITLES_JOB_ID, CHECK_NEW_JOB_ID):
            try:
                scheduler.remove_job(job_id, jobstore="default")
            except Exception:
                pass
        # Restore the previous func target on the plex scan job.
        try:
            scheduler.modify_job(
                PLEX_SCAN_JOB_ID,
                jobstore="default",
                func="indexer_utils.plex_utils:scan_and_index_plex_library",
            )
        except Exception:
            pass
    finally:
        scheduler.shutdown(wait=False)
