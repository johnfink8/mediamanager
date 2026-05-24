"""Tests for the simplified search tools.

After the library_profile refactor the search tools have a strict contract:
- they only return ``added=True`` items
- they don't include ``decision_counts`` (aggregate taste lives in the
  prompt's library_profile block; tools are concrete-example lookups)
- the per-row ``decision`` field is stripped (it would always be "added")
"""

import json

import pytest_asyncio
from agents.tool_context import ToolContext as SdkToolContext

from indexer_utils.ai_tools.base import ToolContext
from indexer_utils.ai_tools.searches import search_by_genre, search_by_network
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session


@pytest_asyncio.fixture
async def session():
    async with db_session() as s:
        yield s


async def _seed(session) -> None:
    """3 added + 5 rejected + 1 pending horror; 2 added Blumhouse; 2 unrelated drama."""
    rows = [
        IgnoreItem(
            uid="add-1",
            title="A1",
            item_type="mv",
            added=True,
            ignore=True,
            shown=True,
            attributes={"genres": ["Horror"], "studio": "Blumhouse"},
        ),
        IgnoreItem(
            uid="add-2",
            title="A2",
            item_type="mv",
            added=True,
            ignore=True,
            shown=True,
            attributes={"genres": ["Horror", "Thriller"], "studio": "A24"},
        ),
        IgnoreItem(
            uid="add-3",
            title="A3",
            item_type="mv",
            added=True,
            ignore=True,
            shown=True,
            attributes={"genres": ["Horror"], "studio": "Blumhouse"},
        ),
        *[
            IgnoreItem(
                uid=f"rej-{i}",
                title=f"R{i}",
                item_type="mv",
                added=False,
                ignore=True,
                shown=True,
                attributes={"genres": ["Horror"], "studio": "Blumhouse"},
            )
            for i in range(5)
        ],
        IgnoreItem(
            uid="pen-1",
            title="P1",
            item_type="mv",
            added=False,
            ignore=False,
            shown=False,
            attributes={"genres": ["Horror"], "studio": "Blumhouse"},
        ),
        IgnoreItem(
            uid="dr-1",
            title="D1",
            item_type="mv",
            added=True,
            ignore=True,
            shown=True,
            attributes={"genres": ["Drama"], "studio": "Searchlight"},
        ),
    ]
    for r in rows:
        session.add(r)
    await session.commit()


async def _invoke(tool, ctx: ToolContext, tool_args: dict) -> dict:
    json_args = json.dumps(tool_args)
    sdk_ctx = SdkToolContext(
        context=ctx,
        tool_name=tool.name,
        tool_call_id="test-call",
        tool_arguments=json_args,
    )
    raw = await tool.on_invoke_tool(sdk_ctx, json_args)
    return raw if isinstance(raw, dict) else json.loads(raw)


def _ctx() -> ToolContext:
    return ToolContext(
        item_type="mv",
        candidate={
            "uid": "candidate-uid",
            "title": "Candidate",
            "year": 2026,
            "genres": ["Horror"],
        },
    )


async def test_genre_excludes_rejected_and_pending(session) -> None:
    await _seed(session)
    result = await _invoke(search_by_genre, _ctx(), {"genres": ["Horror"]})
    uids = {row["uid"] for row in result["results"]}
    assert uids == {"add-1", "add-2", "add-3"}


async def test_genre_response_has_no_decision_counts(session) -> None:
    await _seed(session)
    result = await _invoke(search_by_genre, _ctx(), {"genres": ["Horror"]})
    assert "decision_counts" not in result
    assert set(result.keys()) == {"results"}


async def test_genre_rows_have_no_decision_field(session) -> None:
    """``decision`` would always be 'added' now; strip it to save tokens."""
    await _seed(session)
    result = await _invoke(search_by_genre, _ctx(), {"genres": ["Horror"]})
    for row in result["results"]:
        assert "decision" not in row


