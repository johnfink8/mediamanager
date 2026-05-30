"""Native MCP server for mediamanager, mounted at ``/mcp``.

Exposes a curated, authenticated tool surface over the same logic the
GraphQL API and React UI use. Authentication is delegated to Authelia
acting as an OIDC provider: the connector obtains a JWT access token
from Authelia and presents it as a bearer token; this server validates
it statelessly against Authelia's JWKS (issuer + audience). nginx must
route ``/mcp`` and the ``/.well-known`` discovery paths to the app
*without* its usual Authelia forward-auth — the JWT is the gate here.
"""

import asyncio
import functools
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from decouple import config
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from sqlalchemy import or_, select
from sqlalchemy.orm.attributes import flag_modified

from indexer_utils.ai_recs import (
    annotate_with_ai_async,
    refresh_visible_item_attributes,
)
from indexer_utils.compat import assess_compatibility
from indexer_utils.models import (
    IgnoreItem,
    MovieRecommendationRecord,
    RecommendationPreference,
)
from indexer_utils.plex_utils import (
    anow_playing,
    aresolve_item,
    asearch_videos,
)
from indexer_utils.radarr_utils import (
    aget_movie,
    aradarr_query,
    aredownload_by_imdb,
    aupgrade_by_imdb,
    aupgrade_movie,
)
from indexer_utils.recommendations import recommend_movie
from indexer_utils.scheduler import list_scheduled_jobs
from indexer_utils.session import db_session
from indexer_utils.sonarr_utils import (
    add_series,
    aget_series,
    aredownload_episode,
    aregrab_episode,
    asn_query,
    aupgrade_by_tvdb,
    aupgrade_series,
)
from indexer_utils.vdiag_client import (
    VdiagError,
    aget_job,
    aprobe,
    astart_remux,
    astart_scan,
)
from indexer_utils.vid_utils import addMovie

logger = logging.getLogger(__name__)

# Discovery/identity coordinates. Defaults match the production deployment;
# override per-environment via .env. MCP_AUTH_DISABLED bypasses auth for
# local dev/tests where there's no Authelia in front.
OIDC_ISSUER: str = config("MCP_OIDC_ISSUER", default="https://auth.home.finkdev.com")
OIDC_JWKS_URI: str = config(
    "MCP_OIDC_JWKS_URI", default="https://auth.home.finkdev.com/jwks.json"
)
MCP_RESOURCE_URL: str = config(
    "MCP_RESOURCE_URL", default="https://monitor.home.finkdev.com/mcp"
)
MCP_BASE_URL: str = config("MCP_BASE_URL", default="https://monitor.home.finkdev.com")
MCP_AUTH_DISABLED: bool = config("MCP_AUTH_DISABLED", default=False, cast=bool)


# RFC 9728 Protected Resource Metadata. We serve this ourselves (see main.py)
# rather than letting FastMCP derive it: under an ASGI sub-mount FastMCP
# advertises the wrong ``resource`` and an unreachable metadata path (see
# upstream issue #1348). ``resource`` MUST equal the JWTVerifier audience and
# the ``aud`` Authelia stamps into the token, or validation silently fails.
PROTECTED_RESOURCE_METADATA: Dict[str, Any] = {
    "resource": MCP_RESOURCE_URL,
    "authorization_servers": [OIDC_ISSUER],
    "bearer_methods_supported": ["header"],
    "scopes_supported": [],
}


def _build_auth() -> Optional[RemoteAuthProvider]:
    if MCP_AUTH_DISABLED:
        logger.warning("MCP auth disabled — /mcp is unauthenticated (dev only)")
        return None
    verifier = JWTVerifier(
        jwks_uri=OIDC_JWKS_URI,
        issuer=OIDC_ISSUER,
        audience=MCP_RESOURCE_URL,
    )
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(OIDC_ISSUER)],
        base_url=MCP_BASE_URL,
    )


