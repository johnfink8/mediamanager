"""Lifecycle hooks: per-call audit log + tool-budget tripwire.

The SDK has ``max_turns`` but no cumulative tool-call cap. ``on_tool_start``
fills that gap so ``AI_AGENT_MAX_TOOL_CALLS`` bounds total work, even when a
single turn fires several tools in parallel.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from agents import RunContextWrapper, RunHooks
from agents.agent import Agent
from agents.items import ModelResponse
from agents.run_context import AgentHookContext
from agents.tool import Tool

from .base import ToolContext

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())


class ToolCallBudgetExceeded(RuntimeError):
    """Cumulative tool-call count exceeded the configured budget."""


class AuditHooks(RunHooks[ToolContext]):
    """Records per-tool-call timing + outcome onto an in-memory ``tool_log``.

    Also enforces ``max_tool_calls`` by raising ``ToolCallBudgetExceeded``
    before dispatch when the budget would be breached.
    """

    def __init__(self, *, max_tool_calls: int, log_tag: str) -> None:
        self.max_tool_calls = max_tool_calls
        self.log_tag = log_tag
        self.turns = 0
        self.tool_calls = 0
        self.tool_log: List[Dict[str, Any]] = []
        # tool_call_id -> start monotonic time. The SDK runs function tools
        # concurrently within a turn, so keying by tool.name would collide
        # whenever the model emits the same tool twice in one turn.
        self._starts: Dict[str, float] = {}

    async def on_llm_start(
        self,
        context: RunContextWrapper[ToolContext],
        agent: Agent[ToolContext],
        system_prompt: Optional[str],
        input_items: Any,
    ) -> None:
        self.turns += 1
        try:
            length = len(input_items) if hasattr(input_items, "__len__") else 0
        except TypeError:
            length = 0
        logger.info("%s turn=%d input_items=%d", self.log_tag, self.turns, length)

    async def on_llm_end(
        self,
        context: RunContextWrapper[ToolContext],
        agent: Agent[ToolContext],
        response: ModelResponse,
    ) -> None:
        # Surface the model's narration so the log alone tells the story.
        text = getattr(response, "output_text", None)
        if text:
            preview = text if len(text) <= 600 else text[:600] + " …"
            logger.info("%s turn=%d says: %s", self.log_tag, self.turns, preview)

    async def on_tool_start(
        self,
        context: RunContextWrapper[ToolContext],
        agent: Agent[ToolContext],
        tool: Tool,
    ) -> None:
        # Check + increment must be free of awaits so concurrent on_tool_start
        # invocations within a turn can't both pass the cap.
        if self.tool_calls + 1 > self.max_tool_calls:
            raise ToolCallBudgetExceeded(
                f"agent exceeded {self.max_tool_calls} tool calls"
            )
        self.tool_calls += 1
        call_id = getattr(context, "tool_call_id", None)
        if call_id is not None:
            self._starts[call_id] = time.monotonic()

    async def on_tool_end(
        self,
        context: RunContextWrapper[ToolContext],
        agent: Agent[ToolContext],
        tool: Tool,
        result: str,
    ) -> None:
        call_id = getattr(context, "tool_call_id", None)
        started = self._starts.pop(call_id, None) if call_id is not None else None
        duration_ms = (
            int((time.monotonic() - started) * 1000) if started is not None else None
        )
        preview = result if len(result) <= 800 else result[:800] + " …"
        self.tool_log.append(
            {
                "name": tool.name,
                "duration_ms": duration_ms,
                "error": None,
                "output_preview": preview,
            }
        )
        logger.info(
            "%s tool=%s ms=%s -> %s",
            self.log_tag,
            tool.name,
            duration_ms if duration_ms is not None else "?",
            preview,
        )

    async def on_agent_start(
        self,
        context: AgentHookContext[ToolContext],
        agent: Agent[ToolContext],
    ) -> None:
        return None

    async def on_agent_end(
        self,
        context: AgentHookContext[ToolContext],
        agent: Agent[ToolContext],
        output: Any,
    ) -> None:
        return None
