import asyncio
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote_plus

import requests
from decouple import config

logger = logging.getLogger(__name__)


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
    logger.info("Fetching Plex play history (limit=%d)", limit)
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


def _extract_imdb_from_guid(guid_value: Any) -> Optional[str]:
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


def _extract_tvdb_from_guid(guid_value: Any) -> Optional[str]:
    if not guid_value:
        return None
    text = str(guid_value)
    marker = "tvdb://"
    if marker not in text:
        return None
    rest = text.split(marker, 1)[1]
    rest = rest.split("?")[0].split("/")[0].strip()
    return rest or None


def _extract_ids_from_metadata(
    entry: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(imdb_id, tvdb_id)`` extracted from a Plex metadata entry."""
    imdb_id: Optional[str] = _extract_imdb_from_guid(entry.get("guid"))
    tvdb_id: Optional[str] = _extract_tvdb_from_guid(entry.get("guid"))
    guid_list = entry.get("Guid") or []
    if isinstance(guid_list, list):
        for guid_entry in guid_list:
            value = guid_entry.get("id") if isinstance(guid_entry, dict) else None
            if not imdb_id:
                imdb_id = _extract_imdb_from_guid(value)
            if not tvdb_id:
                tvdb_id = _extract_tvdb_from_guid(value)
            if imdb_id and tvdb_id:
                break
    return imdb_id, tvdb_id


_PLEX_LIBRARY_ATTR_FIELDS = (
    "summary",
    "year",
    "rating",
    "audienceRating",
    "userRating",
    "contentRating",
    "duration",
    "originallyAvailableAt",
    "studio",
    "addedAt",
    "viewCount",
    "lastViewedAt",
)


def _normalize_library_item(
    entry: Dict[str, Any], section_type: str
) -> Optional[Dict[str, Any]]:
    """Convert a Plex library metadata entry into a normalized record.

    Returns ``None`` when no usable external id (IMDB for movies, TVDB for
    shows) can be extracted.
    """
    imdb_id, tvdb_id = _extract_ids_from_metadata(entry)
    if section_type == "movie":
        if not imdb_id:
            return None
        item_type = "mv"
        uid = imdb_id
    elif section_type == "show":
        if not tvdb_id:
            return None
        item_type = "tv"
        uid = tvdb_id
    else:
        return None

    attrs: Dict[str, Any] = {}
    for key in _PLEX_LIBRARY_ATTR_FIELDS:
        if key in entry and entry.get(key) not in (None, ""):
            attrs[key] = entry[key]
    # Plex omits ``viewCount`` from the JSON for items that have never been
    # played to completion. The general loop above drops missing keys, which
    # would conflate "in Plex with zero plays" (a negative engagement signal)
    # with "not in Plex / not yet scanned" (no signal). Default to 0 so the
    # absence of the field cleanly means the latter.
    if "viewCount" not in attrs:
        attrs["viewCount"] = 0
    summary = entry.get("summary")
    if summary:
        attrs["synopsis"] = summary
    genres = entry.get("Genre") or []
    if isinstance(genres, list):
        names = [g.get("tag") for g in genres if isinstance(g, dict) and g.get("tag")]
        if names:
            attrs["genres"] = names
    if imdb_id:
        attrs["imdb_id"] = imdb_id
    if tvdb_id:
        attrs["tvdb_id"] = tvdb_id

    return {
        "item_type": item_type,
        "uid": uid,
        "title": entry.get("title") or "",
        "poster_url": entry.get("thumb"),
        "attributes": attrs,
    }


def iter_plex_library_items() -> Iterable[Dict[str, Any]]:
    """Yield normalized records for every movie/show in Plex libraries."""
    url = config("PLEX_URL")
    headers = _plex_headers()
    sections_resp = requests.get(f"{url}/library/sections", headers=headers)
    sections_resp.raise_for_status()
    directories = (
        sections_resp.json().get("MediaContainer", {}).get("Directory", []) or []
    )
    for section in directories:
        section_type = section.get("type")
        if section_type not in ("movie", "show"):
            continue
        section_id = section.get("key")
        if not section_id:
            continue
        page_resp = requests.get(
            f"{url}/library/sections/{section_id}/all",
            headers=headers,
            params={"includeGuids": 1},
        )
        try:
            page_resp.raise_for_status()
        except Exception:
            logger.exception("Plex library section %s fetch failed", section_id)
            continue
        items = page_resp.json().get("MediaContainer", {}).get("Metadata", []) or []
        for entry in items:
            normalized = _normalize_library_item(entry, section_type)
            if normalized is not None:
                yield normalized


async def scan_and_index_plex_library() -> Dict[str, int]:
    """Scan all Plex libraries and upsert IgnoreItem rows for each item.

    Items found in Plex are marked ``added=True`` and ``ignore=True`` so the
    candidate UI hides them. Plex metadata (including the synopsis) is merged
    into the ``attributes`` JSON without clobbering existing AI fields.
    """
    # Local import to avoid a circular import with ``models`` at module load.
    from sqlalchemy import select

    from .models import IgnoreItem
    from .session import db_session

    created = 0
    updated = 0
    duplicates_seen = 0
    seen = 0
    started = time.monotonic()
    logger.info("Plex library scan starting")
    BATCH = 100
    async with db_session() as session:
        for record in iter_plex_library_items():
            seen += 1
            if seen % 500 == 0:
                logger.info(
                    "Plex library scan progress: %d items scanned (%d new, %d updated)",
                    seen,
                    created,
                    updated,
                )
            # There is no unique constraint on (item_type, uid) so historical
            # races can leave duplicates. Take the lowest-id row as canonical
            # and merge into it; ``.one_or_none()`` would crash here.
            result = await session.execute(
                select(IgnoreItem)
                .filter_by(item_type=record["item_type"], uid=record["uid"])
                .order_by(IgnoreItem.id)
            )
            matches = list(result.scalars())
            if not matches:
                session.add(
                    IgnoreItem(
                        item_type=record["item_type"],
                        uid=record["uid"],
                        title=record["title"],
                        poster_url=record["poster_url"],
                        attributes=record["attributes"],
                        added=True,
                        ignore=True,
                        shown=False,
                    )
                )
                created += 1
            else:
                if len(matches) > 1:
                    duplicates_seen += len(matches) - 1
                    logger.warning(
                        "Duplicate IgnoreItem rows for %s/%s: %d copies (ids=%s)",
                        record["item_type"],
                        record["uid"],
                        len(matches),
                        [m.id for m in matches],
                    )
                existing = matches[0]
                merged = dict(existing.attributes or {})
                merged.update(record["attributes"])
                existing.attributes = merged
                if record["title"] and not existing.title:
                    existing.title = record["title"]
                if record["poster_url"] and not existing.poster_url:
                    existing.poster_url = record["poster_url"]
                existing.added = True
                existing.ignore = True
                updated += 1
            if seen % BATCH == 0:
                await session.commit()
        await session.commit()
    elapsed = time.monotonic() - started
    logger.info(
        "Plex library scan complete in %.1fs: %d items, %d new, %d updated, %d duplicate rows seen",
        elapsed,
        seen,
        created,
        updated,
        duplicates_seen,
    )
    return {"created": created, "updated": updated}


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
