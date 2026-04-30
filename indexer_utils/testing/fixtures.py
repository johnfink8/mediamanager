"""Canned external-service responses for the simulation harness.

The XML feeds, Radarr lookup payloads, Sonarr series payloads, and seed catalog
all use synthetic ids (``tt8000xxx`` / ``tt9999xxx``, tvdb ``8000xxx`` /
``9990xxx``) that should not collide with anything in a real library. Edit the
lists/maps to extend the fixture set.

The seed catalog (``SEED_MOVIES``, ``SEED_SHOWS``) gives the agent a realistic
taste signal during a simulation run: a small number of items the user added,
a larger number they ignored, and a few that haven't been classified yet. Each
seed item carries a hand-written synopsis used for both the DB row's
``ai.synopsis`` and the Weaviate vector store, so ``search_similar_by_synopsis``
returns plausible neighbors.
"""

from pathlib import Path
from typing import Any, Dict, List

_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def load_movies_feed_xml() -> str:
    return (_FIXTURES_DIR / "movies_feed.xml").read_text()


def load_shows_feed_xml() -> str:
    return (_FIXTURES_DIR / "shows_feed.xml").read_text()


# Radarr ``movie/lookup`` keyed by IMDB id (with the ``tt`` prefix).
RADARR_LOOKUPS: Dict[str, Dict[str, Any]] = {
    "tt9999991": {
        "title": "Crimson Tide Below Zero",
        "year": 2024,
        "originalLanguage": {"name": "English"},
        "status": "released",
        "genres": ["Action", "Thriller"],
        "ratings": {
            "imdb": {"votes": 1500, "value": 5.8, "type": "user"},
            "tmdb": {"votes": 200, "value": 6.0, "type": "user"},
        },
        "remotePoster": "https://example.com/posters/9999991.jpg",
        "imdbId": "tt9999991",
        "tmdbId": 99001,
        "overview": (
            "A submarine crew uncovers a buried Cold War relic beneath an "
            "ice shelf, forcing a fragile alliance with their adversaries."
        ),
    },
    "tt9999992": {
        "title": "The Quiet Fox",
        "year": 2024,
        "originalLanguage": {"name": "French"},
        "status": "released",
        "genres": ["Drama"],
        "ratings": {
            "imdb": {"votes": 4200, "value": 7.6, "type": "user"},
            "tmdb": {"votes": 980, "value": 7.8, "type": "user"},
            "rottenTomatoes": {"votes": 85, "value": 92, "type": "user"},
        },
        "remotePoster": "https://example.com/posters/9999992.jpg",
        "imdbId": "tt9999992",
        "tmdbId": 99002,
        "overview": (
            "A small-town veterinarian recounts a year of grief, a missing "
            "father, and the elusive fox that keeps appearing in her garden."
        ),
    },
    "tt9999993": {
        "title": "Galactic Brunch",
        "year": 2024,
        "originalLanguage": {"name": "English"},
        "status": "released",
        "genres": ["Science Fiction", "Comedy"],
        "ratings": {
            "imdb": {"votes": 3100, "value": 6.4, "type": "user"},
            "tmdb": {"votes": 540, "value": 6.7, "type": "user"},
        },
        "remotePoster": "https://example.com/posters/9999993.jpg",
        "imdbId": "tt9999993",
        "tmdbId": 99003,
        "overview": (
            "A washed-up chef on a derelict space station accidentally "
            "becomes the most influential restaurateur in the quadrant."
        ),
    },
    "tt9999994": {
        "title": "Nightshade Cartographer",
        "year": 2023,
        "originalLanguage": {"name": "English"},
        "status": "released",
        "genres": ["Mystery", "Thriller", "Horror"],
        "ratings": {
            "imdb": {"votes": 720, "value": 6.9, "type": "user"},
            "tmdb": {"votes": 110, "value": 7.1, "type": "user"},
        },
        "remotePoster": "https://example.com/posters/9999994.jpg",
        "imdbId": "tt9999994",
        "tmdbId": 99004,
        "overview": (
            "A reclusive mapmaker discovers that the towns she charts begin "
            "vanishing the moment she completes their borders."
        ),
    },
}


# ---------------------------------------------------------------------------
# Seed catalog — pre-populated into the simulation's SQLite DB and Weaviate
# ``_sim`` classes. Mix of added / ignored / undecided to give realistic signal.
# ---------------------------------------------------------------------------

