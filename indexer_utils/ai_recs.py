import json
import logging
from typing import Any, Dict, List, Optional

from decouple import config
from indexer_utils.tmdb import get_movie_id, get_movie_cast
from openai import OpenAI

from .models import IgnoreItem
from .session import db_session
from .weaviate_client import (
    get_nearest_neighbors,
    upsert_item_attrs,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

OPENAI_MODEL = config("OPENAI_MODEL", default="gpt-5-mini")
OPENAI_API_KEY = config("OPENAI_API_KEY", default=None)

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
    "mv": """
    You are a personal media curator. Based on the user's previously added movies,
    decide if the new item matches their taste. Respond in compact JSON with fields:
    recommend (boolean), score (0..1), reason (short sentence).
    """,
    "tv": """
    You are a personal media curator. Based on the user's previously added TV shows,
    decide if the new item matches their taste. Respond in compact JSON with fields:
    recommend (boolean), score (0..1), reason (short sentence).
    """,
}

N_NEIGHBORS = 20
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


def get_openai_client():
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    if not OPENAI_API_KEY or OpenAI is None:
        return None
    try:
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
        return _openai_client
    except Exception:
        logger.exception("Failed to initialize OpenAI client")
        return None


def call_openai_json(system_prompt: str, user_prompt: str) -> Optional[Dict[str, Any]]:
    client = get_openai_client()
    if client is None:
        return None
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
        return json.loads(content)
    except Exception:
        logger.exception("OpenAI recommendation request failed")
        return None


def generate_synopsis_for_candidate(
    title: str,
    year: Optional[int],
    genres: List[str],
    language: List[str],
    item_type: str,
    cast: Optional[List[str]] = None,
) -> Optional[str]:
    system_prompt = SYNOPSIS_PROMPTS.get(item_type)
    user_payload = {
        "title": title,
        "year": year,
        "genres": genres,
        "language": language,
        "cast": cast,
    }
    user_prompt = json.dumps(user_payload)
    result = call_openai_json(system_prompt, user_prompt)
    if not result:
        return None
    return result.get("synopsis")


def annotate_with_ai(
    item_type: str, uid: str, title: str, attrs: Dict[str, Any]
) -> Dict[str, Any]:
    # Basic item facts for the prompt / candidate context
    genres = _to_list_of_str(attrs.get("genres"))
    lang = _to_list_of_str(attrs.get("originalLanguage"))
    year = _year_from_attrs(attrs)
    attrs["tmdb_id"] = attrs.get("tmdb_id") or get_movie_id(uid)
    if attrs["tmdb_id"]:
        attrs["cast"] = attrs.get("cast") or get_movie_cast(attrs["tmdb_id"], n=10)

    # Generate synopsis and embedding first
    candidate_synopsis = generate_synopsis_for_candidate(
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

    similar_summary = [
        {
            "title": s.title,
            "uid": s.uid,
            "distance": round(distance, 3),
            "genres": _to_list_of_str((s.attributes or {}).get("genres")),
            "added": s.added,
            "attributes": {
                k: v
                for k in SIMILAR_ATTRIBUTE_KEYS
                if (v := (s.attributes or {}).get(k)) is not None
            },
        }
        for (s, distance) in similar_pairs
        if s is not None
    ]

    added_similar = [s for s in similar_summary if s.get("added")]
    ignored_similar = [s for s in similar_summary if not s.get("added")]

    system_prompt = RECOMMENDATION_PROMPTS.get(item_type)
    user_payload = {
        "item_type": item_type,
        "candidate": {
            "title": title,
            "uid": uid,
            "year": year,
            "genres": genres,
            "language": lang,
            "synopsis": candidate_synopsis,
            "attributes": {
                k: v
                for k in SIMILAR_ATTRIBUTE_KEYS
                if (v := (attrs or {}).get(k)) is not None
            },
        },
        "similar_items_added": added_similar or "No similar items added",
        "similar_items_ignored": ignored_similar or "No similar items ignored",
    }
    user_prompt = json.dumps(user_payload)

    result = call_openai_json(system_prompt, user_prompt)

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
        },
    )

    if result is None:
        ai_details.update(
            {
                "value": None,
                "score": 0.0,
                "reason": "AI not configured or failed",
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
