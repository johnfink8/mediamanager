"""Application-wide APScheduler instance and admin helpers.

The scheduler uses a ``SQLAlchemyJobStore`` backed by the same postgres
database the rest of the app uses, plus a postgres advisory lock
(``pg_try_advisory_lock``) for leader election so:

* Only **one** worker fires jobs at a time, even though every worker
  imports this module — APScheduler has no built-in cross-process
  coordination, so unsynchronized starts would cause every job to fire
  N times.
* The non-leader workers ("followers") still initialize the jobstore
  in paused mode so the admin GraphQL API works on any worker.
* The next-fire-time survives process restarts (no scan-on-boot).
* Jobs are declared by Alembic migrations rather than re-registered
  every startup.

The :data:`JOB_DESCRIPTIONS` registry is the canonical source for
human-readable names + descriptions of the jobs the admin UI lists.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    JobExecutionEvent,
    JobSubmissionEvent,
)
from apscheduler.job import Job
from apscheduler.jobstores.base import BaseJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

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


# Leader election. Exactly one worker per cluster holds this lock and
# fires jobs; everyone else runs the scheduler in paused mode so the
# admin GraphQL API still works for reads/writes against the jobstore.
LEADER_LOCK_NAME = "mediamanager_apscheduler_leader"


def _lock_key(name: str) -> int:
    """Map a stable string to the signed bigint pg_advisory_lock expects."""
    digest = hashlib.sha1(name.encode()).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


LEADER_LOCK_KEY = _lock_key(LEADER_LOCK_NAME)

# How long to wait between leader heartbeats. Postgres session-level
# advisory locks aren't released on idle but the heartbeat doubles as
# a liveness check on the lock-holding connection.
LEADER_HEARTBEAT_SECONDS = 300

# Cap on how long the leader will wait between jobstore polls. Cross-
# process job edits (e.g., a "Trigger now" click that lands on a
# follower) only update the row; APScheduler's ``wakeup()`` doesn't
# cross processes. Polling at least every 30s keeps the manual-trigger
# UX responsive.
LEADER_MAX_WAIT_SECONDS = 30.0


_scheduler: Optional[BackgroundScheduler] = None
_lock_engine: Optional[Engine] = None
_lock_conn: Optional[Connection] = None
_heartbeat_thread: Optional[threading.Thread] = None
_heartbeat_stop = threading.Event()
_is_leader = False

# Per-job submission timestamps so the EXECUTED/ERROR listener can report
# wall-clock duration. Keyed by APScheduler's run-instance id.
_job_started_at: Dict[str, float] = {}
_job_started_lock = threading.Lock()


class _LeaderScheduler(BackgroundScheduler):  # type: ignore[misc]
    """``BackgroundScheduler`` that clamps the inter-poll wait.

    APScheduler computes ``next_wakeup`` from the next due job time and
    sleeps until then. With multiple workers sharing a jobstore, a
    follower's ``Job.modify(next_run_time=...)`` updates the row but
    can't wake the leader's event loop. Capping the wait ensures the
    leader picks up cross-process edits within
    :data:`LEADER_MAX_WAIT_SECONDS`. On followers this method is never
    productively called (they stay paused), so the cap is a no-op.
    """

    def _process_jobs(self) -> Optional[float]:
        wait: Optional[float] = super()._process_jobs()
        if wait is None or wait > LEADER_MAX_WAIT_SECONDS:
            return LEADER_MAX_WAIT_SECONDS
        return wait


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
        scheduler = _LeaderScheduler(timezone="UTC")
        scheduler.add_jobstore(build_jobstore(), alias=JOBSTORE_ALIAS)
        _scheduler = scheduler
    return _scheduler


def _job_label(job_id: str) -> str:
    desc = JOB_DESCRIPTIONS.get(job_id)
    if desc is None:
        return job_id
    return f"{job_id} ({desc.name})"


def _on_job_submitted(event: JobSubmissionEvent) -> None:
    """Mark a job as in-flight so the executed/error listener can time it."""
    with _job_started_lock:
        _job_started_at[event.job_id] = time.monotonic()
    # ``scheduled_run_times`` is a list (one per coalesced fire). Most jobs
    # have just one. Format the first as the canonical scheduled time.
    runs = event.scheduled_run_times or []
    scheduled = runs[0].isoformat() if runs else "?"
    logger.info(
        "Scheduler dispatching job %s (scheduled_for=%s, coalesced_runs=%d)",
        _job_label(event.job_id),
        scheduled,
        len(runs),
    )


def _on_job_executed(event: JobExecutionEvent) -> None:
    duration = _pop_duration(event.job_id)
    logger.info(
        "Scheduler job %s finished OK in %s",
        _job_label(event.job_id),
        _format_duration(duration),
    )


def _on_job_error(event: JobExecutionEvent) -> None:
    duration = _pop_duration(event.job_id)
    logger.error(
        "Scheduler job %s FAILED after %s: %s",
        _job_label(event.job_id),
        _format_duration(duration),
        event.exception,
    )


def _on_job_missed(event: JobExecutionEvent) -> None:
    logger.warning(
        "Scheduler job %s MISSED its scheduled run at %s "
        "(misfire_grace_time exceeded — likely worker overload or shutdown)",
        _job_label(event.job_id),
        event.scheduled_run_time,
    )


def _pop_duration(job_id: str) -> Optional[float]:
    with _job_started_lock:
        started = _job_started_at.pop(job_id, None)
    return None if started is None else time.monotonic() - started


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?s"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def _attach_job_listeners(scheduler: BackgroundScheduler) -> None:
    """Wire lifecycle listeners so every job fire is logged."""
    scheduler.add_listener(_on_job_submitted, EVENT_JOB_SUBMITTED)
    scheduler.add_listener(_on_job_executed, EVENT_JOB_EXECUTED)
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)


def _log_scheduled_inventory(scheduler: BackgroundScheduler) -> None:
    """Dump the current job table so an operator can confirm it's armed."""
    try:
        jobs = scheduler.get_jobs(jobstore=JOBSTORE_ALIAS)
    except Exception:
        logger.exception("Could not enumerate scheduled jobs at startup")
        return
    if not jobs:
        logger.warning("APScheduler has NO jobs registered — check migrations")
        return
    for job in jobs:
        logger.info(
            "Scheduled job %s — trigger=%s, next_run=%s",
            _job_label(job.id),
            job.trigger,
            job.next_run_time.isoformat() if job.next_run_time else "(paused)",
        )


