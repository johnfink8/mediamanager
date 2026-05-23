"""Behaviour tests for ``AuditHooks``.

Covers the two failure modes flagged in code review of the SDK migration:

1. ``max_tool_calls`` budget enforcement against concurrent ``on_tool_start``
   invocations within a single turn (P1).
2. Per-call timing attribution when the model emits the same tool twice in
   one turn — start times must key off ``tool_call_id``, not ``tool.name`` (P2).
"""

import asyncio
import os
from typing import Any

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-audit-hooks")

from agents.tool import FunctionTool  # noqa: E402
from agents.tool_context import ToolContext as SdkToolContext  # noqa: E402

from indexer_utils.ai_tools.hooks import (  # noqa: E402
    AuditHooks,
    ToolCallBudgetExceeded,
)


def _ctx(call_id: str) -> SdkToolContext[Any]:
    return SdkToolContext(
        context=None,
        tool_name="any",
        tool_call_id=call_id,
        tool_arguments="{}",
    )


def _tool(name: str = "any") -> Any:
    # FunctionTool is one of the union members the hook accepts; build a
    # minimal stand-in. ``Any`` for the cast since we don't care about the
    # full Tool union here.
    return FunctionTool(
        name=name,
        description="",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=lambda ctx, args: "",
    )


def test_budget_blocks_concurrent_starts_within_one_turn() -> None:
    """Two on_tool_start firing concurrently with budget=1 must not both pass.

    Before the fix the budget was checked against ``tool_calls``, incremented
    only in ``on_tool_end`` — so the second start in a turn saw the same
    pre-increment count and slipped through.
    """
    hooks = AuditHooks(max_tool_calls=1, log_tag="test")
    tool = _tool()

    async def run() -> tuple[BaseException | None, BaseException | None]:
        # Gather two starts together — same pattern the SDK uses to launch
        # parallel tool calls within a turn.
        return await asyncio.gather(
            hooks.on_tool_start(_ctx("call-A"), None, tool),
            hooks.on_tool_start(_ctx("call-B"), None, tool),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    # First start should succeed (None), second should raise budget error.
    raised = [r for r in results if isinstance(r, BaseException)]
    passed = [r for r in results if r is None]
    assert len(raised) == 1, f"expected exactly one raise, got {results!r}"
    assert len(passed) == 1
    assert isinstance(raised[0], ToolCallBudgetExceeded)


def test_concurrent_same_tool_calls_get_distinct_timings() -> None:
    """Two calls to the same tool in one turn must produce two distinct timings.

    Before the fix start times were keyed by ``tool.name``, so the second
    on_tool_start overwrote the first's timestamp and durations were wrong.
    """
    hooks = AuditHooks(max_tool_calls=10, log_tag="test")
    tool = _tool("search_similar_by_synopsis")

    async def run() -> None:
        await hooks.on_tool_start(_ctx("call-A"), None, tool)
        # Sleep so the two calls have meaningfully different durations.
        await asyncio.sleep(0.05)
        await hooks.on_tool_start(_ctx("call-B"), None, tool)
        # End call-B first (it has the shorter duration) so we exercise
        # the out-of-FIFO-order completion path.
        await hooks.on_tool_end(_ctx("call-B"), None, tool, "result-B")
        await asyncio.sleep(0.05)
        await hooks.on_tool_end(_ctx("call-A"), None, tool, "result-A")

    asyncio.run(run())

    assert len(hooks.tool_log) == 2
    by_preview = {entry["output_preview"]: entry for entry in hooks.tool_log}
    a = by_preview["result-A"]
    b = by_preview["result-B"]
    # call-A ran the full ~100ms; call-B ran ~0ms (started then immediately ended).
    assert a["duration_ms"] is not None and a["duration_ms"] >= 80
    assert b["duration_ms"] is not None and b["duration_ms"] < 30


def test_duration_is_none_when_no_tool_call_id() -> None:
    """Fallback: if a hook is invoked without an SDK ToolContext (unlikely in
    real SDK flow but possible in tests / custom wrappers), record duration as
    None instead of fabricating one off ``time.monotonic()``.
    """
    hooks = AuditHooks(max_tool_calls=5, log_tag="test")
    tool = _tool()

    # Build a bare RunContextWrapper — no tool_call_id attribute.
    from agents import RunContextWrapper

    bare = RunContextWrapper(context=None)

    async def run() -> None:
        await hooks.on_tool_start(bare, None, tool)
        await hooks.on_tool_end(bare, None, tool, "out")

    asyncio.run(run())
    assert hooks.tool_log[0]["duration_ms"] is None
    # The budget still counts this call.
    assert hooks.tool_calls == 1


def test_budget_counts_starts_not_completions() -> None:
    """If a tool's body raises and ``on_tool_end`` never fires, the started
    call still counts toward the budget. Counting starts is what makes the
    cap a real spend/latency bound."""
    hooks = AuditHooks(max_tool_calls=2, log_tag="test")
    tool = _tool()

    async def run() -> None:
        await hooks.on_tool_start(_ctx("call-A"), None, tool)
        # No on_tool_end for call-A — simulating a tool that hung or raised.
        await hooks.on_tool_start(_ctx("call-B"), None, tool)
        with pytest.raises(ToolCallBudgetExceeded):
            await hooks.on_tool_start(_ctx("call-C"), None, tool)

    asyncio.run(run())
    assert hooks.tool_calls == 2
