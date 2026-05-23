"""Simulation harness for ``check_movies`` / ``check_shows``.

Goal: let a developer watch the full pipeline run end-to-end against real
OpenAI without touching their real postgres, Radarr, Sonarr, Plex, TMDB, or
the indexer feed.

What's isolated:
- ``IgnoreItem`` storage runs against an in-memory SQLite engine pre-seeded
  with ``fixtures.SEED_MOVIES`` / ``fixtures.SEED_SHOWS``.
- ``search_similar_by_synopsis`` (pgvector-backed in production) is patched
  to return the seeded items of the same ``item_type`` with synthetic
  distances, since SQLite can't run a real cosine query.
- ``IgnoreItem.create`` writes are recorded but not persisted.
- ``record_check_result`` is no-op'd to skip Redis writes.

What stays real:
- OpenAI (chat completions for synopsis + the agent loop).
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from . import fixtures as fx

logger = logging.getLogger(__name__)


@dataclass
class SimulationRecorder:
    """Captured side effects from a simulated run."""

    created_items: List[Dict[str, Any]] = field(default_factory=list)
    indexer_requests: List[Dict[str, Any]] = field(default_factory=list)
    radarr_calls: List[Dict[str, Any]] = field(default_factory=list)
    sonarr_calls: List[Dict[str, Any]] = field(default_factory=list)
    plex_lookups: List[Dict[str, Any]] = field(default_factory=list)
    upsert_calls: List[Dict[str, Any]] = field(default_factory=list)
    seed_summary: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# XML / mock builders
# ---------------------------------------------------------------------------


def _trim_feed_xml(xml_text: str, max_items: Optional[int]) -> str:
    """Return the feed XML truncated to ``max_items`` ``<item>`` elements."""
    if not max_items or max_items <= 0:
        return xml_text
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return xml_text
    items = list(channel.findall("item"))
    for extra in items[max_items:]:
        channel.remove(extra)
    return ET.tostring(root, encoding="unicode")


def _make_fake_indexer_get(
    real_get: Callable, recorder: SimulationRecorder, max_items: Optional[int]
) -> Callable:
    def _fake(url: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        params = params or {}
        t_value = str(params.get("t", ""))
        if t_value in {"2040", "5040"}:
            recorder.indexer_requests.append(
                {"url": url, "params": dict(params), "kwargs": kwargs}
            )
            text = (
                fx.load_movies_feed_xml()
                if t_value == "2040"
                else fx.load_shows_feed_xml()
            )
            text = _trim_feed_xml(text, max_items)
            resp = SimpleNamespace()
            resp.text = text
            resp.status_code = 200
            resp.raise_for_status = lambda: None
            return resp
        return real_get(url, params=params, **kwargs)

    return _fake


def _build_radarr_query(recorder: SimulationRecorder) -> Callable:
    def _fake(cmd: str, method: str = "get", **kwargs: Any) -> Any:
        recorder.radarr_calls.append({"cmd": cmd, "method": method, "kwargs": kwargs})
        if cmd == "movie":
            return []
        if cmd == "movie/lookup":
            term = str(kwargs.get("term", ""))
            if term.startswith("imdb:"):
                imdb_id = term[5:]
                lookup = fx.RADARR_LOOKUPS.get(imdb_id)
                if lookup is None:
                    raise IndexError(f"no radarr fixture for {imdb_id}")
                return [lookup]
            raise ValueError(f"unsupported radarr term: {term}")
        if cmd == "movie/lookup/imdb":
            imdb_id = str(kwargs.get("imdbid", ""))
            return fx.RADARR_LOOKUPS.get(imdb_id, {})
        return []

    return _fake


def _build_sonarr_query_series(recorder: SimulationRecorder) -> Callable:
    def _fake(tvdb: str) -> Dict[str, Any]:
        recorder.sonarr_calls.append({"cmd": "series/lookup", "tvdb": tvdb})
        lookup = fx.SONARR_LOOKUPS.get(str(tvdb))
        if lookup is None:
            raise IndexError(f"no sonarr fixture for tvdb={tvdb}")
        return dict(lookup)

    return _fake


def _build_sn_query(recorder: SimulationRecorder) -> Callable:
    def _fake(cmd: str, post: bool = False, **kwargs: Any) -> Any:
        recorder.sonarr_calls.append({"cmd": cmd, "post": post, "kwargs": kwargs})
        if cmd == "series":
            return []
        if cmd == "series/lookup":
            term = str(kwargs.get("term", ""))
            if term.startswith("tvdb:"):
                tvdb = term[5:]
                lookup = fx.SONARR_LOOKUPS.get(tvdb)
                if lookup is None:
                    raise IndexError(f"no sonarr fixture for tvdb={tvdb}")
                return [lookup]
        return []

    return _fake


def _build_get_movie(recorder: SimulationRecorder) -> Callable:
    def _fake(imdb_id: str) -> Optional[Dict[str, Any]]:
        recorder.radarr_calls.append({"cmd": "get_movie", "imdb_id": imdb_id})
        return None

    return _fake


def _build_find_movie(recorder: SimulationRecorder) -> Callable:
    def _fake(title: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        recorder.plex_lookups.append({"title": title, "year": year})
        # Candidates from the indexer feed are always treated as not-yet-in-Plex.
        return None

    return _fake


def _seed_plex_index() -> Dict[str, Dict[str, Any]]:
    """Build a (lower-cased title, year) -> plex-stub index from SEED_MOVIES.

    Items without a ``plex`` key simulate a movie that's no longer in the
    library (e.g. deleted), surfacing ``plex_status='missing_from_library'``
    on details lookups for added items.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for movie in fx.SEED_MOVIES:
        plex = movie.get("plex")
        if not plex:
            continue
        title = str(movie["title"]).lower()
        index[title] = dict(plex)
        year = movie.get("attributes", {}).get("year")
        if year is not None:
            index[f"{title}|{year}"] = dict(plex)
    return index