# mask_error_details=True hides incidental exceptions (a bug, an unreachable
# upstream, a malformed response) behind a generic message — they only get
# logged server-side, never leaked to the model. Intentional, actionable
# failures are surfaced explicitly via @safe_tool (below), which raises
# ToolError; FastMCP forwards ToolError messages verbatim regardless of masking.
mcp: FastMCP = FastMCP(name="mediamanager", auth=_build_auth(), mask_error_details=True)


def safe_tool(fn: Callable[..., Awaitable[Any]]) -> Any:
    """Register an MCP tool that turns expected failures into clean model output.

    A tool (or a helper it calls) signals an actionable negative result with
    ``ValueError`` (not found, bad input, nothing to do) or ``VdiagError`` (the
    sidecar reported a problem). We convert those to ``ToolError`` so the model
    sees the real message even with masking on; anything else propagates and is
    masked as an internal error.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except (ValueError, VdiagError) as exc:
            raise ToolError(str(exc)) from exc

    return mcp.tool(wrapper)


def _serialize_item(item: IgnoreItem) -> Dict[str, Any]:
    """Flatten an IgnoreItem for an LLM consumer.

    Returns the raw ``ai`` verdict block as-is — the model reads the
    score/reason/synopsis itself rather than us pre-digesting it.
    """
    attrs = item.attributes or {}
    return {
        "id": item.id,
        "item_type": item.item_type,
        "uid": item.uid,
        "title": item.title,
        "added": item.added,
        "ignored": item.ignore,
        "poster_url": item.poster_url,
        "created_at": item.created_at,
        "ai": attrs.get("ai") or {},
    }


# --------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------


@safe_tool
async def list_open_candidates(item_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """List undecided candidates (not yet added or ignored, not deferred).

    item_type: "mv" for movies, "tv" for shows, or omit for both. Items
    are returned scored-first (highest AI score first), then oldest.
    """
    async with db_session() as session:
        stmt = select(IgnoreItem).where(
            IgnoreItem.ignore.is_(False),
            or_(
                IgnoreItem.defer_until.is_(None),
                IgnoreItem.defer_until <= datetime.utcnow(),
            ),
        )
        if item_type:
            stmt = stmt.where(IgnoreItem.item_type == item_type)
        items = list((await session.execute(stmt)).scalars())

    def sort_key(item: IgnoreItem) -> "tuple[int, float, float]":
        score = ((item.attributes or {}).get("ai") or {}).get("score")
        score_val = float(score) if isinstance(score, (int, float)) else None
        return (
            0 if score_val is not None else 1,
            -(score_val or 0.0),
            float(item.created_at or float("inf")),
        )

    items.sort(key=sort_key)
    return [_serialize_item(item) for item in items]


@safe_tool
async def get_candidate(item_id: int) -> Dict[str, Any]:
    """Fetch one candidate by its numeric id, including its full ``ai`` block."""
    async with db_session() as session:
        item = await session.get(IgnoreItem, item_id)
        if item is None:
            raise ValueError(f"No candidate with id {item_id}")
        return _serialize_item(item)


@safe_tool
async def list_decided(
    item_type: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    search: Optional[str] = None,
) -> Dict[str, Any]:
    """List already-decided (ignored/added) items, newest first.

    Supports a title substring ``search`` and ``limit``/``offset`` paging.
    """
    async with db_session() as session:
        query = select(IgnoreItem).where(IgnoreItem.ignore.is_(True))
        if item_type:
            query = query.where(IgnoreItem.item_type == item_type)
        if search and search.strip():
            pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    IgnoreItem.title.ilike(pattern),
                    IgnoreItem.checked_title.ilike(pattern),
                )
            )
        query = query.order_by(IgnoreItem.created_at.desc(), IgnoreItem.id.desc())
        items = list(
            (await session.execute(query.offset(offset).limit(limit))).scalars()
        )
    return {
        "items": [_serialize_item(item) for item in items],
        "limit": limit,
        "offset": offset,
    }


@safe_tool
async def scheduled_jobs() -> List[Dict[str, Any]]:
    """List the persistent scheduler jobs and their next run times."""
    return list_scheduled_jobs()


@safe_tool
async def recommend(prompt: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Run the recommendation agent and return a single movie suggestion.

    Expensive: this drives the full agentic pipeline (web search, vector
    queries, several LLM turns). Use sparingly. ``prompt`` optionally
    steers the suggestion (e.g. "something tense and recent").
    """
    result = await recommend_movie(prompt)
    if result is None:
        return None
    return {
        "record_id": result.record_id,
        "imdb_id": result.imdb_id,
        "title": result.title,
        "year": result.year,
        "overview": result.overview,
        "genres": result.genres,
        "cast": result.cast,
        "reason": result.reason,
        "source": result.source,
    }


