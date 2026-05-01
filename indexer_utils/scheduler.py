"""Application-wide APScheduler instance and admin helpers.

The scheduler uses a ``SQLAlchemyJobStore`` backed by the same MySQL
database the rest of the app uses, so:

* Multiple uvicorn/gunicorn workers see the same job table and the
  jobstore's row-level lock prevents duplicate fires.
* The next-fire-time survives process restarts (no scan-on-boot).
* Jobs are declared by Alembic migrations rather than re-registered
  every startup.

A single module-level scheduler instance is created lazily so tests and
migrations can import the helpers without spinning up a worker thread.

The :data:`JOB_DESCRIPTIONS` registry is the canonical source for
human-readable names + descriptions of the jobs the admin UI lists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apscheduler.job import Job
from apscheduler.jobstores.base import BaseJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .session import get_db_url

logger = logging.getLogger(__name__)

JOBSTORE_ALIAS = "default"
JOBSTORE_TABLE = "apscheduler_jobs"

PLEX_SCAN_JOB_ID = "plex_library_scan"
CHECK_NEW_JOB_ID = "check_new_items"
CHECK_TITLES_JOB_ID = "check_titles"


@dataclass(frozen=True)
class JobDescription:
    name: str
    description: str


# Canonical metadata for every scheduled job. New jobs added by future
# migrations should also add an entry here so the admin UI can label them.
JOB_DESCRIPTIONS: Dict[str, JobDescription] = {
    PLEX_SCAN_JOB_ID: JobDescription(
        name="Scan Plex library",
        description=(
            "Walk every Plex library section and upsert its movies and "
            "shows into the local index, marking each as added + ignored."
        ),
    ),
    CHECK_NEW_JOB_ID: JobDescription(
        name="Ingest new items",
        description=(
            "Query configured indexers for new movies and shows across "
            "1, 4, and 30-day windows and queue candidates for review."
        ),
    ),
    CHECK_TITLES_JOB_ID: JobDescription(
        name="Refresh titles & thumbnails",
        description=(
            "Backfill checked titles and poster URLs for items waiting "
            "on metadata from TVDB / IMDB / Radarr."
        ),
    ),
}


_scheduler: Optional[BackgroundScheduler] = None


class _MigrationOwnedJobStore(SQLAlchemyJobStore):  # type: ignore[misc]
    """``SQLAlchemyJobStore`` whose table is owned by Alembic, not the store.

    Upstream's ``start()`` calls ``self.jobs_t.create(engine, True)`` on
    every scheduler boot. ``checkfirst=True`` does an inspect + CREATE in
    two non-atomic steps, so when several gunicorn workers boot in
    parallel one of them races and crashes with
    ``Table 'apscheduler_jobs' already exists`` (MySQL error 1050). The
    migration ``add_plex_scan_job`` already owns the DDL — skip the
    redundant create.
    """

    def start(self, scheduler: Any, alias: str) -> None:
        # Skip ``self.jobs_t.create(...)`` by calling the grandparent
        # ``BaseJobStore.start`` directly (which only stashes the
        # scheduler reference and alias).
        BaseJobStore.start(self, scheduler, alias)


def build_jobstore() -> SQLAlchemyJobStore:
    """Construct a jobstore pointed at the app database."""
    return _MigrationOwnedJobStore(url=get_db_url(), tablename=JOBSTORE_TABLE)


def get_scheduler() -> BackgroundScheduler:
    """Return the process-wide scheduler, instantiating on first call."""
    global _scheduler
    if _scheduler is None:
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_jobstore(build_jobstore(), alias=JOBSTORE_ALIAS)
        _scheduler = scheduler
    return _scheduler


def start_scheduler() -> BackgroundScheduler:
    """Start the scheduler if it is not already running."""
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started (jobstore=%s)", JOBSTORE_TABLE)
    return scheduler


def shutdown_scheduler() -> None:
    """Shut down the scheduler if it is running."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")
    _scheduler = None


# ---------------------------------------------------------------------------
# Admin helpers — used by the GraphQL admin API.
# ---------------------------------------------------------------------------


