"""The shared runner behind every web-research subagent.

A research agent is a ``ResearchSpec``: an ``Agent`` with web tools (plus
any task tools) and a turn budget. Its caller builds the user prompt from
real data it looked up itself — TMDB, the catalog, Plex — so the model
researches from facts it was handed rather than from memory, and
``run_research`` runs it. The production tools (synopsis, buzz, cast
history, the release windows) and ``research_harness.py`` all go through
this one function, so what the harness shows is what production runs.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents import Agent, RunConfig, Runner
from agents.models.openai_provider import OpenAIProvider
from decouple import config
from openai import AsyncOpenAI

from .hooks import AuditHooks
from .shared import strip_preamble
from .turn_budget import TurnBudget

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchSpec:
    """One research agent: its ``Agent`` and how many turns it gets."""

    name: str
    agent: Agent[Any]
    max_turns: int


@dataclass
class ResearchRun:
    """What one research run produced, plus its audit trail.

    ``output`` is the agent's final output: the dossier text (preamble
    stripped) for a prose agent, the parsed model for one with an
    ``output_type``. ``error`` is set instead when the run failed or
    produced nothing.
    """

    output: Any = None
    error: Optional[str] = None
    turns: int = 0
    tool_calls: int = 0
    tool_log: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0

    def audit(self) -> Dict[str, Any]:
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "elapsed_s": round(self.elapsed_s, 1),
            "tool_log": self.tool_log,
        }


async def run_research(
    spec: ResearchSpec,
    user_prompt: str,
    *,
    context: Any = None,
    max_turns: Optional[int] = None,
    log_tag: Optional[str] = None,
) -> ResearchRun:
    """Run ``spec`` on ``user_prompt``; failures come back as ``error``.

    ``context`` reaches the tools (``check_titles`` needs the candidate).
    ``max_turns`` overrides the spec's budget (the harness uses it).
    """
    turns = max_turns or spec.max_turns
    hooks = AuditHooks(max_tool_calls=None, log_tag=log_tag or spec.name)
    started = time.monotonic()

    def done(**kw: Any) -> ResearchRun:
        return ResearchRun(
            turns=hooks.turns,
            tool_calls=hooks.tool_calls,
            tool_log=hooks.tool_log,
            elapsed_s=time.monotonic() - started,
            **kw,
        )

    # Per-run client so the httpx transport is bound to this event loop and
    # closed before the task exits — see agent.py for the same pattern.
    openai_client = AsyncOpenAI(
        api_key=config("OPENAI_API_KEY"),
        base_url=config("OPENAI_BASE_URL", default=None),
    )
    provider = OpenAIProvider(openai_client=openai_client)
    run_config = RunConfig(tracing_disabled=True, model_provider=provider)
    try:
        try:
            result = await Runner.run(
                TurnBudget(turns).prepare(spec.agent, provider),
                user_prompt,
                context=context,
                max_turns=turns,
                hooks=hooks,
                run_config=run_config,
            )
        finally:
            # OpenAIProvider.aclose intentionally leaves the AsyncOpenAI
            # client open (in case it's shared), so we close it ourselves.
            await provider.aclose()
            await openai_client.close()
    except Exception as exc:
        logger.exception("%s subagent failed", spec.name)
        return done(error=f"{exc.__class__.__name__}: {exc}")

    output = result.final_output
    if output is None or isinstance(output, str):
        output = strip_preamble(str(output or "").strip())
        if not output:
            return done(
                error="subagent returned nothing "
                f"(responses={len(result.raw_responses)}, max_turns={turns})"
            )
    return done(output=output)
