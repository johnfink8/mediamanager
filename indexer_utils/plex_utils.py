import asyncio
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote_plus

import requests
from decouple import config


def _plex_headers() -> Dict[str, str]:
    return {
        "X-Plex-Token": config("PLEX_TOKEN"),
        "Accept": "application/json",
    }


def find_movie(title: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    url = config("PLEX_URL")
    headers = _plex_headers()
    r = requests.get(
        url + "/search?query=%s" % quote_plus(title.encode(errors="ignore")),
        headers=headers,
    )
    results = r.json()
    if "Metadata" not in results["MediaContainer"]:
        return None
    for movie in results["MediaContainer"]["Metadata"]:
        if movie["title"].lower() == title.lower():
            if year is None or movie["year"] == year:
                return movie
    return None


def get_recently_played(limit: int = 40) -> List[Dict[str, Any]]:
    """Fetch recent play history for movies from Plex.

    Plex's ``/status/sessions/history/all`` endpoint ignores ``maxResults``
    and returns the entire history (potentially thousands of entries) in
    no guaranteed order. Sort by ``viewedAt`` descending and slice
    client-side so callers actually get the most-recent ``limit`` plays.
    """

    url = config("PLEX_URL")
    headers = _plex_headers()
    params = {"maxResults": limit, "metadataItemType": 1}
    response = requests.get(
        f"{url}/status/sessions/history/all",
        headers=headers,
        params=params,
    )
    response.raise_for_status()
    payload = response.json()
    metadata = payload.get("MediaContainer", {}).get("Metadata", []) or []

    def _viewed_at(entry: Dict[str, Any]) -> int:
        try:
            return int(entry.get("viewedAt") or 0)
        except (TypeError, ValueError):
            return 0

    metadata.sort(key=_viewed_at, reverse=True)
    return metadata[:limit]


def _extract_imdb_from_guid(guid_value: str) -> Optional[str]:
    if not guid_value:
        return None
    guid_value = str(guid_value)
    if "tt" not in guid_value:
        return None
    idx = guid_value.index("tt")
    imdb_id = guid_value[idx:]
    imdb_id = imdb_id.split("?")[0]
    imdb_id = imdb_id.strip()
    return imdb_id or None


_PLEX_DETAIL_FIELDS = (
    "viewCount",
    "lastViewedAt",
    "audienceRating",
    "rating",
    "userRating",
    "addedAt",
    "summary",
    "duration",
    "contentRating",
)


def get_plex_details(
    title: str, year: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Look up a movie in Plex and return a slim dict with view/rating fields.

    Returns None when not found or on error.
    """
    try:
        movie = find_movie(title, year)
    except Exception:
        return None
    if not movie:
        return None
    return {key: movie.get(key) for key in _PLEX_DETAIL_FIELDS if key in movie}


async def aget_plex_details(
    title: str, year: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(get_plex_details, title, year)


async def aget_recently_played(limit: int = 40) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(get_recently_played, limit)


def get_recently_played_imdb_ids(limit: int = 40) -> Set[str]:
    metadata = get_recently_played(limit=limit)
    recent_ids: Set[str] = set()
    for entry in metadata:
        guid = entry.get("guid")
        imdb_id = _extract_imdb_from_guid(guid)
        if imdb_id:
            recent_ids.add(imdb_id)
            continue
        guid_list = entry.get("Guid") or []
        if isinstance(guid_list, list):
            for guid_entry in guid_list:
                imdb_id = _extract_imdb_from_guid(guid_entry.get("id"))
                if imdb_id:
                    recent_ids.add(imdb_id)
                    break
    return recent_ids