SEED_MOVIES: List[Dict[str, Any]] = [
    # --- ADDED + IN PLEX with engagement (strongest positive signal) ---
    {
        "uid": "tt8000001",
        "title": "Glacier Point",
        "added": True,
        "ignore": False,
        "attributes": {
            "genres": ["Action", "Thriller"],
            "year": 2022,
            "originalLanguage": ["English"],
            "rating_value": 7.4,
            "rating_votes": 38000,
            "cast": ["Mads Mikkelsen", "Florence Pugh"],
        },
        "synopsis": (
            "A coast-guard pilot must navigate political intrigue and personal "
            "stakes during an Arctic ice-storm rescue when the survivors turn "
            "out to be carrying a stolen weapons cache."
        ),
        "plex": {
            "viewCount": 3,
            "lastViewedAt": 1744675200,  # 2026-04-15
            "audienceRating": 8.2,
            "userRating": 9.0,
            "addedAt": 1727740800,  # 2025-10-01
        },
    },
    {
        "uid": "tt8000002",
        "title": "The Ventriloquist's Daughter",
        "added": True,
        "ignore": False,
        "attributes": {
            "genres": ["Drama", "Mystery"],
            "year": 2021,
            "originalLanguage": ["French"],
            "rating_value": 7.9,
            "rating_votes": 9200,
            "cast": ["Adèle Exarchopoulos"],
        },
        "synopsis": (
            "A grieving woman returns to her childhood village to settle her "
            "father's estate and confronts the truth behind a decades-old "
            "disappearance, told through quiet domestic detail."
        ),
        "plex": {
            "viewCount": 1,
            "lastViewedAt": 1740787200,  # 2026-03-01
            "audienceRating": 7.8,
            "userRating": 8.0,
            "addedAt": 1735689600,  # 2026-01-01
        },
    },
    {
        "uid": "tt8000003",
        "title": "Edge of Orion",
        "added": True,
        "ignore": False,
        "attributes": {
            "genres": ["Science Fiction", "Adventure"],
            "year": 2023,
            "originalLanguage": ["English"],
            "rating_value": 7.6,
            "rating_votes": 145000,
            "cast": ["Pedro Pascal", "Tessa Thompson"],
        },
        "synopsis": (
            "An interstellar miner befriends a stowaway A.I. and uncovers a "
            "corporate conspiracy threatening their mining colony. Cerebral, "
            "character-led hard sci-fi with practical effects."
        ),
        "plex": {
            "viewCount": 2,
            "lastViewedAt": 1746057600,  # 2026-05-01
            "audienceRating": 8.0,
            "userRating": 8.5,
            "addedAt": 1740787200,
        },
    },
    # --- ADDED but NOT in Plex (likely deleted — strong negative signal) ---
    {
        "uid": "tt8000004",
        "title": "Bistro Galaxia",
        "added": True,
        "ignore": False,
        "attributes": {
            "genres": ["Science Fiction", "Comedy"],
            "year": 2023,
            "originalLanguage": ["English"],
            "rating_value": 7.1,
            "rating_votes": 22000,
        },
        "synopsis": (
            "A frazzled chef opens a tapas bar on a backwater space station "
            "and accidentally ends up catering a galactic peace summit. Warm, "
            "low-stakes ensemble comedy."
        ),
        # No `plex` field — simulates deletion: the user added the item at
        # some point but it is no longer in Plex.
    },
    {
        "uid": "tt8000005",
        "title": "Mapmaker's Lament",
        "added": True,
        "ignore": False,
        "attributes": {
            "genres": ["Mystery", "Drama"],
            "year": 2020,
            "originalLanguage": ["English"],
            "rating_value": 7.8,
            "rating_votes": 5400,
        },
        "synopsis": (
            "A reclusive cartographer realises the hand-drawn maps she sells "
            "to estate agents seem to predict tragedies before they occur."
        ),
        "plex": {
            "viewCount": 4,
            "lastViewedAt": 1743033600,  # 2026-03-27
            "audienceRating": 8.4,
            "userRating": 9.5,
            "addedAt": 1719792000,  # 2025-07-01
        },
    },
    # --- IGNORED (negative signal) ---
    {
        "uid": "tt8000010",
        "title": "Grindhouse Vampires from Outer Space",
        "added": False,
        "ignore": True,
        "attributes": {
            "genres": ["Horror", "Action"],
            "year": 2022,
            "originalLanguage": ["English"],
            "rating_value": 4.1,
            "rating_votes": 2200,
        },
        "synopsis": (
            "Bloodthirsty space vampires invade a roadside diner; one ex-stripper "
            "must save the day with a chainsaw. Deliberate B-movie schlock."
        ),
    },
    {
        "uid": "tt8000011",
        "title": "Reality Date Show: Wedding Edition",
        "added": False,
        "ignore": True,
        "attributes": {
            "genres": ["Reality"],
            "year": 2024,
            "originalLanguage": ["English"],
            "rating_value": 3.2,
            "rating_votes": 800,
        },
        "synopsis": (
            "Twelve singles compete for a televised wedding by completing "
            "humiliating physical and emotional challenges over six episodes."
        ),
    },
    {
        "uid": "tt8000012",
        "title": "Dragon Mecha Rampage",
        "added": False,
        "ignore": True,
        "attributes": {
            "genres": ["Animation", "Action"],
            "year": 2023,
            "originalLanguage": ["Japanese"],
            "rating_value": 5.4,
            "rating_votes": 4100,
        },
        "synopsis": (
            "Giant mecha pilots fight kaiju across post-apocalyptic Tokyo. "
            "Loud, visually busy, and aimed squarely at teen mecha fans."
        ),
    },
    {
        "uid": "tt8000013",
        "title": "Crimson Maw 4: The Reckoning",
        "added": False,
        "ignore": True,
        "attributes": {
            "genres": ["Horror"],
            "year": 2023,
            "originalLanguage": ["English"],
            "rating_value": 4.8,
            "rating_votes": 1500,
        },
        "synopsis": (
            "The fourth chapter of the cult slasher franchise about a haunted "
            "mineshaft and the masked killer who returns every winter solstice."
        ),
    },
    {
        "uid": "tt8000014",
        "title": "Operation Skyhammer",
        "added": False,
        "ignore": True,
        "attributes": {
            "genres": ["Action"],
            "year": 2024,
            "originalLanguage": ["English"],
            "rating_value": 4.6,
            "rating_votes": 980,
        },
        "synopsis": (
            "A black-ops team parachutes into hostile territory to stop a "
            "rogue arms dealer. Direct-to-streaming, thinly written, set-piece "
            "driven action."
        ),
    },
    # --- UNDECIDED (shown but not yet classified) ---
    {
        "uid": "tt8000020",
        "title": "Lighthouse on the Wash",
        "added": False,
        "ignore": False,
        "attributes": {
            "genres": ["Drama"],
            "year": 2024,
            "originalLanguage": ["English"],
            "rating_value": 7.3,
            "rating_votes": 1100,
        },
        "synopsis": (
            "A lighthouse keeper and her estranged daughter face their shared "
            "past during a winter storm on the Norfolk coast."
        ),
    },
    {
        "uid": "tt8000021",
        "title": "Quantum Errand",
        "added": False,
        "ignore": False,
        "attributes": {
            "genres": ["Science Fiction", "Comedy"],
            "year": 2024,
            "originalLanguage": ["English"],
            "rating_value": 6.7,
            "rating_votes": 3300,
        },
        "synopsis": (
            "An office worker accidentally activates a time-loop bracelet and "
            "uses it to win a corporate raffle, slowly realising the cost."
        ),
    },
]