# --------------------------------------------------------------------------
# Write tools
# --------------------------------------------------------------------------


@safe_tool
async def add_item(item_id: int) -> Dict[str, Any]:
    """Add a candidate to the library (Radarr for movies, Sonarr for shows).

    Marks it added + ignored so it leaves the open list. Irreversible from
    here — it hands off to the downloader.
    """
    async with db_session() as session:
        item = await session.get(IgnoreItem, item_id)
        if item is None:
            raise ValueError(f"No candidate with id {item_id}")
        if item.item_type == "mv":
            addMovie(item.uid)
        else:
            add_series(item.uid)
        item.added = True
        item.ignore = True
        session.add(item)
        await session.commit()
        return _serialize_item(item)


@safe_tool
async def ignore_item(item_id: int) -> Dict[str, Any]:
    """Dismiss a candidate (mark ignored) so it drops off the open list."""
    async with db_session() as session:
        item = await session.get(IgnoreItem, item_id)
        if item is None:
            raise ValueError(f"No candidate with id {item_id}")
        item.ignore = True
        session.add(item)
        await session.commit()
        return _serialize_item(item)


@safe_tool
async def retry_ai(item_id: int) -> Dict[str, Any]:
    """Re-run the recommendation agent on a candidate and store the verdict.

    Spends OpenAI tokens. Reads a snapshot, runs the agent without holding
    a DB connection across the await, then writes the refreshed ``ai`` block.
    """
    async with db_session() as session:
        item = await session.get(IgnoreItem, item_id)
        if item is None:
            raise ValueError(f"No candidate with id {item_id}")
        item_type = item.item_type
        uid = item.uid
        title = item.title
        attrs = dict(item.attributes or {})
        attrs.pop("ai", None)
        attrs.pop("_synopsis_vector_tmp", None)

    refreshed = await annotate_with_ai_async(item_type, uid, title, attrs)

    async with db_session() as session:
        item = await session.get(IgnoreItem, item_id)
        if item is None:
            raise ValueError(f"Candidate {item_id} vanished mid-retry")
        item.attributes = refreshed
        flag_modified(item, "attributes")
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return _serialize_item(item)


@safe_tool
async def refresh_item(item_id: int) -> Dict[str, Any]:
    """Re-fetch ratings/metadata from Radarr/Sonarr for one candidate, then re-annotate.

    Unlike retry_ai (which re-verdicts on the existing data), this first
    refreshes the item's ratings and metadata from the source, then re-runs
    the agent. The single-item counterpart to recheck_visible. Spends tokens.
    """
    async with db_session() as session:
        item = await session.get(IgnoreItem, item_id)
        if item is None:
            raise ValueError(f"No candidate with id {item_id}")
        attrs = refresh_visible_item_attributes(item)
        item_type = item.item_type
        uid = item.uid
        title = item.title
    attrs.pop("ai", None)
    attrs.pop("_synopsis_vector_tmp", None)

    refreshed = await annotate_with_ai_async(item_type, uid, title, attrs)

    async with db_session() as session:
        item = await session.get(IgnoreItem, item_id)
        if item is None:
            raise ValueError(f"Candidate {item_id} vanished mid-refresh")
        item.attributes = refreshed
        flag_modified(item, "attributes")
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return _serialize_item(item)


