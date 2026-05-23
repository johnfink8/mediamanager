"""Recommendation Agent factory + ``run_recommendation`` entry point.

This replaces the hand-rolled tool-calling loop. We build a per-item-type
``Agent`` (movies get the theatrical-window tool; TV gets the TV-window
tool; everything else is shared) with a Pydantic ``output_type``, then call
``Runner.run`` and adapt the result back into the legacy
``AgentRunResult`` shape that ``ai_recs._ai_details_from_run`` consumes.

Tracing is disabled — the project doesn't want data flowing to OpenAI's
tracing dashboard. Per-call audit data is captured via ``AuditHooks`` instead.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents import Agent, RunConfig, Runner
from agents.exceptions import MaxTurnsExceeded
from agents.models.openai_provider import OpenAIProvider
from decouple import config
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .base import ToolContext
from .discoveries import (
    search_recent_releases,
    search_recent_tv,
    search_title_buzz,
)
from .hooks import AuditHooks, ToolCallBudgetExceeded
from .inspections import check_added_history, get_item_details, get_user_history
from .searches import search_by_genre, search_by_network, search_similar_by_synopsis

logger = logging.getLogger(__name__)


class Recommendation(BaseModel):
    """Final structured verdict on a candidate."""

    recommend: bool
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=240)


@dataclass
class AgentRunResult:
    """Back-compat shape consumed by ``ai_recs._ai_details_from_run``."""

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
    # process-default AsyncOpenAI, leaking the socket on each asyncio.run
    # exit. The original ai_recs code had the same per-run pattern for the
    # same reason — and ``asyncio.run`` callers from scheduler threads need
    # event-loop-bound transports.
    openai_client = AsyncOpenAI(api_key=config("OPENAI_API_KEY"))
    provider = OpenAIProvider(openai_client=openai_client)
    run_config = RunConfig(tracing_disabled=True, model_provider=provider)

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
        "reason": str(rec.reason)[:240],
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
