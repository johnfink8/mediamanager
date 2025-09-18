from typing import List, Optional

import requests
from decouple import config

TMDB_API_KEY = config("TMDB_API_KEY")
HEADERS = {"accept": "application/json", "Authorization": f"Bearer {TMDB_API_KEY}"}


def get_movie_id(imdb_id: str) -> Optional[str]:
    url = f"https://api.themoviedb.org/3/find/{imdb_id}?external_source=imdb_id"
    response = requests.get(url, headers=HEADERS)
    results = response.json()["movie_results"]
    if results:
        return results[0]["id"]
    return None


def get_movie_cast(movie_id: int, n: int = 10) -> List[str]:
    url = f"https://api.themoviedb.org/3/movie/{movie_id}/credits?language=en-US"
    response = requests.get(url, headers=HEADERS)
    response = response.json()
    if not response.get("cast"):
        print("cast not found", response, movie_id)
    return [cast["name"] for cast in response["cast"][:n]]