def _trigger_payload(trigger: Any) -> Dict[str, Any]:
    """Render a job's trigger as JSON-serialisable data for the API."""
    if isinstance(trigger, CronTrigger):
        fields = {f.name: str(f) for f in trigger.fields}
        # Rebuild a familiar 5-field cron expression for display.
        order = ("minute", "hour", "day", "month", "day_of_week")
        expression = " ".join(fields.get(k, "*") for k in order)
        return {"kind": "cron", "expression": expression, "fields": fields}
    if isinstance(trigger, IntervalTrigger):
        seconds = int(trigger.interval.total_seconds())
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs or not parts:
            parts.append(f"{secs}s")
        return {
            "kind": "interval",
            "expression": "every " + " ".join(parts),
            "fields": {
                "hours": str(hours),
                "minutes": str(minutes),
                "seconds": str(secs),
            },
        }
    return {"kind": "other", "expression": str(trigger), "fields": {}}


def job_to_dict(job: Job) -> Dict[str, Any]:
    """Render a Job into the shape the admin UI consumes."""
    desc = JOB_DESCRIPTIONS.get(job.id)
    return {
        "id": job.id,
        "name": (desc.name if desc else job.name) or job.id,
        "description": desc.description if desc else "",
        "next_run_time": (job.next_run_time.isoformat() if job.next_run_time else None),
        "paused": job.next_run_time is None,
        "trigger": _trigger_payload(job.trigger),
    }


def list_scheduled_jobs() -> List[Dict[str, Any]]:
    """Return every scheduled job as a serialisable dict."""
    scheduler = get_scheduler()
    return [job_to_dict(j) for j in scheduler.get_jobs(jobstore=JOBSTORE_ALIAS)]


def get_scheduled_job(job_id: str) -> Optional[Dict[str, Any]]:
    scheduler = get_scheduler()
    job = scheduler.get_job(job_id, jobstore=JOBSTORE_ALIAS)
    return None if job is None else job_to_dict(job)


def trigger_job_now(job_id: str) -> Optional[Dict[str, Any]]:
    """Reschedule ``job_id`` so the worker picks it up on the next tick.

    Works for both running and paused jobs (paused jobs get re-armed with
    a real ``next_run_time`` and remain on their existing trigger after
    the manual fire completes).
    """
    scheduler = get_scheduler()
    job = scheduler.get_job(job_id, jobstore=JOBSTORE_ALIAS)
    if job is None:
        return None
    job.modify(next_run_time=datetime.now(timezone.utc))
    return get_scheduled_job(job_id)


# ``trigger_plex_scan_now`` is kept for the legacy POST /admin/scan_plex/
# endpoint; new code should call ``trigger_job_now(PLEX_SCAN_JOB_ID)``.
def trigger_plex_scan_now() -> Optional[str]:
    payload = trigger_job_now(PLEX_SCAN_JOB_ID)
    return None if payload is None else payload["next_run_time"]


def pause_job(job_id: str) -> Optional[Dict[str, Any]]:
    scheduler = get_scheduler()
    job = scheduler.get_job(job_id, jobstore=JOBSTORE_ALIAS)
    if job is None:
        return None
    job.pause()
    return get_scheduled_job(job_id)


def resume_job(job_id: str) -> Optional[Dict[str, Any]]:
    scheduler = get_scheduler()
    job = scheduler.get_job(job_id, jobstore=JOBSTORE_ALIAS)
    if job is None:
        return None
    job.resume()
    return get_scheduled_job(job_id)


def update_job_trigger(
    job_id: str,
    *,
    kind: str,
    cron: Optional[Dict[str, str]] = None,
    interval: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """Replace ``job_id``'s trigger.

    ``kind`` is ``"cron"`` (use ``cron`` dict — same field names APScheduler
    accepts: ``year``/``month``/``day``/``day_of_week``/``hour``/``minute``/
    ``second``) or ``"interval"`` (use ``interval`` dict with ``weeks``/
    ``days``/``hours``/``minutes``/``seconds``).
    """
    scheduler = get_scheduler()
    job = scheduler.get_job(job_id, jobstore=JOBSTORE_ALIAS)
    if job is None:
        return None
    if kind == "cron":
        new_trigger = CronTrigger(timezone="UTC", **(cron or {}))
    elif kind == "interval":
        new_trigger = IntervalTrigger(timezone="UTC", **(interval or {}))
    else:
        raise ValueError(f"Unsupported trigger kind: {kind!r}")
    job.reschedule(trigger=new_trigger)
    return get_scheduled_job(job_id)
