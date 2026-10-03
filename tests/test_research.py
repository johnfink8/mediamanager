"""The shared research runner, the synopsis agent's inputs, and buzz identity.

``run_research`` is driven with a scripted model (no network); the input
builders are pure, and TMDB is stubbed.
"""

import json
from typing import Any, Dict

from agents import Agent, function_tool

from indexer_utils import tmdb
from indexer_utils.ai_tools import synopsis as syn
from indexer_utils.ai_tools.discoveries import candidate_facts
from indexer_utils.ai_tools.research import ResearchRun, ResearchSpec, run_research
from tests.test_turn_budget import GreedyModel


@function_tool
def lookup(query: str) -> str:
    """Look something up."""
    return f"found {query}"


class ArgsModel(GreedyModel):
    """GreedyModel, but its tool calls carry arguments."""

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        response = await super().get_response(*args, **kwargs)
        for item in response.output:
            if getattr(item, "type", None) == "function_call":
                item.arguments = json.dumps({"query": "q"})
        return response


def _spec(model: Any, turns: int = 3) -> ResearchSpec:
    agent = Agent(name="t", instructions="x", model=model, tools=[lookup])
    return ResearchSpec(name="t", agent=agent, max_turns=turns)


async def test_run_records_every_tool_call_with_its_arguments() -> None:
    run = await run_research(_spec(ArgsModel(), turns=3), "go")
    assert run.error is None
    assert run.output == "final answer"
    assert run.turns == 3
    assert run.tool_calls == 2
    assert [c["name"] for c in run.tool_log] == ["lookup", "lookup"]
    assert json.loads(run.tool_log[0]["arguments"]) == {"query": "q"}
    assert run.tool_log[0]["output_preview"] == "found q"


async def test_max_turns_overrides_the_spec() -> None:
    run = await run_research(_spec(ArgsModel(), turns=6), "go", max_turns=2)
    assert run.turns == 2


