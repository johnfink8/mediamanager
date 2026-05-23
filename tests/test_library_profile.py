"""Tests for ``indexer_utils.library_profile``.

Covers the two functions the recommendation flow depends on:
- ``compute_library_profile`` — counts/shares per genre/studio/network/etc.
  over added items only.
- ``compute_candidate_match`` — overlays the candidate's rank within
  each distribution so the model doesn't have to derive it.
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("OPENAI_API_KEY", "test-library-profile")

from indexer_utils.library_profile import (  # noqa: E402
    compute_candidate_match,
    compute_library_profile,
)
from indexer_utils.models import IgnoreItem  # noqa: E402
from indexer_utils.session import Base  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        yield s


def _add(session, **kwargs):
    defaults = dict(item_type="mv", ignore=True, shown=True, added=True)
    defaults.update(kwargs)
    item = IgnoreItem(**defaults)
    session.add(item)
    session.commit()
    return item


def test_profile_counts_only_added(session) -> None:
    """Rejected and pending items don't contribute to the profile distributions."""
    _add(
        session,
        uid="add-1",
        title="Liked",
        attributes={"genres": ["Drama"], "year": 2020, "studio": "A24"},
    )
    _add(
        session,
        uid="rej-1",
        title="Rejected",
        added=False,
        attributes={"genres": ["Drama"], "year": 2020, "studio": "A24"},
    )
    _add(
        session,
        uid="pen-1",
        title="Pending",
        added=False,
        ignore=False,
        shown=False,
        attributes={"genres": ["Drama"], "year": 2020, "studio": "A24"},
    )

    profile = compute_library_profile(session, "mv")
    assert profile["total_added"] == 1
    assert profile["top_genres"] == [{"name": "Drama", "count": 1, "share": 1.0}]
    assert profile["top_studios"] == [{"name": "A24", "count": 1, "share": 1.0}]


def test_profile_ranks_by_count(session) -> None:
    """Genres are ordered by frequency; shares reflect total adds."""
    for i in range(5):
        _add(
            session,
            uid=f"c-{i}",
            title=f"Comedy {i}",
            attributes={"genres": ["Comedy"]},
        )
    for i in range(3):
        _add(
            session,
            uid=f"d-{i}",
            title=f"Drama {i}",
            attributes={"genres": ["Drama"]},
        )
    for i in range(2):
        _add(
            session,
            uid=f"h-{i}",
            title=f"Horror {i}",
            attributes={"genres": ["Horror"]},
        )

    profile = compute_library_profile(session, "mv")
    assert profile["total_added"] == 10
    assert [(g["name"], g["count"]) for g in profile["top_genres"]] == [
        ("Comedy", 5),
        ("Drama", 3),
        ("Horror", 2),
    ]
    assert profile["top_genres"][0]["share"] == 0.5


def test_profile_handles_list_and_scalar_attrs(session) -> None:
    """Genres / language stored as both list[str] and bare str must merge cleanly."""
    _add(session, uid="a", title="a", attributes={"genres": ["Horror"]})
    _add(session, uid="b", title="b", attributes={"genres": "Horror"})
    _add(session, uid="c", title="c", attributes={"genres": ["Horror", "Thriller"]})

    profile = compute_library_profile(session, "mv")
    horror_count = next(
        g["count"] for g in profile["top_genres"] if g["name"] == "Horror"
    )
    thriller_count = next(
        g["count"] for g in profile["top_genres"] if g["name"] == "Thriller"
    )
    assert horror_count == 3
    assert thriller_count == 1


def test_profile_decade_bucketing(session) -> None:
    _add(session, uid="x", title="x", attributes={"year": 2024})
    _add(session, uid="y", title="y", attributes={"year": 1987})
    _add(session, uid="z", title="z", attributes={"year": 2021})

    profile = compute_library_profile(session, "mv")
    decades = {d["decade"]: d["count"] for d in profile["decade_distribution"]}
    assert decades == {2020: 2, 1980: 1}


def test_profile_isolates_item_type(session) -> None:
    """A TV row doesn't leak into the movie profile and vice versa."""
    _add(session, uid="mv1", title="mv", attributes={"genres": ["Action"]})
    _add(
        session,
        uid="tv1",
        title="tv",
        item_type="tv",
        attributes={"genres": ["Comedy"], "network": "HBO"},
    )

    mv_profile = compute_library_profile(session, "mv")
    tv_profile = compute_library_profile(session, "tv")
    assert mv_profile["total_added"] == 1
    assert tv_profile["total_added"] == 1
    assert "top_networks" in tv_profile and "top_studios" not in tv_profile
    assert "top_studios" in mv_profile and "top_networks" not in mv_profile


def test_candidate_match_resolves_ranks(session) -> None:
    """The candidate's genres get pre-resolved into ranks the model can read."""
    for i in range(5):
        _add(
            session,
            uid=f"c-{i}",
            title=f"c{i}",
            attributes={"genres": ["Comedy"], "studio": "Universal"},
        )
    for i in range(3):
        _add(
            session,
            uid=f"h-{i}",
            title=f"h{i}",
            attributes={"genres": ["Horror"], "studio": "Blumhouse"},
        )

    profile = compute_library_profile(session, "mv")
    candidate_attrs = {
        "genres": ["Horror", "Mystery"],
        "studio": ["Blumhouse"],
        "year": 2026,
    }
    match = compute_candidate_match(profile, candidate_attrs)

    # Horror is the #2 genre in this library; Mystery isn't in top_genres at all.
    # top_n reflects how many entries the ranked list actually has — capped at
    # TOP_GENRES (20) in real use but only 2 distinct genres in this fixture.
    assert match["genres"][0] == {"name": "Horror", "rank": 2, "top_n": 2}
    assert match["genres"][1]["name"] == "Mystery"
    assert match["genres"][1]["rank"] is None
    assert match["genres"][1]["top_n"] == 2

    # Blumhouse is #2 studio.
    studios = {s["name"]: s for s in match["studios"]}
    assert studios["Blumhouse"]["rank"] == 2


def test_candidate_match_unranked_for_unknown_director(session) -> None:
    _add(session, uid="x", title="x", attributes={"director": "Known Director"})
    profile = compute_library_profile(session, "mv")
    match = compute_candidate_match(profile, {"director": "Stranger"})
    assert match["director"]["rank"] is None


def test_candidate_match_decade_share(session) -> None:
    for y in [2024, 2024, 2024, 1995]:
        _add(session, uid=f"y-{y}-{id(y)}", title="x", attributes={"year": y})
    profile = compute_library_profile(session, "mv")
    match = compute_candidate_match(profile, {"year": 2021})
    # 2021 candidate → 2020s decade → 3 of 4 adds = 0.75 share.
    assert match["decade"]["decade"] == 2020
    assert match["decade"]["share_of_added"] == 0.75


def test_empty_library_profile(session) -> None:
    """No added items shouldn't blow up."""
    profile = compute_library_profile(session, "mv")
    assert profile["total_added"] == 0
    assert profile["top_genres"] == []
    assert profile["decade_distribution"] == []
