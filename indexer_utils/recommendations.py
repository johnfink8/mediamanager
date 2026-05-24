from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

from decouple import UndefinedValueError, config

from .ai_recs import OPENAI_MODEL, call_openai_json
from .models import MovieRecommendationRecord, RecommendationPreference
from .plex_utils import get_recently_played_imdb_ids
from .radarr_utils import radarr_query
from .redis_client import get_redis_client, redis_get_json, redis_set_json

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 25
RECENT_HISTORY_LIMIT = 40
HISTORY_PAYLOAD_LIMIT = 10
CACHE_TTL_SECONDS = 60 * 60
RADARR_MOVIES_CACHE_KEY = "recommend_movie:radarr_movies"
RECENT_IDS_CACHE_KEY = "recommend_movie:recent_ids"


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _radarr_url_parts() -> Tuple[Optional[str], Optional[str]]:
    """Return the Radarr origin and media base URL if configured."""

    try:
        raw = config("RADARR_URL")
    except UndefinedValueError:
        return None, None

    if not raw:
        return None, None

    parsed = urlparse(str(raw))
    if parsed.scheme and parsed.netloc:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        segments = [segment for segment in parsed.path.split("/") if segment]
        while segments and segments[-1].lower().startswith("api"):
            segments.pop()
        base_path = "/" + "/".join(segments) if segments else ""
        base = f"{origin}{base_path}"
        if not base.endswith("/"):
            base = f"{base}/"
        return origin, base

    raw = str(raw).rstrip("/")
    if not raw:
        return None, None
    return raw, f"{raw}/"


def _poster_url(movie: Dict[str, Any]) -> Optional[str]:
    images = movie.get("images") or []
    for image in images:
        if not isinstance(image, dict):
            continue
        if image.get("coverType") == "poster" and image.get("url"):
            poster = str(image["url"])
            if poster.startswith(("http://", "https://", "data:")):
                return poster
            origin, base = _radarr_url_parts()
            if poster.startswith("//") and origin:
                scheme = origin.split(":", 1)[0]
                return f"{scheme}:{poster}"
            if poster.startswith("/"):
                if origin:
                    return f"{origin}{poster}"
                return poster
            if base:
                return urljoin(base, poster)
            return poster
    return None


def _genres(movie: Dict[str, Any]) -> List[str]:
    genres = movie.get("genres") or []
    if isinstance(genres, list):
        return [str(g) for g in genres if g]
    if isinstance(genres, str):
        return [genres]
    return []


def _cast(movie: Dict[str, Any], limit: int = 6) -> List[str]:
    credits: Sequence[Any] = movie.get("credits") or movie.get("people") or []
    names: List[str] = []
    for entry in credits:
        if not isinstance(entry, dict):
            continue
        entry_type = str(entry.get("type", "")).lower()
        if entry_type and entry_type not in {"actor", "cast"}:
            continue
        name = entry.get("name")
        if name:
            names.append(str(name))
        if len(names) >= limit:
            break
    if names:
        return names

    # Fallback to Radarr "actors" or "cast" keys if present
    for key in ("actors", "cast"):
        raw = movie.get(key)
        if isinstance(raw, list):
            names = [str(item.get("name", item)) for item in raw[:limit] if item]
            if names:
                return names
    return []


def _primary_rating(movie: Dict[str, Any]) -> float:
    ratings = movie.get("ratings") or {}
    if isinstance(ratings, dict):
        for key in ("imdb", "tmdb", "metacritic"):
            candidate = ratings.get(key)
            if isinstance(candidate, dict) and candidate.get("value") is not None:
                value = _safe_float(candidate.get("value"))
                if value > 0:
                    return value
    statistics = movie.get("statistics") or {}
    if isinstance(statistics, dict):
        for key in ("popularity", "watchers", "collectors"):
            value = statistics.get(key)
            numeric = _safe_float(value)
            if numeric > 0:
                return numeric
    return _safe_float(movie.get("runtime"))


