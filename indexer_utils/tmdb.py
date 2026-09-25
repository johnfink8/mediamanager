from typing import Any, Dict, List, Optional

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


def get_credit_person_ids(item_type: str, tmdb_id: int) -> Dict[str, int]:
    """Lower-cased name → TMDB person id for a title's cast and directors.

    Resolving people through the title's own credits avoids the namesake
    collisions a name search can hit.
    """
    path = (
        f"movie/{tmdb_id}/credits"
        if item_type == "mv"
        else f"tv/{tmdb_id}/aggregate_credits"
    )
    url = f"https://api.themoviedb.org/3/{path}?language=en-US"
    data = requests.get(url, headers=_auth_headers(), timeout=20).json()
    out: Dict[str, int] = {}
    for person in data.get("cast") or []:
        out.setdefault(str(person.get("name") or "").strip().lower(), person["id"])
    for person in data.get("crew") or []:
        # movie credits carry ``job``; tv aggregate credits carry ``jobs``.
        jobs = {person.get("job")} | {
            j.get("job") for j in person.get("jobs") or [] if isinstance(j, dict)
        }
        if "Director" in jobs:
            out.setdefault(str(person.get("name") or "").strip().lower(), person["id"])
    out.pop("", None)
    return out


def search_person_id(name: str) -> Optional[int]:
    """Most popular TMDB person matching ``name`` exactly, or None."""
    url = "https://api.themoviedb.org/3/search/person"
    data = requests.get(
        url,
        headers=_auth_headers(),
        params={"query": name, "language": "en-US"},
        timeout=20,
    ).json()
    for person in data.get("results") or []:
        if str(person.get("name") or "").strip().lower() == name.strip().lower():
            return int(person["id"])
    return None


def get_person_combined_credits(person_id: int) -> Dict[str, Any]:
    """TMDB ``/person/{id}/combined_credits``: movie + tv, cast + crew."""
    url = (
        f"https://api.themoviedb.org/3/person/{person_id}/combined_credits"
        "?language=en-US"
    )
    data: Dict[str, Any] = requests.get(url, headers=_auth_headers(), timeout=20).json()
    return data
