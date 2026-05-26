"""The `taste_signal` payload block — the recommendation agent's primary input.

Every value is a raw historical count (``added`` of ``n``) over the candidate's
*cohort*: decided same-type titles released within ``±NEIGHBOR_YEAR_WINDOW``
years. No rates, no flags, no interpretation — the consuming model derives
proportions and weighs the axes itself.

Axes:
  * ``neighbor_x_critic`` — the cohort partitioned by (a title's own
    20-nearest-by-synopsis add-rate below/above the cohort rate) × (whether it
    carries a critic score). The interaction is the sharpest single signal:
    within ``below_base``, critic *presence* tracks adds even though the score
    value doesn't, because no-name filler is never critically rated.
  * ``by_attribute`` — the cohort add-rate for the candidate's own categorical
    values (network, language, genre); overlapping sub-counts, not a partition.
  * ``cast_xref`` — how many *added* library titles each of the candidate's cast
    members appears in. Unlike the cohort axes this spans the whole library, not
    the ±window era: cast is a cross-era bridge (an actor's older films
    predicting a new one). Strong for movies (AUC ~0.8 within recent years),
    weaker for TV; the modal case is no overlap.

The cohort cross-tab needs each cohort member's own nearest-neighbour sweep
(~10s for a movie era), so the scored cohort is cached in Redis keyed by
``(item_type, year)`` — identical for every candidate of the same era, so one
compute amortizes across a whole ingest batch.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from .redis_client import get_redis_client, redis_get_json, redis_set_json
from .vector_search import NEIGHBOR_YEAR_WINDOW, synopsis_neighbor_summary

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_VERSION = "v1"

# Candidate attribute key → block label. Whichever the candidate has are used.
# Movie `studio` is dropped: it's only populated on notable/added films, so its
# add-rate is presence-confounded (~100% for any studio with real n). TV
# `network` is clean and decisive (e.g. Korean/anime networks at 0/n).
CATEGORICAL_AXES = [
    ("network", "network"),
    ("originalLanguage", "language"),
    ("genres", "genre"),
]
# block label → cohort-row key (set by COHORT_SQL aliases)
_AXIS_COL = {"network": "network", "language": "language", "genre": "genres"}

# Per decided ±window cohort item: added, critic-presence, the synopsis-neighbor
# stratum (its own 20-NN add-rate vs the cohort mean), and the categorical attrs.
COHORT_SQL = """
WITH cohort AS (
  SELECT uid, added, synopsis_vector,
         (attributes ? 'rottenTomatoesuser_value' OR attributes ? 'metacriticuser_value') AS has_critic,
         attributes->'network'          AS network,
         attributes->'originalLanguage' AS language,
         attributes->'genres'           AS genres
  FROM indexer_utils_ignoreitem
  WHERE item_type = :it AND synopsis_vector IS NOT NULL AND ignore IS TRUE
    AND attributes->>'year' ~ '^[0-9]+$'
    AND abs((attributes->>'year')::int - :y) <= :w
),
base AS (SELECT avg(CASE WHEN added THEN 1.0 ELSE 0 END) br FROM cohort)
SELECT c.added, c.has_critic, c.network, c.language, c.genres,
       (n.added_count::float / NULLIF(n.k, 0)) <= (SELECT br FROM base) AS below_base
FROM cohort c
CROSS JOIN LATERAL (
  SELECT count(*) k, count(*) FILTER (WHERE x.added) added_count
  FROM (SELECT x.added FROM cohort x WHERE x.uid <> c.uid
        ORDER BY x.synopsis_vector <=> c.synopsis_vector LIMIT 20) x
) n;
"""


# How many of the candidate's cast members to name in ``contributors``. The
# scalar counts (best / n_with_prior_add) span the full cast regardless.
CAST_XREF_LIMIT = 5

# Per candidate cast member: how many *added* titles of this type they appear in,
# excluding the candidate itself (uid filter → exact leave-one-out). Targeted to
# the candidate's own cast so it stays a small, fast aggregate rather than a
# library-wide index.
CAST_XREF_SQL = """
SELECT lower(btrim(name)) AS name, count(DISTINCT uid) AS added
FROM indexer_utils_ignoreitem,
     LATERAL jsonb_array_elements_text(attributes->'cast') AS name
WHERE item_type = :it AND added IS TRUE AND uid <> :uid
  AND jsonb_typeof(attributes->'cast') = 'array'
  AND lower(btrim(name)) IN :names
