"""Transient-failure retry in ``run_recommendation``.

The serving gateway rejects a model response with a 502 whenever the sampled
output momentarily violates a schema it validates against (a tool argument,
the structured verdict), and connection blips happen when the model server
restarts. Those clear on a fresh sampling; budget exhaustion and bugs do not,
so they must fail fast rather than burn GPU attempts.
"""

import os
from types import SimpleNamespace
from typing import Any, List, Optional

import httpx

os.environ.setdefault("OPENAI_API_KEY", "test-agent-retry")

import pytest  # noqa: E402
from agents.exceptions import MaxTurnsExceeded  # noqa: E402
from openai import (  # noqa: E402
    APIConnectionError,
    APIStatusError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from indexer_utils.ai_tools import agent as agent_mod  # noqa: E402
from indexer_utils.ai_tools.agent import (  # noqa: E402
    Recommendation,
    _is_transient,
    run_recommendation,
)
from indexer_utils.ai_tools.base import ToolContext  # noqa: E402


def _http_error(cls: Any, status: int) -> APIStatusError:
    request = httpx.Request("POST", "http://model.test/v1/responses")
    response = httpx.Response(status, request=request, json={"error": "boom"})
    return cls("gateway said no", response=response, body=None)


def test_connection_and_server_errors_are_transient() -> None:
    request = httpx.Request("POST", "http://model.test/v1/responses")
    assert _is_transient(APIConnectionError(message="down", request=request))
    assert _is_transient(_http_error(InternalServerError, 502))
    assert _is_transient(_http_error(RateLimitError, 429))
    assert _is_transient(_http_error(APIStatusError, 500))


def test_deterministic_failures_are_not_transient() -> None:
    assert not _is_transient(_http_error(BadRequestError, 400))
    assert not _is_transient(MaxTurnsExceeded("6 turns"))
    assert not _is_transient(ValueError("a bug is a bug"))


class _FakeRunner:
    """Stands in for ``agents.Runner``; replays a scripted outcome per call."""

    def __init__(self, script: List[Optional[BaseException]]) -> None:
        self.script = script
        self.calls = 0

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        exc = self.script[self.calls]
        self.calls += 1
        if exc is not None:
            raise exc
        return SimpleNamespace(
            final_output=Recommendation(recommend=True, score=0.8, reason="yes")
        )


class _PassthroughBudget:
    def __init__(self, max_turns: int) -> None:
        pass

    def prepare(self, agent: Any, provider: Any) -> Any:
        return agent


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    sleeps: List[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(agent_mod, "TurnBudget", _PassthroughBudget)
    monkeypatch.setattr(agent_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(agent_mod, "AGENT_ATTEMPTS", 3)
    monkeypatch.setattr(agent_mod, "RETRY_BACKOFF_SECONDS", 1.0)

    def use(script: List[Optional[BaseException]]) -> SimpleNamespace:
        runner = _FakeRunner(script)
        monkeypatch.setattr(agent_mod, "Runner", runner)
        return SimpleNamespace(runner=runner, sleeps=sleeps)

    return SimpleNamespace(use=use)


async def _run() -> Any:
    ctx = ToolContext(item_type="mv", candidate={"uid": "tt1"})
    return await run_recommendation(
        item_type="mv",
        model="test-model",
        system_prompt="sys",
        user_prompt="{}",
        ctx=ctx,
    )


async def test_transient_failure_is_resampled_until_success(harness: Any) -> None:
    env = harness.use([_http_error(InternalServerError, 502), None])
    result = await _run()
    assert env.runner.calls == 2
    assert env.sleeps == [1.0]  # linear backoff, one sleep per retry
    assert result.failure is None
    assert result.submission == {
        "recommend": True,
        "score": 0.8,
        "reason": "yes",
    }


async def test_gives_up_after_attempts_with_last_failure(harness: Any) -> None:
    err = _http_error(InternalServerError, 502)
    env = harness.use([err, err, err])
    result = await _run()
    assert env.runner.calls == 3
    assert env.sleeps == [1.0, 2.0]
    assert result.failure is not None
    assert result.failure["code"] == "InternalServerError"
    assert result.failure["stage"] == "request"


async def test_non_transient_fails_fast(harness: Any) -> None:
    env = harness.use([ValueError("bug")])
    result = await _run()
    assert env.runner.calls == 1
    assert env.sleeps == []
    assert result.failure is not None
    assert result.failure["code"] == "ValueError"


async def test_turn_exhaustion_is_not_retried(harness: Any) -> None:
    env = harness.use([MaxTurnsExceeded("6 turns")])
    result = await _run()
    assert env.runner.calls == 1
    assert env.sleeps == []
    assert result.failure is not None
    assert result.failure["code"] == "too_many_turns"