SEED_SHOWS: List[Dict[str, Any]] = [
    # --- ADDED (positive signal) ---
    {
        "uid": "8000101",
        "title": "Cartographers of Britain",
        "added": True,
        "ignore": False,
        "attributes": {
            "genres": ["Drama", "Mystery"],
            "year": 2023,
            "network": ["BBC"],
            "originalLanguage": "English",
            "rating_value": 8.1,
            "rating_votes": 14000,
            "seriesType": "standard",
        },
        "synopsis": (
            "Each season follows a different historical mapmaker investigating "
            "a mystery within their charted territory, anchored by quiet "
            "character work and BBC-grade period detail."
        ),
    },
    {
        "uid": "8000102",
        "title": "Tokyo Wires",
        "added": True,
        "ignore": False,
        "attributes": {
            "genres": ["Animation", "Action", "Cyberpunk"],
            "year": 2024,
            "network": ["Crunchyroll"],
            "originalLanguage": "Japanese",
            "rating_value": 8.4,
            "rating_votes": 28000,
            "seriesType": "anime",
        },
        "synopsis": (
            "A streetwise tech smuggler navigates the underground markets of "
            "a near-future Neo-Tokyo while dodging a corporate-funded vigilante."
        ),
    },
    {
        "uid": "8000103",
        "title": "Village Practitioners",
        "added": True,
        "ignore": False,
        "attributes": {
            "genres": ["Drama"],
            "year": 2022,
            "network": ["ITV"],
            "originalLanguage": "English",
            "rating_value": 7.6,
            "rating_votes": 6800,
            "seriesType": "standard",
        },
        "synopsis": (
            "An ensemble of small-town doctors balances bedside manner with "
            "professional rivalries in rural Yorkshire."
        ),
    },
    # --- IGNORED (negative signal) ---
    {
        "uid": "8000110",
        "title": "Reality Cooking Showdown UK",
        "added": False,
        "ignore": True,
        "attributes": {
            "genres": ["Reality"],
            "year": 2023,
            "network": ["Channel 4"],
            "originalLanguage": "English",
            "rating_value": 5.1,
            "rating_votes": 1200,
        },
        "synopsis": (
            "Aspiring chefs compete in absurd cooking challenges judged by "
            "celebrity guests. Bright, fast-cut, and formulaic."
        ),
    },
    {
        "uid": "8000111",
        "title": "Vampire High School Romance",
        "added": False,
        "ignore": True,
        "attributes": {
            "genres": ["Animation", "Romance"],
            "year": 2024,
            "network": ["Crunchyroll"],
            "originalLanguage": "Japanese",
            "rating_value": 6.0,
            "rating_votes": 3400,
            "seriesType": "anime",
        },
        "synopsis": (
            "A teenage vampire navigates first love at a mysterious boarding "
            "school full of supernatural classmates."
        ),
    },
    {
        "uid": "8000112",
        "title": "Crime Boss: Marbella",
        "added": False,
        "ignore": True,
        "attributes": {
            "genres": ["Crime", "Drama"],
            "year": 2023,
            "network": ["Antena 3"],
            "originalLanguage": "Spanish",
            "rating_value": 6.8,
            "rating_votes": 2900,
        },
        "synopsis": (
            "A retired enforcer is pulled back into Mediterranean organised "
            "crime when his daughter's wedding is sabotaged."
        ),
    },
    # --- UNDECIDED ---
    {
        "uid": "8000120",
        "title": "Bridgehead",
        "added": False,
        "ignore": False,
        "attributes": {
            "genres": ["Drama", "Thriller"],
            "year": 2024,
            "network": ["HBO"],
            "originalLanguage": "English",
            "rating_value": 8.0,
            "rating_votes": 4400,
        },
        "synopsis": (
            "A military translator arranges secret evacuations from a "
            "collapsing capital, weighing personal loyalties against the lives "
            "of strangers."
        ),
    },
]