@safe_tool
async def set_recommendation_preference(
    recommendation_id: int,
    preference: Literal["LIKE", "NOT_NOW", "NEVER"],
) -> Dict[str, Any]:
    """Record feedback on a past recommendation (LIKE / NOT_NOW / NEVER).

    ``recommendation_id`` is the ``record_id`` returned by ``recommend``.
    This feedback feeds back into future recommendation taste.
    """
    async with db_session() as session:
        record = await session.get(MovieRecommendationRecord, recommendation_id)
        if record is None:
            raise ValueError(f"No recommendation with id {recommendation_id}")
        record.preference = RecommendationPreference[preference]
        record.updated_at = int(datetime.utcnow().timestamp())
        session.add(record)
        await session.commit()
        return {"id": record.id, "preference": preference}


@safe_tool
async def recheck_visible(item_type: str) -> Dict[str, Any]:
    """Re-fetch ratings/metadata and re-run the agent for every open item of a type.

    Heavy: refreshes external details and re-annotates the whole visible
    list for ``item_type`` ("mv" or "tv"). Returns how many were refreshed.
    """
    async with db_session() as session:
        now = datetime.utcnow()
        items = list(
            (
                await session.execute(
                    select(IgnoreItem).where(
                        IgnoreItem.item_type == item_type,
                        IgnoreItem.ignore.is_(False),
                        or_(
                            IgnoreItem.defer_until.is_(None),
                            IgnoreItem.defer_until <= now,
                        ),
                    )
                )
            ).scalars()
        )
        prepared: List[Dict[str, Any]] = []
        for item in items:
            attrs = refresh_visible_item_attributes(item)
            attrs.pop("ai", None)
            attrs.pop("_synopsis_vector_tmp", None)
            prepared.append(
                {
                    "id": item.id,
                    "item_type": item.item_type,
                    "uid": item.uid,
                    "title": item.title,
                    "attrs": attrs,
                }
            )

    if not prepared:
        return {"item_type": item_type, "rechecked": 0}

    from indexer_utils.vid_utils import AI_ANNOTATE_CONCURRENCY

    semaphore = asyncio.Semaphore(AI_ANNOTATE_CONCURRENCY)

    async def _annotate(p: Dict[str, Any]) -> Dict[str, Any]:
        async with semaphore:
            return await annotate_with_ai_async(
                p["item_type"], p["uid"], p["title"], p["attrs"]
            )

    results = await asyncio.gather(
        *(_annotate(p) for p in prepared), return_exceptions=True
    )

    rechecked = 0
    async with db_session() as session:
        for p, outcome in zip(prepared, results):
            if isinstance(outcome, BaseException):
                logger.exception("recheck annotate failed for %s", p["uid"])
                continue
            row = await session.get(IgnoreItem, p["id"])
            if row is None:
                continue
            row.attributes = outcome
            flag_modified(row, "attributes")
            session.add(row)
            rechecked += 1
        await session.commit()

    return {"item_type": item_type, "rechecked": rechecked}


# --------------------------------------------------------------------------
# Radarr / Sonarr direct proxy
# --------------------------------------------------------------------------


@safe_tool
async def radarr_find(term: str) -> List[Dict[str, Any]]:
    """Search for movies to add (Radarr title lookup).

    Returns candidates with title, year, imdb/tmdb ids, and whether each is
    already in the library. Pass the imdb_id to radarr_add_movie.
    """
    results: Any = await aradarr_query("movie/lookup", term=term)
    return [
        {
            "title": m.get("title"),
            "year": m.get("year"),
            "imdb_id": m.get("imdbId"),
            "tmdb_id": m.get("tmdbId"),
            "in_library": bool(m.get("id")),
            "overview": m.get("overview"),
        }
        for m in results[:20]
    ]


@safe_tool
async def radarr_movies(query: Optional[str] = None) -> List[Dict[str, Any]]:
    """List movies in the Radarr library, optionally filtered by title substring.

    Returns the Radarr movie `id` (needed for radarr_upgrade_movie), quality
    profile, and whether a file is present.
    """
    movies: Any = await aradarr_query("movie")
    if query:
        q = query.lower()
        movies = [m for m in movies if q in (m.get("title") or "").lower()]
    return [
        {
            "id": m.get("id"),
            "title": m.get("title"),
            "year": m.get("year"),
            "quality_profile_id": m.get("qualityProfileId"),
            "has_file": m.get("hasFile"),
            "monitored": m.get("monitored"),
        }
        for m in movies[:50]
    ]


