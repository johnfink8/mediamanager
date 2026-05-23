"""Lifecycle hooks: per-call audit log + tool-budget tripwire.

The SDK doesn't have a native ``max_tool_calls`` cap (it has ``max_turns``).
We add one via ``on_tool_start`` so the legacy ``AI_AGENT_MAX_TOOL_CALLS``
env var still bounds work — and so a model that tries to fire many tools in
a single turn can't slip past ``max_turns`` without our notice.
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
    """Records per-tool-call timing + outcome for the legacy ``tool_log`` field.

    Also enforces ``max_tool_calls`` by raising ``ToolCallBudgetExceeded``
    before dispatch when the budget would be breached.
    """

    def __init__(self, *, max_tool_calls: int, log_tag: str) -> None:
        self.max_tool_calls = max_tool_calls
        self.log_tag = log_tag
        self.turns = 0
        self.tool_calls = 0
        self.tool_log: List[Dict[str, Any]] = []
        # tool name -> start monotonic time; tools can in principle run
        # concurrently within a turn, so key by tool name + call count.
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
        if self.tool_calls + 1 > self.max_tool_calls:
            raise ToolCallBudgetExceeded(
                f"agent exceeded {self.max_tool_calls} tool calls"
            )
        self._starts[tool.name] = time.monotonic()

    async def on_tool_end(
        self,
        context: RunContextWrapper[ToolContext],
        agent: Agent[ToolContext],
        tool: Tool,
        result: str,
    ) -> None:
        started = self._starts.pop(tool.name, time.monotonic())
        duration_ms = int((time.monotonic() - started) * 1000)
        self.tool_calls += 1
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
            "%s tool=%s ms=%d -> %s",
            self.log_tag,
            tool.name,
            duration_ms,
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
