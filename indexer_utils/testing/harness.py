"""Simulation harness for ``check_movies`` / ``check_shows``.

Goal: let a developer watch the full pipeline run end-to-end against real
OpenAI without touching their real MySQL, real Weaviate index, Radarr, Sonarr,
Plex, TMDB, or the indexer feed.

What's isolated:
- ``IgnoreItem`` storage runs against an in-memory SQLite engine pre-seeded
  with ``fixtures.SEED_MOVIES`` / ``fixtures.SEED_SHOWS``.
- Weaviate vector search uses dedicated ``IgnoreItemMV_sim`` /
  ``IgnoreItemTV_sim`` classes seeded with the same items + synopses, so
  ``search_similar_by_synopsis`` returns plausible neighbors.
- ``IgnoreItem.create`` writes are recorded but not persisted.
- ``record_check_result`` is no-op'd to skip Redis writes.

What stays real:
- OpenAI (chat completions for synopsis + the agent loop).
- Weaviate vectorizer (the ``_sim`` classes use ``text2vec-openai``).

Pre-flight: bring Weaviate up (``docker compose up -d weaviate``) and export
``WEAVIATE_HOST=localhost``. If Weaviate is unreachable the harness logs a
warning and continues without seeding it; the agent will see errors from
``search_similar_by_synopsis`` and pivot to other tools.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from . import fixtures as fx

logger = logging.getLogger(__name__)


SIM_CLASS_SUFFIX = "_sim"


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
    weaviate_seed_summary: Dict[str, int] = field(default_factory=dict)
    weaviate_error: Optional[str] = None


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
        return None

    return _fake


def _build_get_recently_played(recorder: SimulationRecorder) -> Callable:
    def _fake(limit: int = 40) -> List[Dict[str, Any]]:
        recorder.plex_lookups.append({"call": "recently_played", "limit": limit})
        return []

    return _fake


def _build_upsert(recorder: SimulationRecorder) -> Callable:
    def _fake(
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
        ai = dict(attrs.get("ai") or {})
        ai.setdefault("weaviate_uuid", str(uuid4()))
        attrs["ai"] = ai
        return attrs

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
            ai["weaviate_uuid"] = ai.get("weaviate_uuid") or _seed_uuid(
                "mv", movie["uid"]
            )
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
            ai["weaviate_uuid"] = ai.get("weaviate_uuid") or _seed_uuid(
                "tv", show["uid"]
            )
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


def _seed_uuid(item_type: str, uid: str) -> str:
    """Deterministic uuid per seed item so re-seeding Weaviate is idempotent."""
    import hashlib

    digest = hashlib.sha1(f"sim:{item_type}:{uid}".encode()).hexdigest()
    # Format as a uuid-like string
    return (
        f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


# ---------------------------------------------------------------------------
# Weaviate _sim class seed
# ---------------------------------------------------------------------------


def _ensure_sim_classes(client: Any, embedding_model: str) -> None:
    """Drop and re-create ``IgnoreItemMV_sim`` and ``IgnoreItemTV_sim`` so
    each run sees clean, deterministic neighbors. Class shape mirrors
    ``weaviate_init_schema.py``."""
    from weaviate.classes.config import Configure, DataType, Property

    for base in ("IgnoreItemMV", "IgnoreItemTV"):
        sim = base + SIM_CLASS_SUFFIX
        if client.collections.exists(sim):
            client.collections.delete(sim)
        client.collections.create(
            sim,
            description="Simulation seed: IgnoreItem vectors",
            properties=[
                Property(name="uid", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="title", data_type=DataType.TEXT),
                Property(name="type", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="synopsis", data_type=DataType.TEXT),
            ],
            vector_config=Configure.Vectors.text2vec_openai(
                name="default",
                model=embedding_model,
                source_properties=["title", "synopsis"],
            ),
        )


def _seed_weaviate(client: Any, recorder: SimulationRecorder) -> None:
    counts = {"mv": 0, "tv": 0}
    coll_mv = client.collections.get("IgnoreItemMV" + SIM_CLASS_SUFFIX)
    for movie in fx.SEED_MOVIES:
        coll_mv.data.insert(
            properties={
                "uid": movie["uid"],
                "title": movie["title"],
                "type": "mv",
                "synopsis": movie["synopsis"],
            },
            uuid=_seed_uuid("mv", movie["uid"]),
        )
        counts["mv"] += 1
    coll_tv = client.collections.get("IgnoreItemTV" + SIM_CLASS_SUFFIX)
    for show in fx.SEED_SHOWS:
        coll_tv.data.insert(
            properties={
                "uid": show["uid"],
                "title": show["title"],
                "type": "tv",
                "synopsis": show["synopsis"],
            },
            uuid=_seed_uuid("tv", show["uid"]),
        )
        counts["tv"] += 1
    recorder.weaviate_seed_summary = counts
    logger.info("seeded weaviate %s: %s", SIM_CLASS_SUFFIX, counts)


def _setup_weaviate_sim(recorder: SimulationRecorder) -> None:
    """Connect to Weaviate, create _sim classes, seed, and close. Errors are
    captured into ``recorder.weaviate_error`` so the simulation still runs
    when Weaviate is unavailable."""
    from decouple import config

    from indexer_utils.weaviate_client import get_weaviate_client

    embedding_model = config("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")
    try:
        client = get_weaviate_client()
    except Exception as exc:
        recorder.weaviate_error = f"connect failed: {exc}"
        logger.warning(
            "Weaviate unavailable (%s); _sim classes not seeded. "
            "search_similar_by_synopsis will return errors.",
            exc,
        )
        return

    try:
        _ensure_sim_classes(client, embedding_model)
        _seed_weaviate(client, recorder)
    except Exception as exc:
        recorder.weaviate_error = f"seed failed: {exc}"
        logger.exception("Weaviate _sim seeding failed")
    finally:
        try:
            client.close()
        except Exception:
            pass


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
        weaviate_client,
    )
    from indexer_utils import (
        session as session_module,
    )
    from indexer_utils.ai_tools import registry as tool_registry
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
    fake_upsert = _build_upsert(recorder)
    fake_create = _build_create(recorder)

    # Map ``mv``/``tv`` to the simulation Weaviate classes. Calls in
    # weaviate_client.py look up _class_name dynamically, so this propagates.
    def fake_class_name(item_type: str) -> str:
        base = "IgnoreItemMV" if item_type == "mv" else "IgnoreItemTV"
        return base + SIM_CLASS_SUFFIX

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
            tool_registry, "aget_recently_played", _async_wrap(fake_recently_played)
        ),
        patch.object(
            tool_registry, "aget_plex_details", _async_wrap(lambda *a, **kw: None)
        ),
        # Weaviate: read-only against _sim classes; do not touch real index.
        patch.object(weaviate_client, "_class_name", fake_class_name),
        patch.object(ai_recs, "upsert_item_attrs", fake_upsert),
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
        _setup_weaviate_sim(recorder)

        try:
            yield recorder
        finally:
            try:
                sqlite_engine.dispose()
            except Exception:
                logger.exception("failed to dispose SQLite engine")
            logger.info(
                "simulation done: created=%d, indexer=%d, radarr=%d, sonarr=%d, "
                "plex=%d, upsert=%d, sqlite_seeded=%s, weaviate_seeded=%s, "
                "weaviate_error=%s",
                len(recorder.created_items),
                len(recorder.indexer_requests),
                len(recorder.radarr_calls),
                len(recorder.sonarr_calls),
                len(recorder.plex_lookups),
                len(recorder.upsert_calls),
                recorder.seed_summary,
                recorder.weaviate_seed_summary,
                recorder.weaviate_error or "-",
            )