def _build_seed_plex_details(
    recorder: SimulationRecorder,
) -> Callable[..., Optional[Dict[str, Any]]]:
    index = _seed_plex_index()

    def _fake(title: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        recorder.plex_lookups.append({"call": "details", "title": title, "year": year})
        if not title:
            return None
        key_with_year = f"{title.lower()}|{year}" if year is not None else None
        if key_with_year and key_with_year in index:
            return dict(index[key_with_year])
        return dict(index[title.lower()]) if title.lower() in index else None

    return _fake


def _build_get_recently_played(recorder: SimulationRecorder) -> Callable:
    def _fake(limit: int = 40) -> List[Dict[str, Any]]:
        recorder.plex_lookups.append({"call": "recently_played", "limit": limit})
        return []

    return _fake


def _build_upsert(recorder: SimulationRecorder) -> Callable:
    async def _fake(
        attrs: Dict[str, Any],
        item_type: str,
        uid: str,
        title: str,
        synopsis: Optional[str],
    ) -> Dict[str, Any]:
        recorder.upsert_calls.append(
            {
                "uid": uid,
                "item_type": item_type,
                "title": title,
                "synopsis_present": bool(synopsis),
            }
        )
        return attrs

    return _fake


def _build_search(recorder: SimulationRecorder) -> Callable:
    """Stand in for ``vector_search.asearch_by_synopsis`` under SQLite.

    Returns the seeded items of the matching ``item_type`` as plausible
    neighbors. ``searches.py`` filters to ``added=True`` post-fetch, so
    rejected items in the fixture are dropped before they reach the LLM.
    """

    async def _fake(query_text: str, k: int, item_type: str) -> List[Dict[str, Any]]:
        seed = fx.SEED_MOVIES if item_type == "mv" else fx.SEED_SHOWS
        results: List[Dict[str, Any]] = []
        for idx, item in enumerate(seed):
            if idx >= k:
                break
            results.append(
                {
                    "uid": item["uid"],
                    "title": item["title"],
                    # Synthetic distances: monotonically increasing so the
                    # ordering is deterministic but distinct.
                    "distance": 0.1 + idx * 0.05,
                }
            )
        return results

    return _fake


def _build_create(recorder: SimulationRecorder) -> Callable:
    def _fake(**kwargs: Any) -> Any:
        recorder.created_items.append(dict(kwargs))
        ns = SimpleNamespace(**kwargs)
        ns.save = lambda: None
        ns.synopsis_vector = None
        return ns

    return _fake


def _async_wrap(sync_fn: Callable[..., Any]) -> Callable[..., Any]:
    async def _aw(*args: Any, **kwargs: Any) -> Any:
        return sync_fn(*args, **kwargs)

    return _aw


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# SQLite seed
# ---------------------------------------------------------------------------


def _build_sqlite_engine() -> Engine:
    from indexer_utils.session import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _seed_sqlite(engine: Engine, recorder: SimulationRecorder) -> None:
    from indexer_utils.models import IgnoreItem

    ts = int(datetime.utcnow().timestamp())
    counts = {
        "mv_added": 0,
        "mv_ignored": 0,
        "mv_undecided": 0,
        "tv_added": 0,
        "tv_ignored": 0,
        "tv_undecided": 0,
    }
    with Session(engine) as session:
        for movie in fx.SEED_MOVIES:
            attrs = dict(movie["attributes"])
            ai = dict(attrs.get("ai") or {})
            ai["synopsis"] = movie["synopsis"]
            attrs["ai"] = ai
            session.add(
                IgnoreItem(
                    item_type="mv",
                    uid=movie["uid"],
                    title=movie["title"],
                    ignore=bool(movie["ignore"]),
                    added=bool(movie["added"]),
                    attributes=attrs,
                    created_at=ts,
                    shown=True,
                )
            )
            if movie["added"]:
                counts["mv_added"] += 1
            elif movie["ignore"]:
                counts["mv_ignored"] += 1
            else:
                counts["mv_undecided"] += 1
        for show in fx.SEED_SHOWS:
            attrs = dict(show["attributes"])
            ai = dict(attrs.get("ai") or {})
            ai["synopsis"] = show["synopsis"]
            attrs["ai"] = ai
            session.add(
                IgnoreItem(
                    item_type="tv",
                    uid=show["uid"],
                    title=show["title"],
                    ignore=bool(show["ignore"]),
                    added=bool(show["added"]),
                    attributes=attrs,
                    created_at=ts,
                    shown=True,
                )
            )
            if show["added"]:
                counts["tv_added"] += 1
            elif show["ignore"]:
                counts["tv_ignored"] += 1
            else:
                counts["tv_undecided"] += 1
        session.commit()
    recorder.seed_summary = counts
    logger.info("seeded sqlite: %s", counts)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def simulate_environment(
    max_feed_items: Optional[int] = None,
) -> Iterator[SimulationRecorder]:
    """Patch external touchpoints, seed an isolated DB + Weaviate, run.

    Args:
        max_feed_items: cap how many ``<item>`` elements survive in the
            fixture XML. Useful for limiting OpenAI cost.
    """
    import requests

    from indexer_utils import (
        ai_recs,
        check_feedback,
        plex_utils,
        radarr_utils,
        sonarr_utils,
        vid_utils,
    )
    from indexer_utils import (
        session as session_module,
    )
    from indexer_utils.ai_tools import inspections as tool_inspections
    from indexer_utils.ai_tools import searches as tool_searches
    from indexer_utils.models import IgnoreItem

    recorder = SimulationRecorder()

    # ---- DB engine + seed ----
    sqlite_engine = _build_sqlite_engine()

    fake_indexer_get = _make_fake_indexer_get(requests.get, recorder, max_feed_items)
    fake_radarr_query = _build_radarr_query(recorder)
    fake_sonarr_query_series = _build_sonarr_query_series(recorder)
    fake_sn_query = _build_sn_query(recorder)
    fake_get_movie = _build_get_movie(recorder)
    fake_find_movie = _build_find_movie(recorder)
    fake_recently_played = _build_get_recently_played(recorder)
    fake_seed_plex_details = _build_seed_plex_details(recorder)
    fake_upsert = _build_upsert(recorder)
    fake_search = _build_search(recorder)
    fake_create = _build_create(recorder)

    patches: List[Any] = [
        # DB engine routed to the in-memory SQLite for the duration of the run.
        patch.object(session_module, "get_engine", lambda: sqlite_engine),
        # Indexer feed: fixture XML.
        patch.object(requests, "get", fake_indexer_get),
        # Radarr.
        patch.object(vid_utils, "radarr_query", fake_radarr_query),
        patch.object(radarr_utils, "radarr_query", fake_radarr_query),
        patch.object(ai_recs, "radarr_query", fake_radarr_query),
        patch.object(vid_utils, "get_movie", fake_get_movie),
        patch.object(radarr_utils, "get_movie", fake_get_movie),
        patch.object(vid_utils, "reset_movies", _noop),
        # Sonarr.
        patch.object(vid_utils, "query_series", fake_sonarr_query_series),
        patch.object(sonarr_utils, "query_series", fake_sonarr_query_series),
        patch.object(ai_recs, "query_series", fake_sonarr_query_series),
        patch.object(sonarr_utils, "sn_query", fake_sn_query),
        patch.object(vid_utils, "reset_series", _noop),
        # TMDB.
        patch.object(ai_recs, "get_movie_id", lambda uid: None),
        patch.object(ai_recs, "get_tv_id", lambda uid: None),
        patch.object(ai_recs, "get_movie_cast", lambda *a, **kw: []),
        patch.object(ai_recs, "get_tv_cast", lambda *a, **kw: []),
        patch.object(ai_recs, "get_movie_release_count", lambda *a, **kw: 0),
        patch.object(vid_utils, "get_tv_id", lambda uid: None),
        patch.object(vid_utils, "get_tv_cast", lambda *a, **kw: []),
        # Plex.
        patch.object(plex_utils, "find_movie", fake_find_movie),
        patch.object(plex_utils, "get_recently_played", fake_recently_played),
        patch.object(vid_utils, "find_movie", fake_find_movie),
        patch.object(
            tool_inspections, "aget_recently_played", _async_wrap(fake_recently_played)
        ),
        # Seeded movies with a ``plex`` key surface as in_library; added items
        # without one surface as missing_from_library.
        patch.object(
            tool_inspections, "aget_plex_details", _async_wrap(fake_seed_plex_details)
        ),
        # Vector search: SQLite can't run a real cosine query, so stub it
        # with seeded fixture items.
        patch.object(tool_searches, "asearch_by_synopsis", fake_search),
        patch.object(ai_recs, "aupsert_item_vector", fake_upsert),
        # Persistence: capture but do not write.
        patch.object(
            IgnoreItem, "create", classmethod(lambda cls, **kw: fake_create(**kw))
        ),
        patch.object(check_feedback, "record_check_result", _noop),
        patch("indexer_utils.vid_utils.record_check_result", _noop),
    ]

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)

        _seed_sqlite(sqlite_engine, recorder)

        try:
            yield recorder
        finally:
            try:
                sqlite_engine.dispose()
            except Exception:
                logger.exception("failed to dispose SQLite engine")
            logger.info(
                "simulation done: created=%d, indexer=%d, radarr=%d, sonarr=%d, "
                "plex=%d, upsert=%d, sqlite_seeded=%s",
                len(recorder.created_items),
                len(recorder.indexer_requests),
                len(recorder.radarr_calls),
                len(recorder.sonarr_calls),
                len(recorder.plex_lookups),
                len(recorder.upsert_calls),
                recorder.seed_summary,
            )
