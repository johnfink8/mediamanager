from typing import List, Optional

import requests
from decouple import config


def _auth_headers() -> dict:
    api_key = config("TMDB_API_KEY")
    return {
        "accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def get_movie_id(imdb_id: str) -> Optional[str]:
    url = f"https://api.themoviedb.org/3/find/{imdb_id}?external_source=imdb_id"
    response = requests.get(url, headers=_auth_headers())
    results = response.json()["movie_results"]
    if results:
        return results[0]["id"]
    return None


def get_movie_cast(movie_id: int, n: int = 10) -> List[str]:
    url = f"https://api.themoviedb.org/3/movie/{movie_id}/credits?language=en-US"
    response = requests.get(url, headers=_auth_headers())
    response = response.json()
    if not response.get("cast"):
        print("cast not found", response, movie_id)
    return [cast["name"] for cast in response["cast"][:n]]


def get_movie_director(movie_id: int) -> Optional[str]:
    """Return the primary director name for a TMDB movie, or None.

    For multi-director films, returns the first Director credit (TMDB
    typically lists co-directors in alphabetical order).
    """
    url = f"https://api.themoviedb.org/3/movie/{movie_id}/credits?language=en-US"
    response = requests.get(url, headers=_auth_headers())
    crew = response.json().get("crew") or []
    for member in crew:
        if member.get("job") == "Director":
            name = member.get("name")
            if name:
                return str(name)
    return None


def get_movie_release_count(movie_id: int) -> int:
    url = f"https://api.themoviedb.org/3/movie/{movie_id}/release_dates?language=en-US"
    response = requests.get(url, headers=_auth_headers())
    response = response.json()
    return len(response.get("results", []))


def get_tv_id(tvdb_id: str) -> Optional[str]:
    url = f"https://api.themoviedb.org/3/find/{tvdb_id}?external_source=tvdb_id"
    response = requests.get(url, headers=_auth_headers())
    results = response.json().get("tv_results", [])
    if results:
        return results[0]["id"]
    return None


def get_tv_cast(tv_id: int, n: int = 10) -> List[str]:
    url = f"https://api.themoviedb.org/3/tv/{tv_id}/credits?language=en-US"
    response = requests.get(url, headers=_auth_headers())
    response = response.json()
    if not response.get("cast"):
        print("cast not found", response, tv_id)
    return [cast["name"] for cast in response["cast"][:n]]
