"""_ai_details_from_run — what gets stored as the item's ``ai`` block."""

from indexer_utils.ai_recs import _ai_details_from_run
from indexer_utils.ai_tools import AgentRunResult
from indexer_utils.ai_tools.research import ResearchRun
from indexer_utils.ai_tools.synopsis import SynopsisResult

SYNOPSIS_FAILURE = {
    "code": "missing_synopsis",
    "stage": "synopsis",
    "message": "Synopsis missing from AI response",
}


def _synopsis(text=None, failure=None):
    return SynopsisResult(
        synopsis=text,
        sources=["TMDB overview"] if text else [],
        failure=failure,
        run=ResearchRun(turns=2, tool_calls=1),
    )


def _run(**kwargs):
    return AgentRunResult(
        submission={"recommend": False, "score": 0.32, "reason": "drag"}, **kwargs
    )


def test_synopsis_failure_does_not_fail_a_completed_verdict():
    ai = _ai_details_from_run(_run(), _synopsis(failure=SYNOPSIS_FAILURE), {})
    assert ai["failed"] is False
    assert ai["failure"] is None  # the UI reads any failure as a failed verdict
    assert ai["synopsis_failure"] == SYNOPSIS_FAILURE
    assert (ai["value"], ai["score"], ai["reason"]) == (False, 0.32, "drag")


def test_clean_run_has_no_failures():
    ai = _ai_details_from_run(_run(), _synopsis("a synopsis"), {})
    assert ai["failed"] is False
    assert ai["failure"] is None
    assert ai["synopsis_failure"] is None
    assert ai["synopsis"] == "a synopsis"
    assert ai["synopsis_sources"] == ["TMDB overview"]
    assert ai["synopsis_research"]["turns"] == 2


def test_agent_failure_still_fails_even_with_a_synopsis_failure():
    run = AgentRunResult(
        failure={"code": "too_many_turns", "message": "turns", "stage": "rec"}
    )
    ai = _ai_details_from_run(run, _synopsis(failure=SYNOPSIS_FAILURE), {})
    assert ai["failed"] is True
    assert ai["value"] is None
    # both are kept: the agent's as the failure, the synopsis's on its own key
    assert ai["failure"]["code"] == "too_many_turns"
    assert ai["synopsis_failure"] == SYNOPSIS_FAILURE


def test_retry_clears_a_stale_failure():
    stale = {"failed": True, "failure": SYNOPSIS_FAILURE, "value": False}
    ai = _ai_details_from_run(_run(), _synopsis("now it worked"), stale)
    assert ai["failed"] is False
    assert ai["failure"] is None