# Sonarr ``series/lookup`` keyed by tvdb id (string form).
SONARR_LOOKUPS: Dict[str, Dict[str, Any]] = {
    "9990001": {
        "title": "The Last Cartographer",
        "year": 2024,
        "tmdbId": 88001,
        "ratings": {"votes": 540, "value": 7.4},
        "network": "BBC",
        "genres": ["Drama", "Mystery"],
        "status": "continuing",
        "seriesType": "standard",
        "certification": "TV-14",
        "originalLanguage": "English",
    },
    "9990002": {
        "title": "Neon Bazaar",
        "year": 2024,
        "tmdbId": 88002,
        "ratings": {"votes": 1820, "value": 8.3},
        "network": "Crunchyroll",
        "genres": ["Animation", "Action", "Cyberpunk"],
        "status": "continuing",
        "seriesType": "anime",
        "certification": "TV-MA",
        "originalLanguage": "Japanese",
    },
    "9990003": {
        "title": "Quiet Country Doctors",
        "year": 2024,
        "tmdbId": 88003,
        "ratings": {"votes": 230, "value": 6.8},
        "network": "ITV",
        "genres": ["Drama"],
        "status": "continuing",
        "seriesType": "standard",
        "certification": "TV-PG",
        "originalLanguage": "English",
    },
}
