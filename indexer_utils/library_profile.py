"""Pre-computed taste-profile snapshot of the user's added library.

Injected into the recommendation agent's user prompt instead of being
derived by the agent from per-tool ``decision_counts``. Two reasons:

1. Per-genre absolute counts are unbounded — the universe of "horror
   movies the user hasn't added" is effectively all horror ever made, so
   "added vs rejected" ratios within a single genre carry no useful
   information about taste strength.
2. Relative composition (genre shares, top studios/directors, decade
   distribution) is bounded, deterministic, and directly comparable
   across candidates. The model reads a candidate's genres against the
   profile's rankings and gets a real taste signal.

The profile is candidate-agnostic and cheap enough to compute per run
(single pass over added rows). ``compute_candidate_match`` overlays the
candidate's position within each distribution so the model sees a
pre-resolved "Horror: #6 of 52" rather than having to count.
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import IgnoreItem

# How many entries to keep per distribution. Top-N is enough to convey shape;
# the long tail rarely shifts a recommendation and just burns tokens.
TOP_GENRES = 20
TOP_NETWORKS = 15
TOP_STUDIOS = 15
TOP_DIRECTORS = 10
TOP_LANGUAGES = 6


def _attrs_list(attrs: Dict[str, Any], key: str) -> List[str]:
    """Normalize an attrs field that's sometimes a list and sometimes a str."""
    val = attrs.get(key)
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val if v]
    return [str(val)]


def _attrs_str(attrs: Dict[str, Any], key: str) -> Optional[str]:
    val = attrs.get(key)
    if val is None:
        return None
    if isinstance(val, list):
        return str(val[0]) if val else None
    return str(val)


def _decade_of(year: Any) -> Optional[int]:
    try:
        y = int(str(year))
    except (TypeError, ValueError):
        return None
    if y < 1900 or y > 2100:
        return None
    return (y // 10) * 10


def _top(counter: Counter, n: int, total: int) -> List[Dict[str, Any]]:
    return [
        {"name": name, "count": count, "share": round(count / total, 4) if total else 0}
        for name, count in counter.most_common(n)
    ]


async def compute_library_profile(
    session: AsyncSession, item_type: str
) -> Dict[str, Any]:
    """Snapshot the user's added-items distribution for ``item_type``.

    Single pass over IgnoreItem rows where ``added=True``. Returns a dict
    safe to JSON-encode and drop into the user prompt.
    """
    result = await session.execute(
        select(IgnoreItem).where(
            IgnoreItem.item_type == item_type, IgnoreItem.added.is_(True)
        )
    )
    rows = list(result.scalars())
    total = len(rows)

    genres = Counter()
    languages = Counter()
    decades = Counter()
    networks = Counter()  # tv-relevant
    studios = Counter()  # mv-relevant
    directors = Counter()  # mv-relevant

    for r in rows:
        attrs = r.attributes or {}
        for g in _attrs_list(attrs, "genres"):
            genres[g] += 1
        for lang in _attrs_list(attrs, "originalLanguage"):
            languages[lang] += 1
        d = _decade_of(attrs.get("year"))
        if d is not None:
            decades[d] += 1
        # Studio is mv-side; network is tv-side. Plenty of rows have both
        # populated though, so count separately.
        for s in _attrs_list(attrs, "studio"):
            studios[s] += 1
        for n in _attrs_list(attrs, "network"):
            networks[n] += 1
        director = _attrs_str(attrs, "director")
        if director:
            directors[director] += 1

    profile: Dict[str, Any] = {
        "item_type": item_type,
        "total_added": total,
        "top_genres": _top(genres, TOP_GENRES, total),
        "top_languages": _top(languages, TOP_LANGUAGES, total),
        "decade_distribution": [
            {
                "decade": dec,
                "count": cnt,
                "share": round(cnt / total, 4) if total else 0,
            }
            for dec, cnt in sorted(decades.items(), reverse=True)
        ],
    }
    if item_type == "mv":
        profile["top_studios"] = _top(studios, TOP_STUDIOS, total)
        profile["top_directors"] = _top(directors, TOP_DIRECTORS, total)
    else:
        profile["top_networks"] = _top(networks, TOP_NETWORKS, total)
    return profile


def _rank_in(distribution: Sequence[Dict[str, Any]], name: str) -> Optional[int]:
    """1-indexed rank of ``name`` within an ordered top-N list."""
    target = name.strip().lower()
    for i, entry in enumerate(distribution, 1):
        if entry["name"].strip().lower() == target:
            return i
    return None


def compute_candidate_match(
    profile: Dict[str, Any], attrs: Dict[str, Any]
) -> Dict[str, Any]:
    """Resolve the candidate's position within each library distribution.

    The model reads this instead of running its own counting via tools.
    Anything not in the top-N is reported with ``rank=null`` and the
    matching list count so the model can tell "unranked but exists" from
    "absent entirely."
    """
    out: Dict[str, Any] = {}

    genres = _attrs_list(attrs, "genres")
    out["genres"] = [
        {
            "name": g,
            "rank": _rank_in(profile["top_genres"], g),
            "top_n": len(profile["top_genres"]),
        }
        for g in genres
    ]

    languages = _attrs_list(attrs, "originalLanguage")
    if languages:
        out["languages"] = [
            {
                "name": lang,
                "rank": _rank_in(profile["top_languages"], lang),
                "top_n": len(profile["top_languages"]),
            }
            for lang in languages
        ]

    decade = _decade_of(attrs.get("year"))
    if decade is not None:
        # decade_distribution isn't ranked by frequency, it's chronological,
        # so report share + chronological neighbours instead of rank.
        share = next(
            (
                entry["share"]
                for entry in profile["decade_distribution"]
                if entry["decade"] == decade
            ),
            0.0,
        )
        out["decade"] = {"decade": decade, "share_of_added": share}

    if profile["item_type"] == "mv":
        for s in _attrs_list(attrs, "studio"):
            out.setdefault("studios", []).append(
                {
                    "name": s,
                    "rank": _rank_in(profile["top_studios"], s),
                    "top_n": len(profile["top_studios"]),
                }
            )
        director = _attrs_str(attrs, "director")
        if director:
            out["director"] = {
                "name": director,
                "rank": _rank_in(profile["top_directors"], director),
                "top_n": len(profile["top_directors"]),
            }
    else:
        for n in _attrs_list(attrs, "network"):
            out.setdefault("networks", []).append(
                {
                    "name": n,
                    "rank": _rank_in(profile["top_networks"], n),
                    "top_n": len(profile["top_networks"]),
                }
            )

    return out