def _normalize_imdb(imdb_id: Optional[str]) -> Optional[str]:
    if not imdb_id:
        return None
    if not isinstance(imdb_id, str):
        imdb_id = str(imdb_id)
    if "tt" in imdb_id:
        idx = imdb_id.index("tt")
        imdb_id = imdb_id[idx:]
    imdb_id = imdb_id.strip()
    return imdb_id or None


def _recent_titles(
    imdb_ids: Set[str],
    radarr_movies: Iterable[Dict[str, Any]],
    limit: int = 5,
) -> List[str]:
    titles: List[str] = []
    for movie in radarr_movies:
        imdb_id = _normalize_imdb(movie.get("imdbId"))
        if imdb_id is None:
            continue
        if imdb_id in imdb_ids:
            title = movie.get("title") or movie.get("originalTitle")
            if title:
                titles.append(str(title))
        if len(titles) >= limit:
            break
    return titles


@dataclass
class MovieCandidate:
    imdb_id: str
    title: str
    year: Optional[int]
    overview: Optional[str]
    poster_url: Optional[str]
    genres: List[str]
    cast: List[str]
    rating: float
    radarr_payload: Dict[str, Any]


@dataclass
class MovieRecommendationResult:
    imdb_id: str
    title: str
    overview: Optional[str]
    poster_url: Optional[str]
    year: Optional[int]
    genres: List[str]
    cast: List[str]
    reason: Optional[str]
    source: str
    prompt: Optional[str]
    excluded_recent: List[str]
    record_id: Optional[int]
    preference: Optional[RecommendationPreference]


async def _history_payload(
    limit: int = HISTORY_PAYLOAD_LIMIT,
) -> List[Dict[str, Optional[str]]]:
    try:
        records = await MovieRecommendationRecord.recent_history(limit=limit)
    except Exception:
        logger.exception("Failed to load recommendation history")
        return []
    history: List[Dict[str, Optional[str]]] = []
    for record in records:
        history.append(
            {
                "prompt": record.prompt,
                "imdb_id": record.recommended_imdb_id,
                "title": record.recommended_title,
                "preference": record.preference.value if record.preference else None,
            }
        )
    return history


def _build_candidates(
    radarr_movies: Sequence[Dict[str, Any]], recent_ids: Set[str]
) -> List[MovieCandidate]:
    candidates: List[MovieCandidate] = []
    for movie in radarr_movies:
        if movie.get("hasFile") is False:
            continue
        imdb_id = _normalize_imdb(movie.get("imdbId"))
        if imdb_id is None or imdb_id in recent_ids:
            continue
        title = movie.get("title") or movie.get("originalTitle")
        if not title:
            continue
        candidate = MovieCandidate(
            imdb_id=imdb_id,
            title=str(title),
            year=movie.get("year"),
            overview=movie.get("overview"),
            poster_url=_poster_url(movie),
            genres=_genres(movie),
            cast=_cast(movie),
            rating=_primary_rating(movie),
            radarr_payload=movie,
        )
        candidates.append(candidate)
    candidates.sort(key=lambda c: (c.rating, c.year or 0), reverse=True)
    return candidates


async def _choose_with_openai(
    prompt: Optional[str], candidates: Sequence[MovieCandidate]
) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    payload = {
        "prompt": prompt,
        "model": OPENAI_MODEL,
        "movies": [
            {
                "title": c.title,
                "imdb_id": c.imdb_id,
                "year": c.year,
                "genres": c.genres,
                "cast": c.cast,
                "overview": c.overview,
                "rating_hint": c.rating,
            }
            for c in candidates[:MAX_CANDIDATES]
        ],
        "history": await _history_payload(),
    }
    system_prompt = (
        "You are a movie recommendation assistant for a personal media library. "
        "Pick a single movie from the provided list that best matches the viewer's preferences. "
        "If the viewer provides a prompt, weigh it heavily. "
        "Always avoid suggesting movies that are flagged as recently watched. "
        "Respond using compact JSON with fields: imdb_id (string), title (string), reason (string)."
    )
    logger.info("Sending OpenAI movie recommendation request with prompt: %s", prompt)
    logger.debug("OpenAI movie recommendation payload: %s", payload)
    try:
        result, failure = call_openai_json(system_prompt, json.dumps(payload))
        logger.info(
            "Received OpenAI movie recommendation result: %s (failure=%s)",
            result,
            failure,
        )
        if not isinstance(result, dict):
            return None
        return result
    except Exception:
        logger.exception("OpenAI movie recommendation failed")
        return None