@safe_tool
async def radarr_quality_profiles() -> List[Dict[str, Any]]:
    """List Radarr quality profiles (id + name) — e.g. to find the '1080p' profile id."""
    profiles: Any = await aradarr_query("qualityprofile")
    return [{"id": p.get("id"), "name": p.get("name")} for p in profiles]


@safe_tool
async def radarr_add_movie(imdb_id: str) -> Dict[str, Any]:
    """Add a movie to Radarr by IMDb id (monitored) and trigger a search.

    To pull it in a specific quality, follow with radarr_upgrade_movie.
    """
    await asyncio.to_thread(addMovie, imdb_id)
    return {"imdb_id": imdb_id, "status": "added"}


@safe_tool
async def radarr_upgrade_movie(
    movie_id: int, quality_profile_id: Optional[int] = None
) -> Dict[str, Any]:
    """Re-grab a movie, optionally at a new quality ("get this one in 1080p").

    Pass quality_profile_id (from radarr_quality_profiles) to switch profiles;
    omit it to just re-search at the current quality. Triggers a Radarr search.
    """
    return await aupgrade_movie(movie_id, quality_profile_id)


@safe_tool
async def sonarr_find(term: str) -> List[Dict[str, Any]]:
    """Search for series to add (Sonarr title lookup). Pass tvdb_id to sonarr_add_series."""
    results: Any = await asn_query("series/lookup", term=term)
    return [
        {
            "title": s.get("title"),
            "year": s.get("year"),
            "tvdb_id": s.get("tvdbId"),
            "in_library": bool(s.get("id")),
            "overview": s.get("overview"),
        }
        for s in results[:20]
    ]


@safe_tool
async def sonarr_series(query: Optional[str] = None) -> List[Dict[str, Any]]:
    """List series in the Sonarr library, optionally filtered by title.

    Returns the Sonarr series `id` (needed for sonarr_episodes).
    """
    series: Any = await asn_query("series")
    if query:
        q = query.lower()
        series = [s for s in series if q in (s.get("title") or "").lower()]
    return [
        {
            "id": s.get("id"),
            "title": s.get("title"),
            "year": s.get("year"),
            "quality_profile_id": s.get("qualityProfileId"),
            "monitored": s.get("monitored"),
        }
        for s in series[:50]
    ]


@safe_tool
async def sonarr_quality_profiles() -> List[Dict[str, Any]]:
    """List Sonarr quality profiles (id + name)."""
    profiles: Any = await asn_query("qualityprofile")
    return [{"id": p.get("id"), "name": p.get("name")} for p in profiles]


@safe_tool
async def sonarr_add_series(tvdb_id: str) -> Dict[str, Any]:
    """Add a series to Sonarr by TVDB id (all seasons monitored) and search for episodes."""
    await asyncio.to_thread(add_series, tvdb_id)
    return {"tvdb_id": tvdb_id, "status": "added"}


@safe_tool
async def sonarr_episodes(
    series_id: int, season: Optional[int] = None
) -> List[Dict[str, Any]]:
    """List a series' episodes (id, season/episode number, title, file status).

    Use this to find the episode_id for sonarr_regrab_episode. Optionally filter
    by season number.
    """
    eps: Any = await asn_query("episode", seriesId=series_id)
    out: List[Dict[str, Any]] = []
    for e in eps:
        if season is not None and e.get("seasonNumber") != season:
            continue
        out.append(
            {
                "id": e.get("id"),
                "season": e.get("seasonNumber"),
                "episode": e.get("episodeNumber"),
                "title": e.get("title"),
                "has_file": e.get("hasFile"),
            }
        )
    return out


@safe_tool
async def sonarr_regrab_episode(
    episode_id: int, replace_file: bool = True
) -> Dict[str, Any]:
    """Fetch a fresh copy of one episode ("this episode won't play, get a new copy").

    By default deletes the existing file first so Sonarr grabs a replacement even
    when the current file already meets the quality cutoff, then triggers a
    search. Set replace_file=False to only search for an upgrade without deleting.
    """
    return await aregrab_episode(episode_id, replace_file=replace_file)


