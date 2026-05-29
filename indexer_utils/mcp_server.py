"""Native MCP server for mediamanager, mounted at ``/mcp``.

Exposes a curated, authenticated tool surface over the same logic the
GraphQL API and React UI use. Authentication is delegated to Authelia
acting as an OIDC provider: the connector obtains a JWT access token
from Authelia and presents it as a bearer token; this server validates
it statelessly against Authelia's JWKS (issuer + audience). nginx must
route ``/mcp`` and the ``/.well-known`` discovery paths to the app
*without* its usual Authelia forward-auth — the JWT is the gate here.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from decouple import config
from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from sqlalchemy import or_, select
from sqlalchemy.orm.attributes import flag_modified

from indexer_utils.ai_recs import (
    annotate_with_ai_async,
    refresh_visible_item_attributes,
)
from indexer_utils.models import (
    IgnoreItem,
    MovieRecommendationRecord,
    RecommendationPreference,
)
from indexer_utils.recommendations import recommend_movie
from indexer_utils.scheduler import list_scheduled_jobs
from indexer_utils.session import db_session
from indexer_utils.sonarr_utils import add_series
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


mcp: FastMCP = FastMCP(name="mediamanager", auth=_build_auth())


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


@mcp.tool
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


@mcp.tool
async def get_candidate(item_id: int) -> Dict[str, Any]:
    """Fetch one candidate by its numeric id, including its full ``ai`` block."""
    async with db_session() as session:
        item = await session.get(IgnoreItem, item_id)
        if item is None:
            raise ValueError(f"No candidate with id {item_id}")
        return _serialize_item(item)


@mcp.tool
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


@mcp.tool
async def scheduled_jobs() -> List[Dict[str, Any]]:
    """List the persistent scheduler jobs and their next run times."""
    return list_scheduled_jobs()


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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

    import asyncio

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
