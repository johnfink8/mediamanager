"""register the annotation-repair APScheduler job

Adds ``annotation_repairs`` — an hourly pass that re-annotates pending
candidates whose AI recommendation is missing or failed (gateway 502s,
connection drops, embedding outages). Selection and per-item exponential
backoff live in ``indexer_utils.repairs``.

Revision ID: add_annotation_repair_job
Revises: drop_pgvector_synopsis_1536
Create Date: 2026-10-31 00:00:00.000000
"""

from typing import Sequence, Union

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_annotation_repair_job"
down_revision: Union[str, Sequence[str], None] = "drop_pgvector_synopsis_1536"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JOBSTORE_TABLE = "apscheduler_jobs"
REPAIR_JOB_ID = "annotation_repairs"
REPAIR_FUNC = "indexer_utils.jobs:run_annotation_repairs"


def _make_scheduler() -> BackgroundScheduler:
    bind = op.get_bind()
    jobstore = SQLAlchemyJobStore(engine=bind.engine, tablename=JOBSTORE_TABLE)
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_jobstore(jobstore, alias="default")
    return scheduler


def upgrade() -> None:
    scheduler = _make_scheduler()
    # Start paused so add_job flushes the pickled job_state to the jobstore
    # without firing the worker thread inside Alembic.
    scheduler.start(paused=True)
    try:
        scheduler.add_job(
            REPAIR_FUNC,
            trigger=IntervalTrigger(hours=1, timezone="UTC"),
            id=REPAIR_JOB_ID,
            name="Repair failed AI recommendations",
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
        try:
            scheduler.remove_job(REPAIR_JOB_ID)
        except Exception:
            pass
    finally:
        scheduler.shutdown(wait=False)
