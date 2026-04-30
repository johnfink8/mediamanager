"""Async OpenAI tool-calling agent loop.

The loop is sequential per agent run — each turn the model picks tools, we
dispatch them (multiple in one turn run via ``asyncio.gather``), feed the
results back, and repeat until the model calls the terminal tool or we hit a
safety cap.

External callers run many agents in parallel via ``asyncio.gather`` over the
``run_agent`` calls themselves — that's where the wall-clock win lives.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from .base import Tool, ToolContext, safe_input, truncate_for_log

logger = logging.getLogger(__name__)


class NoSubmissionError(RuntimeError):
    """Model finished without ever calling the terminal tool."""


class TooManyTurnsError(RuntimeError):
    """Loop exceeded the configured max_turns."""


class TooManyToolCallsError(RuntimeError):
    """Loop exceeded the configured max_tool_calls."""


@dataclass
class AgentRunResult:
    """The outcome of one ``run_agent`` invocation.

    submission: payload from the terminal tool when one was called.
    turns: number of model turns consumed.
    tool_calls: count of tool dispatches across the run.
    tool_log: per-tool-call audit trail; useful for prompt tuning.
    """

    submission: Optional[Dict[str, Any]] = None
    turns: int = 0
    tool_calls: int = 0
    tool_log: List[Dict[str, Any]] = field(default_factory=list)
    failure: Optional[Dict[str, Any]] = None


def _format_tool_result(output: Any) -> str:
    try:
        return json.dumps(output, default=str)
    except (TypeError, ValueError):
        return json.dumps({"error": "tool result not JSON-serializable"})


async def _dispatch_tool_call(
    tool_call: Any,
    registry: Dict[str, Tool],
    ctx: ToolContext,
    log_tag: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """Run one tool_call. Returns (message-for-openai, audit-entry, is_terminal)."""
    name = tool_call.function.name
    raw_args = tool_call.function.arguments
    arguments = safe_input(raw_args)
    started = time.monotonic()
    is_terminal = False
    error: Optional[str] = None
    output: Any
    tool = registry.get(name)
    if tool is None:
        output = {"error": f"unknown tool: {name}"}
        error = "unknown_tool"
    else:
        try:
            result = await tool.execute(arguments, ctx)
            output = result.output
            is_terminal = result.is_terminal or tool.is_terminal
        except Exception as exc:
            logger.exception("%s tool '%s' raised", log_tag, name)
            output = {"error": f"{exc.__class__.__name__}: {exc}"}
            error = exc.__class__.__name__

    duration_ms = int((time.monotonic() - started) * 1000)
    audit_entry = {
        "name": name,
        "arguments": arguments,
        "duration_ms": duration_ms,
        "error": error,
        "is_terminal": is_terminal,
        "output_preview": truncate_for_log(output, 600),
    }
    logger.info(
        "%s tool=%s ms=%d err=%s args=%s",
        log_tag,
        name,
        duration_ms,
        error or "-",
        truncate_for_log(arguments, 200),
    )
    message = {
        "role": "tool",
        "tool_call_id": tool_call.id,
        "content": _format_tool_result(output),
    }
    return message, audit_entry, is_terminal


async def run_agent(
    *,
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    registry: Dict[str, Tool],
    ctx: ToolContext,
    max_turns: int = 6,
    max_tool_calls: int = 16,
    reasoning_effort: Optional[str] = None,
    log_tag: Optional[str] = None,
) -> AgentRunResult:
    """Drive an OpenAI tool-calling loop until the agent submits or fails.

    Returns an ``AgentRunResult``. The loop never raises for a model-level
    failure (no submission, malformed output, exhausted budget) — those land in
    ``result.failure``. Truly exceptional errors (e.g. transport) propagate.
    """
    log_tag = log_tag or f"agent[{ctx.item_type}:{ctx.candidate.get('uid')}]"
    result = AgentRunResult()

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tools_payload = [t.to_openai() for t in registry.values()]

    while True:
        if result.turns >= max_turns:
            result.failure = {
                "code": "too_many_turns",
                "message": f"agent exceeded {max_turns} turns without submitting",
                "stage": "recommendation",
            }
            logger.warning("%s exhausted turns=%d", log_tag, result.turns)
            return result

        result.turns += 1
        request_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools_payload,
        }
        if reasoning_effort:
            request_kwargs["reasoning_effort"] = reasoning_effort

        logger.info(
            "%s turn=%d sending messages=%d", log_tag, result.turns, len(messages)
        )
        try:
            resp = await client.chat.completions.create(**request_kwargs)
        except Exception as exc:
            logger.exception("%s openai request failed", log_tag)
            result.failure = {
                "code": exc.__class__.__name__,
                "message": str(exc),
                "stage": "request",
            }
            return result

        choice = resp.choices[0]
        message = choice.message
        tool_calls = message.tool_calls or []

        assistant_msg: Dict[str, Any] = {"role": "assistant"}
        if message.content:
            assistant_msg["content"] = message.content
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            content_preview = truncate_for_log(message.content, 200)
            logger.warning(
                "%s turn=%d no tool calls, finish=%s content=%s",
                log_tag,
                result.turns,
                choice.finish_reason,
                content_preview,
            )
            result.failure = {
                "code": "no_tool_call",
                "message": "model returned no tool call",
                "stage": "recommendation",
                "finish_reason": choice.finish_reason,
            }
            return result

        if result.tool_calls + len(tool_calls) > max_tool_calls:
            result.failure = {
                "code": "too_many_tool_calls",
                "message": f"agent exceeded {max_tool_calls} tool calls",
                "stage": "recommendation",
            }
            logger.warning("%s exhausted tool budget at turn=%d", log_tag, result.turns)
            return result

        dispatch = await asyncio.gather(
            *(_dispatch_tool_call(tc, registry, ctx, log_tag) for tc in tool_calls)
        )

        terminal_payload: Optional[Dict[str, Any]] = None
        for (msg, audit, is_terminal), tc in zip(dispatch, tool_calls):
            messages.append(msg)
            result.tool_log.append(audit)
            result.tool_calls += 1
            if is_terminal and terminal_payload is None:
                if isinstance(audit["output_preview"], str):
                    pass
                terminal_payload = safe_input(tc.function.arguments)

        if terminal_payload is not None:
            result.submission = terminal_payload
            logger.info(
                "%s submitted recommend=%s score=%s",
                log_tag,
                terminal_payload.get("recommend"),
                terminal_payload.get("score"),
            )
            return result
