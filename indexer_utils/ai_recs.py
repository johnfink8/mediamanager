import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from decouple import config
from openai import AsyncOpenAI, OpenAI

from indexer_utils.tmdb import (
    get_movie_cast,
    get_movie_director,
    get_movie_id,
    get_movie_release_count,
    get_tv_cast,
    get_tv_id,
)

from .ai_tools import REGISTRY, AgentRunResult, ToolContext, run_agent
from .log import item_context
from .models import IgnoreItem
from .radarr_utils import radarr_query
from .sonarr_utils import query_series
from .weaviate_client import upsert_item_attrs

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
PROMPTS_DIR = BASE_DIR / "prompts"
OPENAI_MODEL = config("OPENAI_MODEL", default="gpt-5.5")

AGENT_MAX_TURNS = int(config("AI_AGENT_MAX_TURNS", default=6))
AGENT_MAX_TOOL_CALLS = int(config("AI_AGENT_MAX_TOOL_CALLS", default=16))


def load_prompt(filename: str) -> str:
    with open(PROMPTS_DIR / filename, "r") as f:
        return f.read()


SYNOPSIS_PROMPTS = {
    "mv": """
    You are a helpful assistant that writes a brief synopsis for a movie.
    The synopsis should briefly describe the story type, mention main star(s) if known,
    and indicate if the item is part of or a sequel to an existing property.
    Return strict JSON with the single field: synopsis (1-3 sentences).
    """,
    "tv": """
    You are a helpful assistant that writes a brief synopsis for a TV show.
    The synopsis should briefly describe the story type, mention main star(s) if known,
    mention the main genre, network, country of origin, and language.
    Return strict JSON with the single field: synopsis (1-3 sentences).
    """,
}

RECOMMENDATION_PROMPTS = {
    "mv": load_prompt("mv_recommendation.md"),
    "tv": load_prompt("tv_recommendation.md"),
}

_openai_client: Optional[OpenAI] = None
# NOTE: AsyncOpenAI is intentionally NOT cached. httpx transports are bound
# to whichever event loop opened them, and this module is called from both
# the FastAPI loop (mutations) and short-lived loops created by
# ``asyncio.run`` inside scheduled-job threads. A global cache crossed the
# streams and fired ``Event loop is closed`` at runtime.


def _to_list_of_str(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict):
        return [str(v) for v in value.values()]
    return [str(value)]


def _year_from_attrs(attrs: Dict[str, Any]) -> Optional[int]:
    try:
        y = attrs.get("year")
        if isinstance(y, list) and y:
            return int(str(y[0]))
        if isinstance(y, (str, int)):
            return int(str(y))
    except Exception:
        return None
    return None


def ensure_movie_release_count(
    attrs: Dict[str, Any], uid: str
) -> Tuple[Dict[str, Any], Optional[int], bool]:
    ai = attrs.get("ai")
    if not isinstance(ai, dict):
        ai = {}
    if "release_count" in ai:
        return attrs, ai["release_count"], False
    tmdb_id = attrs.get("tmdb_id") or ai.get("tmdb_id")
    if not tmdb_id:
        tmdb_id = get_movie_id(uid)
        if tmdb_id:
            attrs["tmdb_id"] = tmdb_id
    if not tmdb_id:
        release_count = None
    else:
        try:
            release_count = get_movie_release_count(tmdb_id)
        except Exception:
            logger.exception(
                "Failed to fetch movie release count", extra={"tmdb_id": tmdb_id}
            )
            release_count = None
    ai["release_count"] = release_count
    attrs["ai"] = ai
    return attrs, release_count, True


def get_openai_client() -> Optional[OpenAI]:
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    try:
        api_key = config("OPENAI_API_KEY")
        _openai_client = OpenAI(api_key=api_key)
        return _openai_client
    except Exception:
        logger.exception("Failed to initialize OpenAI client")
        return None


def make_async_openai_client() -> Optional[AsyncOpenAI]:
    """Build a fresh AsyncOpenAI client tied to the current event loop.

    Callers are responsible for closing it (use ``async with`` or
    ``await client.close()`` in a ``finally`` block).
    """
    try:
        api_key = config("OPENAI_API_KEY")
        return AsyncOpenAI(api_key=api_key)
    except Exception:
        logger.exception("Failed to initialize async OpenAI client")
        return None


