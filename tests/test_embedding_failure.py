"""An embedding failure fails the annotation visibly, never a blind verdict."""

from typing import Any

import pytest

from indexer_utils import ai_recs, vector_search
from indexer_utils.ai_tools.research import ResearchRun
from indexer_utils.ai_tools.synopsis import SynopsisResult


async def _broken_embed(text: str) -> Any:
    raise ConnectionError("embedding server unreachable")


async def test_upsert_raises_when_embedding_fails(monkeypatch: Any) -> None:
    monkeypatch.setattr(vector_search, "_embed", _broken_embed)
    with pytest.raises(ConnectionError):
        await vector_search.upsert_item_vector({}, "mv", "tt1", "Heat", "A heist.")


async def test_annotation_records_it_and_skips_the_agent(monkeypatch: Any) -> None:
    async def hydrate(item_type: str, uid: str, attrs: Any) -> None:
        return None

    async def synopsis(*args: Any, **kwargs: Any) -> SynopsisResult:
        return SynopsisResult("A heist.", ["TMDB overview"], None, ResearchRun())

    async def agent(**kwargs: Any) -> Any:
        raise AssertionError("the agent must not run without a vector")

    monkeypatch.setattr(ai_recs, "hydrate_candidate", hydrate)
    monkeypatch.setattr(ai_recs, "research_synopsis", synopsis)
    monkeypatch.setattr(ai_recs, "run_recommendation", agent)
    monkeypatch.setattr(vector_search, "_embed", _broken_embed)

    attrs = await ai_recs.annotate_with_ai_async("mv", "tt1", "Heat", {})

    ai = attrs["ai"]
    assert ai["failed"] is True
    assert ai["failure"]["code"] == "embedding_failed"
    assert "unreachable" in ai["failure"]["message"]
    assert ai["synopsis"] == "A heist."  # the research isn't lost
