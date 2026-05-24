"""Integration tests for ``build_filter_clauses``.

Each filter is layered onto a real ``SELECT IgnoreItem`` against the test
postgres and we assert which uids come back. That catches both wrong-key
bugs (filter targets the wrong attrs path) and wrong-SQL bugs (regex
guard missing, operator inverted) — far stronger than inspecting
compiled SQL strings.
"""

from typing import Iterable, List

import pytest_asyncio
from sqlalchemy import select

from indexer_utils.ai_tools.shared import build_filter_clauses
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session


@pytest_asyncio.fixture
async def session():
    async with db_session() as s:
        yield s


def _item(uid: str, **attrs) -> IgnoreItem:
    return IgnoreItem(
        item_type="mv",
        uid=uid,
        title=uid,
        ignore=True,
        added=True,
        shown=True,
        attributes=attrs,
    )


async def _seed(session, items: Iterable[IgnoreItem]) -> None:
    for item in items:
        session.add(item)
    await session.commit()


async def _uids_matching(session, filters: dict) -> set:
    stmt = select(IgnoreItem.uid)
    for clause in build_filter_clauses(filters):
        stmt = stmt.where(clause)
    rows = (await session.execute(stmt)).all()
    return {uid for (uid,) in rows}


async def test_empty_filters_returns_everything(session) -> None:
    await _seed(session, [_item("a"), _item("b"), _item("c")])
    assert await _uids_matching(session, {}) == {"a", "b", "c"}


async def test_year_min_filters_non_numeric_silently(session) -> None:
    """``year`` is stored as int, list, or string across the corpus; the
    regex guard must let non-numeric values fall out instead of raising."""
    await _seed(
        session,
        [
            _item("y2015", year=2015),
            _item("y2020", year=2020),
            _item("y2025", year=2025),
            _item("ylist", year=[2024]),  # legacy list shape
            _item("ynone"),  # no year at all
        ],
    )
    assert await _uids_matching(session, {"year_min": 2020}) == {"y2020", "y2025"}
    # year_max uses the same guard.
    assert await _uids_matching(session, {"year_max": 2020}) == {"y2015", "y2020"}


async def test_year_min_and_max_intersect(session) -> None:
    await _seed(
        session,
        [
            _item("y2015", year=2015),
            _item("y2020", year=2020),
            _item("y2025", year=2025),
        ],
    )
    assert await _uids_matching(session, {"year_min": 2018, "year_max": 2022}) == {
        "y2020"
    }


async def test_language_substring_case_insensitive(session) -> None:
    """``language`` does an ILIKE substring against ``originalLanguage`` —
    matches both the bare-string and list-shaped storage formats."""
    await _seed(
        session,
        [
            _item("en", originalLanguage="English"),
            _item("en_list", originalLanguage=["English"]),
            _item("es", originalLanguage="Spanish"),
            _item("none"),
        ],
    )
    assert await _uids_matching(session, {"language": "english"}) == {
        "en",
        "en_list",
    }


async def test_blank_language_is_ignored(session) -> None:
    """A whitespace-only filter value is dropped, not applied as ``ILIKE %%``."""
    await _seed(session, [_item("a", originalLanguage="English"), _item("b")])
    assert await _uids_matching(session, {"language": "   "}) == {"a", "b"}


async def test_runtime_min_and_max(session) -> None:
    await _seed(
        session,
        [
            _item("short", runtime=70),
            _item("normal", runtime=110),
            _item("long", runtime=180),
            _item("none"),
        ],
    )
    assert await _uids_matching(session, {"runtime_min": 80, "runtime_max": 150}) == {
        "normal"
    }


async def test_rating_min_uses_rating_value(session) -> None:
    await _seed(
        session,
        [
            _item("low", rating_value=6.0),
            _item("mid", rating_value=7.5),
            _item("high", rating_value=9.0),
        ],
    )
    assert await _uids_matching(session, {"rating_min": 7.5}) == {"mid", "high"}


async def test_per_source_rating_filters_route_correctly(session) -> None:
    """Each per-source filter must hit its own ``*user_value`` key. A row
    with only TMDB data shouldn't accidentally satisfy ``imdb_min``."""
    await _seed(
        session,
        [
            _item("imdb_only", imdbuser_value=8.0, imdbuser_votes=12000),
            _item("tmdb_only", tmdbuser_value=8.5, tmdbuser_votes=300),
            _item("trakt_only", traktuser_value=8.2, traktuser_votes=400),
        ],
    )
    assert await _uids_matching(session, {"imdb_min": 7.0}) == {"imdb_only"}
    assert await _uids_matching(session, {"tmdb_min": 7.0}) == {"tmdb_only"}
    assert await _uids_matching(session, {"trakt_min": 7.0}) == {"trakt_only"}
    # The votes_min filter must look at the matching votes column.
    assert await _uids_matching(session, {"imdb_votes_min": 1000}) == {"imdb_only"}


async def test_critic_source_filters(session) -> None:
    await _seed(
        session,
        [
            _item("rt_high", rottenTomatoesuser_value=92),
            _item("rt_low", rottenTomatoesuser_value=40),
            _item("mc_high", metacriticuser_value=85),
        ],
    )
    assert await _uids_matching(session, {"rt_min": 80}) == {"rt_high"}
    assert await _uids_matching(session, {"metacritic_min": 80}) == {"mc_high"}


async def test_filters_compose_with_AND_semantics(session) -> None:
    """Multiple filters AND together; a row must satisfy all of them."""
    await _seed(
        session,
        [
            _item("ok", year=2022, imdbuser_value=8.0),
            _item("low_imdb", year=2022, imdbuser_value=6.0),
            _item("old", year=2010, imdbuser_value=8.0),
        ],
    )
    assert await _uids_matching(session, {"year_min": 2020, "imdb_min": 7.0}) == {"ok"}


async def test_missing_data_fails_numeric_filter(session) -> None:
    """``row_passes_filters`` and ``build_filter_clauses`` agree: a filter
    that names a field treats a missing value as a fail (not as a pass)."""
    await _seed(
        session,
        [
            _item("has_rating", rating_value=7.5),
            _item("no_rating"),
        ],
    )
    assert await _uids_matching(session, {"rating_min": 5.0}) == {"has_rating"}


async def test_filter_signatures_smoke_compile() -> None:
    """Sanity: every filter key the agent tool exposes produces a valid
    clause (compile reaches a SQL string). Catches typos in the key names
    that would otherwise only surface at runtime when an agent picks an
    unusual filter."""
    keys = [
        "language",
        "director",
        "runtime_min",
        "runtime_max",
        "rating_min",
        "rating_votes_min",
        "imdb_min",
        "imdb_votes_min",
        "tmdb_min",
        "tmdb_votes_min",
        "trakt_min",
        "trakt_votes_min",
        "rt_min",
        "metacritic_min",
        "year_min",
        "year_max",
    ]
    filters = {k: ("English" if k in ("language", "director") else 1) for k in keys}
    clauses: List = build_filter_clauses(filters)
    assert len(clauses) == len(keys)
    for c in clauses:
        # ``str(clause)`` triggers compilation; raises if the clause is bad.
        assert str(c)