@safe_tool
async def sonarr_upgrade_series(
    series_id: int,
    quality_profile_id: Optional[int] = None,
    episode_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Upgrade a series' quality ("get this show in 1080p") — the Sonarr analogue
    of radarr_upgrade_movie.

    Pass quality_profile_id (from sonarr_quality_profiles) to switch the series'
    profile; omit it to just re-search at the current quality. Quality in Sonarr
    is per-series, so pass episode_id to pull only that episode at the new
    quality, otherwise the whole series is searched.
    """
    return await aupgrade_series(series_id, quality_profile_id, episode_id)


# --------------------------------------------------------------------------
# Video diagnostics & repair
#
# locate -> diagnose -> repair. Paths come from Plex (the reliable source) and
# are inspected/repaired by the vdiag ffmpeg sidecar; the model never supplies a
# filesystem path, only a Plex ratingKey.
# --------------------------------------------------------------------------


@safe_tool
async def locate_video(
    title: str,
    item_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Find a movie or episode in Plex and its on-disk file ("X is freezing").

    item_type is "mv" for a movie or "tv" for an episode; for episodes pass
    season + episode to narrow it down. Returns candidates each with a
    plex_rating_key — hand that to diagnose_video / repair_video.
    """
    return await asearch_videos(title, item_type, season, episode)


@safe_tool
async def now_playing() -> List[Dict[str, Any]]:
    """List what's currently playing in Plex ("the show I'm watching now is freezing").

    Returns each active stream with a plex_rating_key (for diagnose_video /
    repair_video) plus user/player/state so you can tell whose stream is which
    when several are live.
    """
    return await anow_playing()


@safe_tool
async def diagnose_video(plex_rating_key: str, deep: bool = False) -> Dict[str, Any]:
    """Inspect a located video file for the corruption that causes freezing.

    Quick by default (ffprobe header/stream check, returns immediately). Set
    deep=True to also launch a full decode scan (ffmpeg reads the whole file,
    minutes) that surfaces frame errors a header probe misses — use it when
    playback freezes/stutters. The deep scan runs in the background: the result
    carries a scan_job_id; poll get_video_job(job_id) for its progress/outcome.
    """
    item = await aresolve_item(plex_rating_key)
    if not item:
        raise ValueError(f"no Plex item for ratingKey {plex_rating_key}")
    # vdiag re-resolves the path from the ratingKey itself; we pass the key, not
    # a path. aresolve_item here is just for the item_type/title context.
    result: Dict[str, Any] = {
        "plex_rating_key": plex_rating_key,
        "item_type": item.get("item_type"),
        "title": item.get("title"),
        "probe": await aprobe(plex_rating_key),
    }
    if deep:
        job = await astart_scan(plex_rating_key)
        result["scan_job_id"] = job.get("job_id")
        result["note"] = "deep scan running in the background; poll get_video_job"
    return result


@safe_tool
async def get_video_job(job_id: str) -> Dict[str, Any]:
    """Poll a background scan/remux job started by diagnose_video or repair_video.

    Returns status ("running" | "done" | "error"), a progress percentage while
    running, and on completion the result (scan findings, or remux before/after)
    or an error message. Jobs expire after a day.
    """
    job = await aget_job(job_id)
    if job is None:
        raise ValueError(f"no vdiag job {job_id} (unknown or expired)")
    return job


@safe_tool
async def check_compatibility(
    plex_rating_key: str, client: str = "appletv"
) -> Dict[str, Any]:
    """Check whether a video direct-plays or must transcode on your client.

    Defaults to Apple TV. Probes the file's container + video/audio codecs and
    flags the usual transcode triggers (DTS audio, VP9/AV1 video, 4K H.264) —
    a file forced to transcode is a common cause of freezing/buffering on a busy
    server or weak network, distinct from on-disk corruption (use diagnose_video
    for that).
    """
    probe = await aprobe(plex_rating_key)
    return {"plex_rating_key": plex_rating_key, **assess_compatibility(probe, client)}


@safe_tool
async def repair_video(plex_rating_key: str, mode: str) -> Dict[str, Any]:
    """Repair a freezing video, either in place or by re-downloading.

    mode="remux" losslessly rebuilds the container in place (fixes a broken
    index/interleave without re-downloading) and refreshes Plex when done.
    Because a remux can take minutes it runs in the background: the result carries
    a job_id; poll get_video_job(job_id) for progress/outcome. mode="redownload"
    deletes the file and triggers a fresh Radarr/Sonarr grab (returns immediately).
    Try remux first; fall back to redownload if the diagnosis shows real corruption.
    """
    item = await aresolve_item(plex_rating_key)
    if not item:
        raise ValueError(f"no Plex item for ratingKey {plex_rating_key}")

    if mode == "remux":
        job = await astart_remux(plex_rating_key)
        return {
            "mode": "remux",
            "title": item.get("title"),
            "job_id": job.get("job_id"),
            "status": job.get("status"),
            "note": "remux running in the background; poll get_video_job",
        }

    if mode == "redownload":
        if item.get("item_type") == "mv":
            imdb_id = item.get("imdb_id")
            if not imdb_id:
                raise ValueError(
                    "no imdb id on the Plex movie to drive a Radarr re-grab"
                )
            outcome = await aredownload_by_imdb(imdb_id)
        else:
            tvdb_id = item.get("tvdb_id")
            season = item.get("season")
            episode = item.get("episode")
            if not (tvdb_id and season is not None and episode is not None):
                raise ValueError(
                    "missing tvdb id / season / episode for a Sonarr re-grab"
                )
            outcome = await aredownload_episode(tvdb_id, season, episode)
        return {"mode": "redownload", "title": item.get("title"), **outcome}

    raise ValueError(f"unknown repair mode {mode!r}; use 'remux' or 'redownload'")


@safe_tool
async def upgrade_video(
    plex_rating_key: str, quality_profile_id: Optional[int] = None
) -> Dict[str, Any]:
    """Upgrade what you're watching to a better quality ("get this in 1080p").

    Movies and shows use different quality-profile sets, so call once with just
    the ratingKey to see this item's available profiles + its current one, then
    call again with the chosen quality_profile_id to switch and re-grab. For an
    episode the new quality is applied to the series and that episode is searched.
    """
    item = await aresolve_item(plex_rating_key)
    if not item:
        raise ValueError(f"no Plex item for ratingKey {plex_rating_key}")

    if item["item_type"] == "mv":
        imdb_id = item.get("imdb_id")
        if not imdb_id:
            raise ValueError("no imdb id on the Plex movie to drive a Radarr upgrade")
        if quality_profile_id is None:
            movie: Any = await aget_movie(imdb_id)
            profiles: Any = await aradarr_query("qualityprofile")
            return {
                "item_type": "mv",
                "title": item.get("title"),
                "current_quality_profile_id": (movie or {}).get("qualityProfileId"),
                "available_profiles": [
                    {"id": p.get("id"), "name": p.get("name")} for p in profiles
                ],
            }
        outcome = await aupgrade_by_imdb(imdb_id, quality_profile_id)
        return {"item_type": "mv", "title": item.get("title"), **outcome}

    tvdb_id = item.get("tvdb_id")
    if not tvdb_id:
        raise ValueError("no tvdb id on the Plex show to drive a Sonarr upgrade")
    if quality_profile_id is None:
        series: Any = await aget_series(int(tvdb_id))
        sn_profiles: Any = await asn_query("qualityprofile")
        return {
            "item_type": "tv",
            "title": item.get("title"),
            "current_quality_profile_id": (series or {}).get("qualityProfileId"),
            "available_profiles": [
                {"id": p.get("id"), "name": p.get("name")} for p in sn_profiles
            ],
        }
    outcome = await aupgrade_by_tvdb(
        tvdb_id, quality_profile_id, item.get("season"), item.get("episode")
    )
    return {"item_type": "tv", "title": item.get("title"), **outcome}