GROUP BY lower(btrim(name));
"""


def _cast_names(attrs: Dict[str, Any]) -> List[str]:
    """Candidate cast as display strings, de-duped, first-seen casing kept."""
    raw = attrs.get("cast")
    if not isinstance(raw, list):
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for x in raw:
        if isinstance(x, str):
            name = x.strip()
        elif isinstance(x, dict):
            name = str(x.get("name") or "").strip()
        else:
            name = ""
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


async def _cast_xref(
    session: AsyncSession,
    item_type: str,
    candidate_attrs: Dict[str, Any],
    candidate_uid: str,
) -> Optional[Dict[str, Any]]:
    """Cross-reference the candidate's cast against the added library.

    ``None`` when the candidate carries no cast metadata (so the consumer can
    tell "no cast data" apart from "cast present, no overlap" — the latter is
    the informative modal case and comes back with empty ``contributors``).
    """
    names = _cast_names(candidate_attrs)
    if not names:
        return None
    rows = (
        await session.execute(
            text(CAST_XREF_SQL).bindparams(bindparam("names", expanding=True)),
            {
                "it": item_type,
                "uid": candidate_uid,
                "names": [n.lower() for n in names],
            },
        )
    ).all()
    counts = {r.name: int(r.added) for r in rows}
    matched = [(n, counts[n.lower()]) for n in names if counts.get(n.lower())]
    matched.sort(key=lambda nc: nc[1], reverse=True)
    return {
        "best_actor_adds": max(counts.values(), default=0),
        "n_cast_with_prior_add": len(matched),
        "contributors": [{"name": n, "added": c} for n, c in matched[:CAST_XREF_LIMIT]],
    }


def _to_set(v: Any) -> Set[str]:
    """Normalize a stored attr value (str / list / None) to a lowercased set."""
    if v is None:
        return set()
    if isinstance(v, list):
        return {str(x).strip().lower() for x in v if str(x).strip()}
    return {str(v).strip().lower()}


def _cell(added: int, n: int) -> Dict[str, int]:
    return {"added": added, "n": n}


async def _scored_cohort(
    session: AsyncSession, item_type: str, year: int
) -> List[Dict[str, Any]]:
    """The decided ±window cohort, each row scored with its neighbor stratum.

    Cached in Redis by ``(item_type, year)`` — the expensive part (one nearest-
    neighbour sweep per cohort member) is identical for every candidate of the
    same era.
    """
    key = f"mediamanager:taste_cohort:{CACHE_VERSION}:{item_type}:{year}"
    redis = get_redis_client()
    cached = redis_get_json(redis, key)
    if isinstance(cached, list):
        return cached

    rows = (
        await session.execute(
            text(COHORT_SQL), {"it": item_type, "y": year, "w": NEIGHBOR_YEAR_WINDOW}
        )
    ).all()
    scored = [
        {
            "added": bool(r.added),
            "has_critic": bool(r.has_critic),
            "below_base": None if r.below_base is None else bool(r.below_base),
            "network": r.network,
            "language": r.language,
            "genres": r.genres,
        }
        for r in rows
    ]
    redis_set_json(redis, key, scored, CACHE_TTL_SECONDS)
    return scored


async def build_taste_signal(
    session: AsyncSession,
    *,
    item_type: str,
    year: int,
    candidate_attrs: Dict[str, Any],
    candidate_vec: List[float],
    candidate_uid: str,
) -> Optional[Dict[str, Any]]:
    """Assemble the ``taste_signal`` block, or ``None`` when there's no cohort
    or no neighbour data to compare against."""
    scored = await _scored_cohort(session, item_type, year)
    if not scored:
        return None
    n_cohort = len(scored)
    added_cohort = sum(1 for r in scored if r["added"])

    # neighbor-stratum × critic-presence, raw counts; partitions the cohort.
    matrix: Dict[str, Dict[str, int]] = {}
    for below in (True, False):
        for crit in (True, False):
            grp = [
                r
                for r in scored
                if r["below_base"] == below and r["has_critic"] == crit
            ]
            key = (
                f"{'below' if below else 'above'}_base + "
                f"{'has_critic' if crit else 'no_critic'}"
            )
            matrix[key] = _cell(sum(1 for r in grp if r["added"]), len(grp))

    nb = await synopsis_neighbor_summary(
        session, item_type, candidate_uid, candidate_vec, year
    )
    if nb is None:
        return None
    added_of_top, kk, nearest = nb["added_of_top"], nb["k"], nb["nearest"]
    has_critic = (
        "rottenTomatoesuser_value" in candidate_attrs
        or "metacriticuser_value" in candidate_attrs
    )
    # Index the candidate into the matrix: its 20-NN add-rate vs the cohort rate.
    cand_below = (added_of_top / kk if kk else 0.0) <= (
        added_cohort / n_cohort if n_cohort else 0.0
    )
    cell = (
        f"{'below' if cand_below else 'above'}_base + "
        f"{'has_critic' if has_critic else 'no_critic'}"
    )

    by_attr: Dict[str, Dict[str, Dict[str, int]]] = {}
    for attr_key, label in CATEGORICAL_AXES:
        cand_vals = _to_set(candidate_attrs.get(attr_key))
        if not cand_vals:
            continue
        col = _AXIS_COL[label]
        per_value: Dict[str, Dict[str, int]] = {}
        for val in sorted(cand_vals):
            matched = [r for r in scored if val in _to_set(r[col])]
            per_value[val] = _cell(sum(1 for r in matched if r["added"]), len(matched))
        by_attr[label] = per_value

    window = NEIGHBOR_YEAR_WINDOW
    block: Dict[str, Any] = {
        "cohort": {
            "scope": f"decided {item_type} titles released "
            f"{year - window}-{year + window}",
            "n": n_cohort,
            "added": added_cohort,
        },
        "candidate": {
            "neighbors_added": added_of_top,
            "of": kk,
            "has_critic_rating": has_critic,
            "cell": cell,
        },
        "neighbor_x_critic": matrix,
        "by_attribute": by_attr,
        "nearest": nearest,
    }
    cast_xref = await _cast_xref(session, item_type, candidate_attrs, candidate_uid)
    if cast_xref is not None:
        block["cast_xref"] = cast_xref
    return block
