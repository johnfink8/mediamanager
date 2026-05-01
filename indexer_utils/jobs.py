"""Top-level callables registered with APScheduler.

APScheduler pickles its job's ``func`` reference into the jobstore as a
``module:attribute`` string, so each job target needs a stable, importable
location. Keep these wrappers thin — actual work lives in ``vid_utils``
and ``plex_utils``.
"""

from __future__ import annotations

import logging

from .plex_utils import scan_and_index_plex_library
from .vid_utils import check_movies, check_shows, get_movie_titles, get_show_titles

logger = logging.getLogger(__name__)

CHECK_NEW_DAYS = (1, 4, 30)


def _signal_event(item_type: str) -> None:
    """Notify any open GraphQL subscriptions that the queue changed."""
    # Lazy import: schema.py is heavy and we don't want to pull it in
    # during scheduler / migration boot.
    from .schema import events

    event = events.get(item_type)
    if event is not None:
        event.set()


def run_plex_library_scan() -> None:
    """Job: weekly walk of every Plex library, upsert ``IgnoreItem`` rows."""
    scan_and_index_plex_library()


def run_check_new_items() -> None:
    """Job: ingest new candidates from the configured indexers.

    Mirrors the work the legacy ``GET /check_new/`` curl-cron used to do —
    runs the movie + show checks across the configured day-windows and
    fires the GraphQL subscription events so any open clients refresh.
    """
    logger.info("Scheduled check_new starting")
    for window in CHECK_NEW_DAYS:
        try:
            check_movies(window)
        except Exception:
            logger.exception("check_movies(%s) failed", window)
    _signal_event("mv")
    for window in CHECK_NEW_DAYS:
        try:
            check_shows(window)
        except Exception:
            logger.exception("check_shows(%s) failed", window)
    _signal_event("tv")
    logger.info("Scheduled check_new finished")


def run_check_titles() -> None:
    """Job: backfill checked titles + thumbnail URLs for pending items."""
    logger.info("Scheduled check_titles starting")
    try:
        get_movie_titles()
    except Exception:
        logger.exception("get_movie_titles failed")
    _signal_event("mv")
    try:
        get_show_titles()
    except Exception:
        logger.exception("get_show_titles failed")
    _signal_event("tv")
    logger.info("Scheduled check_titles finished")