def is_leader() -> bool:
    """True if this worker holds the leader lock."""
    return _is_leader


def _try_acquire_leader_lock() -> bool:
    """Non-blocking ``pg_try_advisory_lock`` attempt on a dedicated connection.

    Returns ``True`` iff this worker now owns the lock. A separate
    engine + a single, held-open connection are used because
    postgres session-level advisory locks are scoped to the connection;
    releasing the connection releases the lock.
    """
    global _lock_engine, _lock_conn
    engine = create_engine(
        get_db_url(), pool_size=1, max_overflow=0, pool_pre_ping=False
    )
    conn = engine.connect()
    try:
        result = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_LOCK_KEY}
        ).scalar()
    except Exception:
        conn.close()
        engine.dispose()
        raise
    if result is True:
        _lock_engine = engine
        _lock_conn = conn
        return True
    conn.close()
    engine.dispose()
    return False


def _start_leader_heartbeat() -> None:
    """Start a daemon thread that pings the lock connection periodically.

    Doubles as a liveness check: if the held connection dies, the
    follower election can take over on the next worker boot.
    """
    global _heartbeat_thread
    _heartbeat_stop.clear()

    def _run() -> None:
        while not _heartbeat_stop.wait(LEADER_HEARTBEAT_SECONDS):
            try:
                if _lock_conn is not None:
                    _lock_conn.execute(text("SELECT 1"))
            except Exception:
                logger.exception("Leader lock heartbeat ping failed")

    thread = threading.Thread(
        target=_run, name="apscheduler-leader-heartbeat", daemon=True
    )
    thread.start()
    _heartbeat_thread = thread


def _release_leader_lock() -> None:
    """Stop the heartbeat, release the lock, close the connection."""
    global _lock_engine, _lock_conn, _heartbeat_thread
    _heartbeat_stop.set()
    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=5)
        _heartbeat_thread = None
    if _lock_conn is not None:
        try:
            _lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": LEADER_LOCK_KEY}
            )
        except Exception:
            logger.exception("pg_advisory_unlock failed")
        try:
            _lock_conn.close()
        except Exception:
            logger.exception("Closing lock connection failed")
        _lock_conn = None
    if _lock_engine is not None:
        try:
            _lock_engine.dispose()
        except Exception:
            logger.exception("Disposing lock engine failed")
        _lock_engine = None


def start_scheduler() -> BackgroundScheduler:
    """Start the scheduler. Promotes this worker to leader if possible.

    The leader runs the scheduler normally and dispatches jobs.
    Followers start in paused mode — their jobstore is fully
    initialized so the admin GraphQL API works on any worker, but they
    never fire jobs themselves.
    """
    global _is_leader
    scheduler = get_scheduler()
    if scheduler.running:
        return scheduler
    if _try_acquire_leader_lock():
        _is_leader = True
        _attach_job_listeners(scheduler)
        scheduler.start()
        _start_leader_heartbeat()
        logger.info(
            "APScheduler started as LEADER (jobstore=%s, lock=%s)",
            JOBSTORE_TABLE,
            LEADER_LOCK_NAME,
        )
        _log_scheduled_inventory(scheduler)
    else:
        _is_leader = False
        scheduler.start(paused=True)
        logger.info(
            "APScheduler started as FOLLOWER — another worker holds %s; "
            "this worker only serves admin reads/writes",
            LEADER_LOCK_NAME,
        )
    return scheduler


def shutdown_scheduler() -> None:
    """Stop the scheduler and, if leader, release the lock."""
    global _scheduler, _is_leader
    if _scheduler is not None and _scheduler.running:
        try:
            _scheduler.shutdown(wait=False)
            logger.info("APScheduler shut down")
        except Exception:
            logger.exception("APScheduler shutdown failed")
    if _is_leader:
        _release_leader_lock()
        logger.info("Released APScheduler leader lock")
        _is_leader = False
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
