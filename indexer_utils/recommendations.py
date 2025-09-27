from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from .ai_recs import OPENAI_MODEL, call_openai_json
from .plex_utils import get_recently_played_imdb_ids
from .radarr_utils import radarr_query

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 25
RECENT_HISTORY_LIMIT = 40


def _safe_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _poster_url(movie: Dict[str, Any]) -> Optional[str]:
    images = movie.get("images") or []
    for image in images:
        if not isinstance(image, dict):
            continue
        if image.get("coverType") == "poster" and image.get("url"):
            return str(image["url"])
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


def _build_candidates(radarr_movies: Sequence[Dict[str, Any]], recent_ids: Set[str]) -> List[MovieCandidate]:
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


def _choose_with_openai(
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
    }
    system_prompt = (
        "You are a movie recommendation assistant for a personal media library. "
        "Pick a single movie from the provided list that best matches the viewer's preferences. "
        "If the viewer provides a prompt, weigh it heavily. "
        "Always avoid suggesting movies that are flagged as recently watched. "
        "Respond using compact JSON with fields: imdb_id (string), title (string), reason (string)."
    )
    try:
        result = call_openai_json(system_prompt, json.dumps(payload))
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


def recommend_movie(prompt: Optional[str] = None) -> Optional[MovieRecommendationResult]:
    try:
        radarr_movies = radarr_query("movie")
    except Exception:
        logger.exception("Failed to load Radarr library")
        return None
    if not isinstance(radarr_movies, list):
        logger.warning("Unexpected Radarr response: %s", type(radarr_movies))
        return None

    recent_ids = get_recently_played_imdb_ids(limit=RECENT_HISTORY_LIMIT)
    candidates = _build_candidates(radarr_movies, recent_ids)
    if not candidates:
        return None

    openai_result = _choose_with_openai(prompt, candidates)
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
    )