def call_openai_json(
    system_prompt: str, user_prompt: str
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    client = get_openai_client()
    if client is None:
        return None, {
            "code": "client_not_configured",
            "message": "OpenAI client not configured",
            "stage": "client",
        }
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            reasoning_effort="high",
        )
        content = resp.choices[0].message.content or "{}"
        return json.loads(content), None
    except Exception as exc:
        logger.exception("OpenAI recommendation request failed")
        return None, {
            "code": exc.__class__.__name__,
            "message": str(exc),
            "stage": "request",
        }


async def acall_openai_json(
    system_prompt: str, user_prompt: str
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    return await asyncio.to_thread(call_openai_json, system_prompt, user_prompt)


def generate_synopsis_for_candidate(
    title: str,
    year: Optional[int],
    genres: List[str],
    language: List[str],
    item_type: str,
    cast: Optional[List[str]] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    system_prompt = SYNOPSIS_PROMPTS.get(item_type)
    user_payload = {
        "title": title,
        "year": year,
        "genres": genres,
        "language": language,
        "cast": cast,
    }
    user_prompt = json.dumps(user_payload)
    result, failure = call_openai_json(system_prompt, user_prompt)
    synopsis_failure = failure.copy() if failure else None
    if synopsis_failure:
        synopsis_failure.setdefault("stage", "synopsis")
        synopsis_failure.setdefault("step", "synopsis")
    if not result:
        if synopsis_failure is None:
            synopsis_failure = {
                "code": "empty_response",
                "message": "No synopsis result returned",
                "stage": "synopsis",
                "step": "synopsis",
            }
        return None, synopsis_failure
    synopsis = result.get("synopsis")
    if synopsis is None and synopsis_failure is None:
        synopsis_failure = {
            "code": "missing_synopsis",
            "message": "Synopsis missing from AI response",
            "stage": "synopsis",
            "step": "synopsis",
        }
    return synopsis, synopsis_failure


async def agenerate_synopsis_for_candidate(
    title: str,
    year: Optional[int],
    genres: List[str],
    language: List[str],
    item_type: str,
    cast: Optional[List[str]] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    return await asyncio.to_thread(
        generate_synopsis_for_candidate, title, year, genres, language, item_type, cast
    )


def _add_attr(attrs: Dict[str, Any], show: Dict[str, Any], key: str) -> None:
    val = show.get(key)
    if not val:
        return
    if isinstance(val, list):
        attrs[key] = [str(x) for x in val]
    elif isinstance(val, dict):
        if "name" in val:
            attrs[key] = [str(val["name"])]
        else:
            attrs[key] = [str(x) for x in val.values()]
    else:
        attrs[key] = [str(val)]


def refresh_visible_item_attributes(item: IgnoreItem) -> Dict[str, Any]:
    attrs = dict(item.attributes or {})
    if item.item_type == "mv":
        try:
            result = radarr_query("movie/lookup", term="imdb:" + item.uid)[0]
            _add_attr(attrs, result, "genres")
            _add_attr(attrs, result, "originalLanguage")
            _add_attr(attrs, result, "studio")
            _add_attr(attrs, result, "runtime")
            attrs["year"] = result.get("year")
            tmdb_id = result.get("tmdbId") or get_movie_id(item.uid)
            if tmdb_id:
                attrs["tmdb_id"] = str(tmdb_id)
                if not attrs.get("director"):
                    try:
                        director = get_movie_director(int(tmdb_id))
                        if director:
                            attrs["director"] = director
                    except Exception:
                        logger.exception("Failed to refresh director for %s", item.uid)
            ratings = result.get("ratings")
            if ratings:
                attrs["rating_votes"] = ratings.get("votes")
                attrs["rating_value"] = ratings.get("value")
        except Exception:
            logger.exception("Failed to refresh movie attributes for %s", item.uid)
    else:
        try:
            show = query_series(item.uid)
            attrs["year"] = show.get("year")
            tmdb_id = show.get("tmdbId") or get_tv_id(item.uid)
            if tmdb_id:
                attrs["tmdb_id"] = str(tmdb_id)
                attrs["cast"] = get_tv_cast(tmdb_id, n=10)
            ratings = show.get("ratings")
            if ratings:
                attrs["rating_votes"] = ratings.get("votes")
                attrs["rating_value"] = ratings.get("value")
            for key in ("network", "genres", "status", "seriesType", "certification"):
                _add_attr(attrs, show, key)
        except Exception:
            logger.exception("Failed to refresh show attributes for %s", item.uid)
    return attrs


def _build_user_payload(
    item_type: str,
    uid: str,
    title: str,
    attrs: Dict[str, Any],
    candidate_synopsis: Optional[str],
) -> Dict[str, Any]:
    genres = _to_list_of_str(attrs.get("genres"))
    lang = _to_list_of_str(attrs.get("originalLanguage"))
    year = _year_from_attrs(attrs)
    payload: Dict[str, Any] = {
        "item_type": item_type,
        "candidate": {
            "title": title,
            "uid": uid,
            "year": year,
            "genres": genres,
            "language": lang,
            "synopsis": candidate_synopsis,
            "cast": attrs.get("cast"),
            "director": attrs.get("director"),
            "network": attrs.get("network"),
            "studio": attrs.get("studio"),
            "runtime": attrs.get("runtime"),
            "rating_value": attrs.get("rating_value"),
            "rating_votes": attrs.get("rating_votes"),
        },
    }
    if item_type == "mv":
        payload["candidate"]["release_count"] = (attrs.get("ai") or {}).get(
            "release_count"
        )
    return payload


def _ai_details_from_run(
    run: AgentRunResult,
    candidate_synopsis: Optional[str],
    synopsis_failure: Optional[Dict[str, Any]],
    base_ai: Dict[str, Any],
) -> Dict[str, Any]:
    details = dict(base_ai)
    details.setdefault("model", OPENAI_MODEL)
    details.setdefault("weaviate_uuid", base_ai.get("weaviate_uuid"))
    details["synopsis"] = candidate_synopsis
    details["tool_log"] = run.tool_log
    details["turns"] = run.turns
    details["tool_calls"] = run.tool_calls

    failure = synopsis_failure or run.failure
    submission = run.submission
    if submission is not None and run.failure is None:
        details.update(
            {
                "value": bool(submission.get("recommend")),
                "score": float(submission.get("score") or 0.0),
                "reason": str(submission.get("reason") or "")[:240],
                "failure": failure,
                "failed": failure is not None,
            }
        )
    else:
        details.update(
            {
                "value": None,
                "score": 0.0,
                "reason": (failure or {}).get("message", "AI did not submit verdict"),
                "failure": failure
                or {
                    "code": "no_submission",
                    "message": "AI completed without submitting a verdict",
                    "stage": "recommendation",
                },
                "failed": True,
            }
        )
    return details


async def annotate_with_ai_async(
    item_type: str,
    uid: str,
    title: str,
    attrs: Dict[str, Any],
    *,
    client: Optional[AsyncOpenAI] = None,
) -> Dict[str, Any]:
    """Async agentic recommendation flow.

    Steps: (1) hydrate cast/release_count, (2) generate synopsis + upsert to
    Weaviate, (3) run the OpenAI tool-calling agent until ``submit_recommendation``,
    (4) write a single consolidated ``ai`` block back to ``attrs``.

    If ``client`` is provided, it is reused for the agent loop (lets a batch
    caller share one connection pool across many candidates). If omitted, a
    fresh client is built and closed within this call so the caller doesn't
    have to manage its lifecycle.
    """
    with item_context(f"{item_type}:{uid}"):
        return await _annotate_with_ai_async_inner(
            item_type, uid, title, attrs, client=client
        )


async def _annotate_with_ai_async_inner(
    item_type: str,
    uid: str,
    title: str,
    attrs: Dict[str, Any],
    *,
    client: Optional[AsyncOpenAI] = None,
) -> Dict[str, Any]:
    import time as _time

    started_at = _time.monotonic()
    logger.info(
        "annotate START %s:%s '%s' genres=%s",
        item_type,
        uid,
        title,
        _to_list_of_str(attrs.get("genres")),
    )
    genres = _to_list_of_str(attrs.get("genres"))
    lang = _to_list_of_str(attrs.get("originalLanguage"))
    year = _year_from_attrs(attrs)

    if item_type == "mv":
        if not attrs.get("tmdb_id"):
            tmdb_id = await asyncio.to_thread(get_movie_id, uid)
            if tmdb_id:
                attrs["tmdb_id"] = tmdb_id
        if attrs.get("tmdb_id") and not attrs.get("cast"):
            try:
                attrs["cast"] = await asyncio.to_thread(
                    get_movie_cast, attrs["tmdb_id"], 10
                )
            except Exception:
                logger.exception("get_movie_cast failed for %s", uid)
        if attrs.get("tmdb_id") and not attrs.get("director"):
            try:
                director = await asyncio.to_thread(
                    get_movie_director, int(attrs["tmdb_id"])
                )
                if director:
                    attrs["director"] = director
            except Exception:
                logger.exception("get_movie_director failed for %s", uid)
        attrs, _, _ = await asyncio.to_thread(ensure_movie_release_count, attrs, uid)
    elif item_type == "tv":
        if not attrs.get("tmdb_id"):
            tmdb_id = await asyncio.to_thread(get_tv_id, uid)
            if tmdb_id:
                attrs["tmdb_id"] = tmdb_id
        if attrs.get("tmdb_id") and not attrs.get("cast"):
            try:
                attrs["cast"] = await asyncio.to_thread(
                    get_tv_cast, attrs["tmdb_id"], 10
                )
            except Exception:
                logger.exception("get_tv_cast failed for %s", uid)

    candidate_synopsis, synopsis_failure = await agenerate_synopsis_for_candidate(
        title, year, genres, lang, item_type, attrs.get("cast")
    )
    if synopsis_failure:
        logger.warning(
            "annotate %s:%s synopsis FAILED: %s",
            item_type,
            uid,
            synopsis_failure.get("message"),
        )
    elif candidate_synopsis:
        logger.info(
            "annotate %s:%s synopsis: %s",
            item_type,
            uid,
            (candidate_synopsis[:480] + " …")
            if len(candidate_synopsis) > 480
            else candidate_synopsis,
        )
    attrs = await asyncio.to_thread(
        upsert_item_attrs, attrs, item_type, uid, title, candidate_synopsis
    )

    base_ai = dict(attrs.get("ai") or {})

    owns_client = client is None
    if client is None:
        client = make_async_openai_client()
    if client is None:
        run = AgentRunResult(
            failure={
                "code": "client_not_configured",
                "message": "OpenAI async client not configured",
                "stage": "client",
            }
        )
        attrs_out = dict(attrs)
        attrs_out["ai"] = _ai_details_from_run(
            run, candidate_synopsis, synopsis_failure, base_ai
        )
        return attrs_out

    system_prompt = RECOMMENDATION_PROMPTS.get(item_type, "")
    user_payload = _build_user_payload(item_type, uid, title, attrs, candidate_synopsis)
    user_prompt = json.dumps(user_payload, default=str)

    ctx = ToolContext(
        item_type=item_type,
        candidate={
            "uid": uid,
            "title": title,
            "year": year,
            "genres": genres,
        },
    )

    try:
        run = await run_agent(
            client=client,
            model=OPENAI_MODEL,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            registry=REGISTRY,
            ctx=ctx,
            max_turns=AGENT_MAX_TURNS,
            max_tool_calls=AGENT_MAX_TOOL_CALLS,
        )
    finally:
        if owns_client:
            try:
                await client.close()
            except Exception:
                logger.exception("failed to close ad-hoc AsyncOpenAI client")

    attrs_out = dict(attrs)
    attrs_out["ai"] = _ai_details_from_run(
        run, candidate_synopsis, synopsis_failure, base_ai
    )
    elapsed = _time.monotonic() - started_at
    ai_out = attrs_out["ai"]
    if ai_out.get("failed"):
        logger.warning(
            "annotate FAIL  %s:%s '%s' elapsed=%.1fs failure=%s",
            item_type,
            uid,
            title,
            elapsed,
            ai_out.get("failure"),
        )
    else:
        reason = ai_out.get("reason") or ""
        logger.info(
            "annotate DONE  %s:%s '%s' recommend=%s score=%s "
            "turns=%s tool_calls=%s elapsed=%.1fs reason=%s",
            item_type,
            uid,
            title,
            ai_out.get("value"),
            ai_out.get("score"),
            ai_out.get("turns"),
            ai_out.get("tool_calls"),
            elapsed,
            reason if len(reason) <= 320 else reason[:320] + " …",
        )
    return attrs_out


def annotate_with_ai(
    item_type: str, uid: str, title: str, attrs: Dict[str, Any]
) -> Dict[str, Any]:
    """Synchronous wrapper. Prefer ``annotate_with_ai_async`` when in async code."""
    return asyncio.run(annotate_with_ai_async(item_type, uid, title, attrs))


def annotate_attributes_for_item(
    item_type: str, uid: str, title: str, attrs: Dict[str, Any]
) -> Dict[str, Any]:
    return annotate_with_ai(item_type, uid, title, attrs)


async def annotate_attributes_for_item_async(
    item_type: str, uid: str, title: str, attrs: Dict[str, Any]
) -> Dict[str, Any]:
    return await annotate_with_ai_async(item_type, uid, title, attrs)