def test_genre_schema_omits_added_only() -> None:
    """The added_only knob is gone from the tool's published schema —
    the model literally can't ask for it."""
    schema = search_by_genre.params_json_schema
    properties = schema.get("properties", {})
    assert "added_only" not in properties
    # Sanity: the args we still expose are present.
    assert "genres" in properties
    assert "limit" in properties


async def test_network_excludes_rejected_and_pending(session) -> None:
    await _seed(session)
    result = await _invoke(search_by_network, _ctx(), {"network": "Blumhouse"})
    uids = {row["uid"] for row in result["results"]}
    # 5 rejected and 1 pending Blumhouse are excluded; only the 2 added remain.
    assert uids == {"add-1", "add-3"}


async def test_network_response_has_no_decision_counts(session) -> None:
    await _seed(session)
    result = await _invoke(search_by_network, _ctx(), {"network": "Blumhouse"})
    assert "decision_counts" not in result
    assert set(result.keys()) == {"results"}


async def test_per_source_rating_filters(session) -> None:
    """Each rating source has its own filter; missing data on a source means
    that source's filter excludes the row (no fall-through to other sources)."""
    rows = [
        IgnoreItem(
            uid="imdb-rated",
            title="Imdb Rated",
            item_type="mv",
            added=True,
            ignore=True,
            shown=True,
            attributes={
                "genres": ["Horror"],
                "imdbuser_value": 8.0,
                "imdbuser_votes": 50000,
            },
        ),
        IgnoreItem(
            uid="tmdb-only",
            title="Tmdb Only",
            item_type="mv",
            added=True,
            ignore=True,
            shown=True,
            attributes={
                "genres": ["Horror"],
                "tmdbuser_value": 8.5,
                "tmdbuser_votes": 200,
            },
        ),
        IgnoreItem(
            uid="rt-only",
            title="Rt Only",
            item_type="mv",
            added=True,
            ignore=True,
            shown=True,
            attributes={
                "genres": ["Horror"],
                "rottenTomatoesuser_value": 92,
            },
        ),
    ]
    for r in rows:
        session.add(r)
    await session.commit()

    r = await _invoke(search_by_genre, _ctx(), {"genres": ["Horror"], "imdb_min": 7.0})
    assert {row["uid"] for row in r["results"]} == {"imdb-rated"}

    r = await _invoke(search_by_genre, _ctx(), {"genres": ["Horror"], "tmdb_min": 7.0})
    assert {row["uid"] for row in r["results"]} == {"tmdb-only"}

    r = await _invoke(search_by_genre, _ctx(), {"genres": ["Horror"], "rt_min": 80})
    assert {row["uid"] for row in r["results"]} == {"rt-only"}

    # imdb_votes_min: only the IMDB row has votes; missing data → fails.
    r = await _invoke(
        search_by_genre,
        _ctx(),
        {"genres": ["Horror"], "imdb_votes_min": 1000},
    )
    assert {row["uid"] for row in r["results"]} == {"imdb-rated"}


async def test_summary_rows_expose_populated_rating_sources(session) -> None:
    """``summarize_item`` should emit only the rating fields that are
    populated for the row — keeps the token cost down on sparse data."""
    session.add(
        IgnoreItem(
            uid="multi-source",
            title="Multi",
            item_type="mv",
            added=True,
            ignore=True,
            shown=True,
            attributes={
                "genres": ["Horror"],
                "imdbuser_value": 7.4,
                "imdbuser_votes": 12000,
                "rottenTomatoesuser_value": 88,
            },
        )
    )
    await session.commit()

    r = await _invoke(search_by_genre, _ctx(), {"genres": ["Horror"]})
    row = next(x for x in r["results"] if x["uid"] == "multi-source")
    assert row["imdb_rating"] == 7.4
    assert row["imdb_votes"] == 12000
    assert row["rt"] == 88
    # Sources without data are omitted entirely.
    assert "tmdb_rating" not in row
    assert "metacritic" not in row
    assert "rating" not in row