async def test_a_model_error_is_returned_not_raised() -> None:
    class Broken(GreedyModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("gateway down")

    run = await run_research(_spec(Broken()), "go")
    assert run.output is None
    assert run.error == "RuntimeError: gateway down"


class TestSynopsisInput:
    CANDIDATE = {
        "uid": "tt6933238",
        "title": "Unabomber.2026.1080p.WEB",
        "year": 2026,
        "cast": ["A", "B", "C", "D", "E", "F", "G"],
        "director": "Some Director",
        "genres": ["Drama"],
        "tmdb_id": 42,
        "network": None,
    }

    def test_carries_the_tmdb_facts_and_ids(self) -> None:
        details = {"title": "Unabomber", "overview": "The hunt for…"}
        facts = json.loads(syn.synopsis_input("mv", self.CANDIDATE, details))
        assert facts["title"] == "Unabomber"  # TMDB's, not the filename
        assert facts["ids"] == {"imdb": "tt6933238", "tmdb": 42}
        assert facts["tmdb"]["overview"] == "The hunt for…"
        assert facts["cast"] == ["A", "B", "C", "D", "E", "F"]
        assert "network" not in facts  # empty values are dropped

    def test_says_when_tmdb_has_nothing(self) -> None:
        facts = json.loads(syn.synopsis_input("tv", {"uid": "242521"}, None))
        assert facts["ids"] == {"tvdb": "242521"}
        assert facts["tmdb"].startswith("no TMDB record")


class TestResearchSynopsis:
    async def _result(self, monkeypatch: Any, run: ResearchRun) -> Any:
        async def fake_run(*args: Any, **kwargs: Any) -> ResearchRun:
            return run

        monkeypatch.setattr(syn, "run_research", fake_run)
        tmdb = {"overview": "A crew plans a heist."}
        return await syn.research_synopsis("mv", {"uid": "tt1"}, tmdb)

    async def test_success(self, monkeypatch: Any) -> None:
        reply = "A  heist\nfilm.\n\nSources:\n- TMDB overview\n- https://x.org/never"
        res = await self._result(monkeypatch, ResearchRun(output=reply))
        assert res.synopsis == "A heist film."
        assert res.sources == ["TMDB overview"]  # the unfetched page is dropped
        assert res.failure is None

    async def test_run_error_is_a_failure(self, monkeypatch: Any) -> None:
        res = await self._result(monkeypatch, ResearchRun(error="boom"))
        assert res.synopsis is None
        assert res.failure["code"] == "research_failed"
        assert res.failure["message"] == "boom"
        assert res.failure["stage"] == "synopsis"

    async def test_blank_synopsis_is_a_failure(self, monkeypatch: Any) -> None:
        res = await self._result(monkeypatch, ResearchRun(output="Sources:\n- x"))
        assert res.failure["code"] == "missing_synopsis"


class TestParseReply:
    def test_plain(self) -> None:
        assert syn.parse_reply("One. Two.\n\nSources:\n- TMDB overview") == (
            "One. Two.",
            ["TMDB overview"],
        )

    def test_drafts_notes_and_url_notes_are_dropped(self) -> None:
        reply = (
            "**Locker Diaries (2026)**\n\nSome notes on what I found.\n\n"
            "Drafted synopsis (≤3 sentences):\n\nThe real synopsis.\n\n"
            "(379 characters)\n\n**Sources:**\n"
            "- https://press.example.com/a (anthology framing)\n- TMDB overview"
        )
        assert syn.parse_reply(reply) == (
            "The real synopsis.",
            ["https://press.example.com/a", "TMDB overview"],
        )

    def test_no_sources_line(self) -> None:
        assert syn.parse_reply("Just a synopsis.") == ("Just a synopsis.", [])


class TestCandidateFacts:
    CANDIDATE = {
        "uid": "tt0039439",
        "title": "The Guilty",
        "tmdb_title": "The Guilty",
        "tmdb_id": 35546,
        "director": "John Reinhardt",
        "cast": ["Bonita Granville", "Don Castle"],
    }

    def test_the_candidate_itself_gets_its_identity(self) -> None:
        facts = candidate_facts("mv", self.CANDIDATE, "the guilty")
        assert facts is not None
        assert facts["IMDb id"].startswith("tt0039439")
        assert "35546" in facts["TMDB id"]
        assert facts["director"] == "John Reinhardt"
        assert facts["lead cast"] == "Bonita Granville, Don Castle"

    def test_other_titles_get_nothing(self) -> None:
        assert candidate_facts("mv", self.CANDIDATE, "Detour") is None


def test_title_details_are_curated(monkeypatch: Any) -> None:
    payload = {
        "title": "Heat",
        "original_title": "Heat",
        "overview": "A cop and a crook.",
        "tagline": "",
        "genres": [{"id": 1, "name": "Crime"}],
        "belongs_to_collection": None,
        "release_date": "1995-12-15",
        "runtime": 170,
        "production_countries": [{"iso_3166_1": "US"}],
        "production_companies": [{"name": "Regency"}],
        "budget": 60000000,
    }

    class Resp:
        def json(self) -> Any:
            return payload

    monkeypatch.setattr(tmdb.requests, "get", lambda *a, **k: Resp())
    assert tmdb.get_title_details("mv", 949) == {
        "title": "Heat",
        "overview": "A cop and a crook.",
        "genres": ["Crime"],
        "countries": ["US"],
        "release_date": "1995-12-15",
        "runtime_min": 170,
        "studios": ["Regency"],
    }


def _fetch(url: str, preview: str) -> Dict[str, Any]:
    return {
        "name": "web_fetch",
        "arguments": json.dumps({"url": url}),
        "output_preview": preview,
    }


WIKI = "https://en.wikipedia.org/wiki/X"
IMDB = "https://www.imdb.com/title/tt313973/"
LOG = [
    {"name": "brave_search", "arguments": '{"query": "x"}'},
    _fetch(WIKI, "{'url': 'https://en.wikipedia.org/wiki/X', 'chars': 5120, '…"),
    {"name": "web_fetch", "arguments": "not json", "output_preview": ""},
]


def test_only_fetched_pages_count_as_sources() -> None:
    kept, dropped = syn.verify_sources(
        ["TMDB overview", WIKI + "/", IMDB], LOG, had_overview=True
    )
    assert kept == ["TMDB overview", WIKI + "/"]
    assert dropped == [IMDB]


def test_a_failed_or_empty_fetch_is_not_a_source() -> None:
    log = [
        _fetch(IMDB, "{'error': 'HTTP 403 for https://www.imdb.com/title/tt313973/'}"),
        _fetch(WIKI, "{'url': 'https://en.wikipedia.org/wiki/X', 'chars': 0, '…"),
    ]
    assert syn.verify_sources([IMDB, WIKI], log, had_overview=True) == (
        [],
        [IMDB, WIKI],
    )


def test_tmdb_overview_needs_an_overview() -> None:
    kept, dropped = syn.verify_sources(["TMDB overview"], LOG, had_overview=False)
    assert (kept, dropped) == ([], ["TMDB overview"])


async def test_a_missing_api_key_is_returned_not_raised(monkeypatch: Any) -> None:
    from decouple import UndefinedValueError

    from indexer_utils.ai_tools import research

    def config(key: str, **kw: Any) -> Any:
        if key == "OPENAI_API_KEY":
            raise UndefinedValueError("OPENAI_API_KEY not found")
        return kw.get("default")

    monkeypatch.setattr(research, "config", config)
    run = await run_research(_spec(ArgsModel()), "go")
    assert run.output is None
    assert run.error is not None and "OPENAI_API_KEY" in run.error


def test_buzz_cache_is_per_candidate_when_its_facts_are_used() -> None:
    from datetime import date

    from indexer_utils.ai_tools.discoveries import _buzz_cache_key

    def key(uid: Any) -> str:
        return _buzz_cache_key(
            today=date(2026, 10, 3),
            title="The Guilty",
            year=None,
            item_type="mv",
            uid=uid,
        )

    assert key("tt0039439") != key("tt9054192")
    assert key(None) != key("tt0039439")
