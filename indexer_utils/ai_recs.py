import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from decouple import config
from openai import OpenAI

from indexer_utils.tmdb import (
    get_movie_cast,
    get_movie_id,
    get_movie_release_count,
    get_tv_cast,
    get_tv_id,
)

from .models import IgnoreItem
from .radarr_utils import radarr_query
from .session import db_session
from .sonarr_utils import query_series
from .weaviate_client import (
    get_nearest_neighbors,
    upsert_item_attrs,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

BASE_DIR = Path(__file__).parent
PROMPTS_DIR = BASE_DIR / "prompts"
OPENAI_MODEL = config("OPENAI_MODEL", default="gpt-5.2")


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

N_NEIGHBORS = 40
_openai_client = None

SIMILAR_ATTRIBUTE_KEYS = [
    "genres",
    "director",
    "network",
    "rating_value",
    "rating_votes",
    "imdbuser_value",
    "imdbuser_votes",
    "tmdbuser_value",
    "tmdbuser_votes",
    "originalLanguage",
    "rottenTomatoesuser_value",
    "rottenTomatoesuser_votes",
    "cast",
]


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


def get_openai_client():
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    if OpenAI is None:
        return None
    try:
        api_key = config("OPENAI_API_KEY")
        _openai_client = OpenAI(api_key=api_key)
        return _openai_client
    except Exception:
        logger.exception("Failed to initialize OpenAI client")
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
            attrs["year"] = result.get("year")
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


def annotate_with_ai(
    item_type: str, uid: str, title: str, attrs: Dict[str, Any]
) -> Dict[str, Any]:
    # Basic item facts for the prompt / candidate context
    logger.info(f"Annotating {item_type} {uid} {title}")
    genres = _to_list_of_str(attrs.get("genres"))
    lang = _to_list_of_str(attrs.get("originalLanguage"))
    year = _year_from_attrs(attrs)
    if item_type == "mv":
        attrs["tmdb_id"] = attrs.get("tmdb_id") or get_movie_id(uid)
        if attrs["tmdb_id"]:
            attrs["cast"] = attrs.get("cast") or get_movie_cast(attrs["tmdb_id"], n=10)
        attrs, _, _ = ensure_movie_release_count(attrs, uid)
    elif item_type == "tv":
        attrs["tmdb_id"] = attrs.get("tmdb_id") or get_tv_id(uid)
        if attrs["tmdb_id"]:
            attrs["cast"] = attrs.get("cast") or get_tv_cast(attrs["tmdb_id"], n=10)

    # Generate synopsis and embedding first
    candidate_synopsis, synopsis_failure = generate_synopsis_for_candidate(
        title, year, genres, lang, item_type, attrs.get("cast")
    )
    attrs = upsert_item_attrs(attrs, item_type, uid, title, candidate_synopsis)

    # Choose similar by vector if possible, otherwise fallback to attribute similarity
    similar_defs = get_nearest_neighbors(
        attrs.get("ai", {}).get("weaviate_uuid"), N_NEIGHBORS, item_type
    )
    session = db_session()
    similar_pairs = [
        (
            session.query(IgnoreItem).filter(IgnoreItem.uid == item_def["uid"]).first(),
            item_def["distance"],
        )
        for item_def in similar_defs.values()
    ]

    similar_summary = []
    for s, distance in similar_pairs:
        if s is None:
            continue
        if s.uid == uid:
            continue
        item_attrs = s.attributes or {}
        attributes = {
            k: v for k in SIMILAR_ATTRIBUTE_KEYS if (v := item_attrs.get(k)) is not None
        }
        if item_type == "mv":
            item_attrs, release_count, updated = ensure_movie_release_count(
                item_attrs, s.uid
            )
            attributes["release_count"] = release_count
            if updated:
                s.attributes = item_attrs
                s.save()
        similar_summary.append(
            {
                "title": s.title,
                "uid": s.uid,
                "distance": round(distance, 3),
                "genres": _to_list_of_str(item_attrs.get("genres")),
                "added": s.added,
                "attributes": attributes,
            }
        )

    added_similar = [s for s in similar_summary if s.get("added")]
    ignored_similar = [s for s in similar_summary if not s.get("added")]

    system_prompt = RECOMMENDATION_PROMPTS.get(item_type)
    candidate_attributes = {
        k: v for k in SIMILAR_ATTRIBUTE_KEYS if (v := (attrs or {}).get(k)) is not None
    }
    if item_type == "mv":
        candidate_attributes["release_count"] = (attrs.get("ai") or {}).get(
            "release_count"
        )
    user_payload = {
        "item_type": item_type,
        "candidate": {
            "title": title,
            "uid": uid,
            "year": year,
            "genres": genres,
            "language": lang,
            "synopsis": candidate_synopsis,
            "attributes": candidate_attributes,
        },
        "similar_items_added": added_similar or "No similar items added",
        "similar_items_ignored": ignored_similar or "No similar items ignored",
    }
    user_prompt = json.dumps(user_payload)

    result, recommendation_failure = call_openai_json(system_prompt, user_prompt)
    if recommendation_failure:
        recommendation_failure = dict(recommendation_failure)
        recommendation_failure.setdefault("stage", "recommendation")
        recommendation_failure.setdefault("step", "recommendation")

    # Consolidate AI info into a single 'ai' attribute with boolean value and details
    attrs_out = dict(attrs)
    ai_details: Dict[str, Any] = attrs_out.get(
        "ai",
        {
            "value": None,  # boolean when available
            "score": None,
            "reason": None,
            "synopsis": None,
            "similar": similar_summary,
            "model": OPENAI_MODEL,
            "weaviate_uuid": None,
            "failure": None,
            "failed": False,
        },
    )

    failure_details = synopsis_failure or recommendation_failure
    if failure_details:
        ai_details.update(
            {
                "failure": failure_details,
                "failed": True,
            }
        )
    else:
        ai_details.update({"failure": None, "failed": False})

    if result is None:
        ai_details.update(
            {
                "value": None,
                "score": 0.0,
                "reason": "AI not configured or failed",
            }
        )
        if ai_details.get("failure") is None:
            ai_details.update(
                {
                    "failure": {
                        "code": "unknown_failure",
                        "message": "AI returned no result",
                        "stage": "recommendation",
                    },
                    "failed": True,
                }
            )
    else:
        try:
            rec = bool(result.get("recommend"))
            score = float(result.get("score", 0))
            reason = str(result.get("reason", ""))[:240]
            ai_details.update(
                {
                    "value": rec,
                    "score": score,
                    "reason": reason,
                    "synopsis": candidate_synopsis,
                }
            )
        except Exception:
            ai_details.update(
                {
                    "value": None,
                    "score": 0.0,
                    "reason": "AI parse error",
                }
            )
            if ai_details.get("failure") is None:
                ai_details.update(
                    {
                        "failure": {
                            "code": "parse_error",
                            "message": "Unable to parse AI recommendation",
                            "stage": "recommendation",
                        },
                        "failed": True,
                    }
                )

    attrs_out["ai"] = ai_details
    return attrs_out


def annotate_attributes_for_item(
    item_type: str, uid: str, title: str, attrs: Dict[str, Any]
) -> Dict[str, Any]:
    """Public wrapper to annotate attributes using the same logic as ingestion.

    Returns a new attributes dict including the consolidated 'ai' details and
    optional 'synopsis_vector' if embeddings are enabled.
    """
    return annotate_with_ai(item_type, uid, title, attrs)