def _resolve_openai_choice(
    result: Optional[Dict[str, Any]], candidates: Sequence[MovieCandidate]
) -> Optional[MovieCandidate]:
    if not result:
        return None
    imdb_id = result.get("imdb_id") or result.get("imdbId")
    imdb_id = _normalize_imdb(imdb_id)
    if imdb_id:
        for candidate in candidates:
            if candidate.imdb_id == imdb_id:
                return candidate
    title = result.get("title")
    if title:
        title = str(title).strip().lower()
        for candidate in candidates:
            if candidate.title.lower() == title:
                return candidate
    return None


async def recommend_movie(
    prompt: Optional[str] = None,
) -> Optional[MovieRecommendationResult]:
    redis_client = get_redis_client()

    cached_movies = redis_get_json(redis_client, RADARR_MOVIES_CACHE_KEY)
    radarr_movies: Optional[List[Dict[str, Any]]]
    if isinstance(cached_movies, list):
        radarr_movies = cached_movies
    else:
        try:
            radarr_movies = radarr_query("movie")
        except Exception:
            logger.exception("Failed to load Radarr library")
            return None
        if isinstance(radarr_movies, list):
            redis_set_json(
                redis_client,
                RADARR_MOVIES_CACHE_KEY,
                radarr_movies,
                CACHE_TTL_SECONDS,
            )

    if not isinstance(radarr_movies, list):
        logger.warning("Unexpected Radarr response: %s", type(radarr_movies))
        return None

    cached_recent = redis_get_json(redis_client, RECENT_IDS_CACHE_KEY)
    if isinstance(cached_recent, list):
        recent_ids = {str(item) for item in cached_recent if item}
    else:
        recent_ids = get_recently_played_imdb_ids(limit=RECENT_HISTORY_LIMIT)
        redis_set_json(
            redis_client,
            RECENT_IDS_CACHE_KEY,
            sorted(str(item) for item in recent_ids if item),
            CACHE_TTL_SECONDS,
        )
    candidates = _build_candidates(radarr_movies, recent_ids)
    if not candidates:
        return None

    openai_result = await _choose_with_openai(prompt, candidates)
    chosen = _resolve_openai_choice(openai_result, candidates)

    source = "openai" if chosen and openai_result else "fallback"
    reason = None
    if openai_result and isinstance(openai_result, dict):
        reason = openai_result.get("reason") or openai_result.get("explanation")
        if reason is not None:
            reason = str(reason)

    if chosen is None:
        chosen = candidates[0]
        if reason is None:
            reason = "Top-rated available movie in your Radarr library."

    excluded_titles = _recent_titles(recent_ids, radarr_movies)

    record: Optional[MovieRecommendationRecord] = None
    try:
        record = await MovieRecommendationRecord.log_recommendation(
            prompt=prompt,
            imdb_id=chosen.imdb_id,
            title=chosen.title,
            reason=reason,
            source=source,
        )
    except Exception:
        logger.exception("Failed to persist movie recommendation record")

    return MovieRecommendationResult(
        imdb_id=chosen.imdb_id,
        title=chosen.title,
        overview=chosen.overview,
        poster_url=chosen.poster_url,
        year=chosen.year,
        genres=chosen.genres,
        cast=chosen.cast,
        reason=reason,
        source=source,
        prompt=prompt,
        excluded_recent=excluded_titles,
        record_id=record.id if record else None,
        preference=record.preference if record else None,
    )
