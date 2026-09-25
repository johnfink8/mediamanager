"""Recommendation Agent factory + ``run_recommendation`` entry point.

Builds a per-item-type ``Agent`` — movies see ``search_recent_releases``,
TV sees ``search_recent_tv``, the rest of the toolset is shared — with a
Pydantic ``Recommendation`` as the structured output type.
``run_recommendation`` drives ``Runner.run`` and returns an
``AgentRunResult`` carrying the structured submission plus per-run audit
data (turns, tool calls, tool log, failure reason).

Tracing is disabled via ``RunConfig`` — the project doesn't want data
flowing to OpenAI's tracing dashboard. Per-call audit data is captured via
``AuditHooks``.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents import Agent, RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded
from agents.models.openai_provider import OpenAIProvider
from decouple import config
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from .base import ToolContext
from .cast_history import search_cast_history
from .discoveries import (
    search_recent_releases,
    search_recent_tv,
    search_title_buzz,
)
from .hooks import AuditHooks, ToolCallBudgetExceeded
from .inspections import check_added_history, get_item_details, get_user_history
from .searches import search_by_genre, search_by_network, search_similar_by_synopsis
from .shared import REASON_CLIP
from .turn_budget import TurnBudget

logger = logging.getLogger(__name__)


class Recommendation(BaseModel):
    """Final structured verdict on a candidate."""

    recommend: bool = Field(
        description="True if the candidate should be surfaced to the user.",
    )
    score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Recommendation strength, 0.0–1.0, monotonic with `recommend`. "
            "Use score ≥ 0.5 when recommend=true (0.6 marginal, 0.9 strong); "
            "score < 0.5 when recommend=false (0.1 definite no, 0.4 close "
            "call). The pending-candidate list is sorted by score regardless "
            "of `recommend`, so a high score paired with recommend=false "
            "would surface a rejected pick at the top of the user's queue — "
            "keep the two aligned."
        ),
    )
    reason: str = Field(
        max_length=REASON_CLIP,
        description=(
            "One or two complete sentences (about 300 characters at most) naming "
            "the single strongest signal driving the verdict and its concrete "
            "evidence."
        ),
    )

    @field_validator("score")
    @classmethod
    def _align_score_with_verdict(cls, v: float, info: Any) -> float:
        # Observed ~8% of the time the model pairs a confident score with the
        # opposite verdict (e.g. 0.9 + recommend=false for an already-watched
        # title). The verdict is what the user consumes; the score only ranks
        # the queue — so on a disagreement, clamp the score to the verdict's side.
        rec: Optional[bool] = info.data.get("recommend")
        if rec is True and v < 0.5:
            logger.info("score %.2f clamped to 0.5 (recommend=true)", v)
            return 0.5
        if rec is False and v >= 0.5:
            logger.info("score %.2f clamped to 0.49 (recommend=false)", v)
            return 0.49
        return v


@dataclass
class AgentRunResult:
    """Structured submission + audit data for one recommendation run."""

    submission: Optional[Dict[str, Any]] = None
    turns: int = 0
    tool_calls: int = 0
    tool_log: List[Dict[str, Any]] = field(default_factory=list)
    failure: Optional[Dict[str, Any]] = None


_COMMON_TOOLS = [
    search_similar_by_synopsis,
    search_by_genre,
    search_by_network,
    get_item_details,
    get_user_history,
    check_added_history,
    search_title_buzz,
    search_cast_history,
]


def build_agent(
    *, item_type: str, model: str, system_prompt: str
) -> Agent[ToolContext]:
    """Construct an Agent wired with the tools appropriate for ``item_type``."""
    tools = list(_COMMON_TOOLS)
    if item_type == "mv":
        tools.append(search_recent_releases)
    elif item_type == "tv":
        tools.append(search_recent_tv)
    return Agent[ToolContext](
        name=f"recommend-{item_type}",
        model=model,
        instructions=system_prompt,
        tools=tools,
        output_type=Recommendation,
    )


async def run_recommendation(
    *,
    item_type: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    ctx: ToolContext,
    max_turns: int = 6,
    max_tool_calls: int = 16,
    log_tag: Optional[str] = None,
) -> AgentRunResult:
    """Drive the recommendation Agent and return an ``AgentRunResult``.

    Model-level failures (turn cap, tool-budget cap, transport error) become
    ``result.failure``; only truly exceptional bugs propagate.
    """
    tag = log_tag or f"agent[{item_type}:{ctx.candidate.get('uid')}]"
    agent = build_agent(item_type=item_type, model=model, system_prompt=system_prompt)
    hooks = AuditHooks(max_tool_calls=max_tool_calls, log_tag=tag)

    # Build a fresh httpx-backed client per run and close it before this task
    # returns. The SDK would otherwise lazily build (and never close) a
    # process-default AsyncOpenAI, leaking the socket on each ``asyncio.run``
    # exit. Per-run scoping also keeps httpx transports bound to the loop
    # that opened them — required when scheduler threads spin up short-lived
    # loops via ``asyncio.run``.
    openai_client = AsyncOpenAI(
        api_key=config("OPENAI_API_KEY"),
        base_url=config("OPENAI_BASE_URL", default=None),
    )
    provider = OpenAIProvider(openai_client=openai_client)
    run_config = RunConfig(tracing_disabled=True, model_provider=provider)
    agent = TurnBudget(max_turns).prepare(agent, provider)

    try:
        try:
            result = await Runner.run(
                agent,
                user_prompt,
                context=ctx,
                max_turns=max_turns,
                hooks=hooks,
                run_config=run_config,
            )
        finally:
            # OpenAIProvider.aclose intentionally leaves the AsyncOpenAI
            # client open (in case it's shared), so we close it ourselves.
            await provider.aclose()
            await openai_client.close()
    except MaxTurnsExceeded as exc:
        logger.warning("%s exhausted turns: %s", tag, exc)
        return AgentRunResult(
            failure={
                "code": "too_many_turns",
                "message": str(exc),
                "stage": "recommendation",
            },
            turns=hooks.turns,
            tool_calls=hooks.tool_calls,
            tool_log=hooks.tool_log,
        )
    except ToolCallBudgetExceeded as exc:
        logger.warning("%s exhausted tool budget: %s", tag, exc)
        return AgentRunResult(
            failure={
                "code": "too_many_tool_calls",
                "message": str(exc),
                "stage": "recommendation",
            },
            turns=hooks.turns,
            tool_calls=hooks.tool_calls,
            tool_log=hooks.tool_log,
        )
    except Exception as exc:
        logger.exception("%s agent run failed", tag)
        return AgentRunResult(
            failure={
                "code": exc.__class__.__name__,
                "message": str(exc),
                "stage": "request",
            },
            turns=hooks.turns,
            tool_calls=hooks.tool_calls,
            tool_log=hooks.tool_log,
        )

    rec: Recommendation = result.final_output
    submission = {
        "recommend": bool(rec.recommend),
        "score": max(0.0, min(float(rec.score), 1.0)),
        "reason": str(rec.reason)[:REASON_CLIP],
    }
    logger.info(
        "%s submitted recommend=%s score=%s turns=%d tool_calls=%d reason=%s",
        tag,
        submission["recommend"],
        submission["score"],
        hooks.turns,
        hooks.tool_calls,
        submission["reason"][:320],
    )
    return AgentRunResult(
        submission=submission,
        turns=hooks.turns,
        tool_calls=hooks.tool_calls,
        tool_log=hooks.tool_log,
    )
