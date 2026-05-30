import asyncio
import json
from typing import Any, Dict, List, Optional, cast

import requests
from decouple import config
from requests.auth import HTTPBasicAuth


def radarr_query(cmd: str, method: str = "get", **kwargs) -> List[Dict[str, Any]]:
    session = requests.Session()
    session.headers["X-Api-Key"] = config("RADARR_APIKEY")
    url = "/".join([config("RADARR_URL"), cmd])
    auth = None
    if config("RADARR_USERNAME", None):
        auth = HTTPBasicAuth(config("RADARR_USERNAME"), config("RADARR_PASSWORD"))
    if method == "get":
        resp = session.get(
            url,
            params=kwargs,
            auth=auth,
        )
    else:
        method_func = getattr(session, method)
        resp = method_func(
            url,
            json=kwargs,
            auth=auth,
        )
    txt = resp.text
    return json.loads(txt)


MOVIES: List[Dict[str, object]] = []
_movies_lock: Optional[asyncio.Lock] = None


def _get_movies_lock() -> asyncio.Lock:
    global _movies_lock
    if _movies_lock is None:
        _movies_lock = asyncio.Lock()
    return _movies_lock


def reset_movies() -> None:
    del MOVIES[:]


def get_movie(imdbId: str) -> Optional[Dict[str, object]]:
    if not MOVIES:
        MOVIES.extend(cast(List[Dict[str, object]], radarr_query("movie")))

    for movie in MOVIES:
        if "imdbId" not in movie:
            continue
        if movie["imdbId"] == imdbId:
            return movie
    return None


async def aget_movie(imdbId: str) -> Optional[Dict[str, object]]:
    async with _get_movies_lock():
        if not MOVIES:
            fetched = await asyncio.to_thread(radarr_query, "movie")
            MOVIES.extend(cast(List[Dict[str, object]], fetched))
    for movie in MOVIES:
        if movie.get("imdbId") == imdbId:
            return movie
    return None


async def aradarr_query(
    cmd: str, method: str = "get", **kwargs: Any
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(radarr_query, cmd, method, **kwargs)


async def aredownload_by_imdb(imdb_id: str) -> Dict[str, Any]:
    """Delete a movie's file (if present) and trigger a fresh Radarr search.

    Radarr's own tools only re-search; this adds the delete-and-research used
    when a download is corrupt and re-grabbing the same release won't help.
    """
    movie: Any = await aget_movie(imdb_id)
    if not movie:
        raise ValueError(f"no Radarr movie for imdb {imdb_id}")
    movie_id = movie.get("id")
    movie_file: Any = movie.get("movieFile") or {}
    file_id = movie_file.get("id")
    deleted = False
    if file_id:
        await aradarr_query(f"moviefile/{file_id}", method="delete")
        deleted = True
    await aradarr_query(
        "command", method="post", name="MoviesSearch", movieIds=[movie_id]
    )
    # The cached movie list now has a stale movieFile/hasFile for this title.
    reset_movies()
    return {"movie_id": movie_id, "deleted_old_file": deleted, "status": "searching"}


async def aupgrade_movie(
    movie_id: int, quality_profile_id: Optional[int] = None
) -> Dict[str, Any]:
    """Switch a movie's quality profile (optional) and trigger a Radarr search."""
    movie: Any = await aradarr_query(f"movie/{movie_id}")
    if quality_profile_id is not None:
        movie["qualityProfileId"] = quality_profile_id
        movie["monitored"] = True
        await aradarr_query(f"movie/{movie_id}", method="put", **movie)
    await aradarr_query(
        "command", method="post", name="MoviesSearch", movieIds=[movie_id]
    )
    return {
        "movie_id": movie_id,
        "quality_profile_id": movie.get("qualityProfileId"),
        "status": "searching",
    }


async def aupgrade_by_imdb(
    imdb_id: str, quality_profile_id: Optional[int] = None
) -> Dict[str, Any]:
    """Resolve an imdb id to a Radarr movie and upgrade its quality."""
    movie: Any = await aget_movie(imdb_id)
    if not movie:
        raise ValueError(f"no Radarr movie for imdb {imdb_id}")
    return await aupgrade_movie(movie["id"], quality_profile_id)
