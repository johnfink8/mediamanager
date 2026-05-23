"""Behaviour tests for the ``@safe_tool`` wrapper.

Covers two paths the SDK exercises differently:

1. The function body raises mid-execution.
2. The model passes invalid arguments (caught by the SDK's JSON-schema layer
   before the body ever runs).

In both cases we want the model-facing payload to be a JSON-encoded
``{"error_type", "message"}`` dict — tight enough that the model gets a
useful signal without burning tokens on traceback frames or doc URLs.
"""

import asyncio
import json
import os
from typing import Any, Dict

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-safe-tool")

from agents import RunContextWrapper  # noqa: E402
from agents.tool_context import ToolContext as SdkToolContext  # noqa: E402

from indexer_utils.ai_tools.safe_tool import safe_tool  # noqa: E402


@safe_tool
async def boom(wrapper: RunContextWrapper[Any]) -> Dict[str, Any]:
    """Tool that always raises — used to exercise the failure handler.

    Args: (none)
    """
    raise RuntimeError("kaboom")


@safe_tool
async def takes_int(wrapper: RunContextWrapper[Any], n: int) -> Dict[str, Any]:
    """Echo n. Used to trigger an arg-validation failure.

    Args:
        n: An integer.
    """
    return {"got": n}


def _ctx(tool_name: str, args: str) -> SdkToolContext[Any]:
    # ``on_invoke_tool`` reads tool_name + run_config off the context; build a
    # minimal SDK ToolContext rather than a bare RunContextWrapper.
    return SdkToolContext(
        context=None,
        tool_name=tool_name,
        tool_call_id="test-call-id",
        tool_arguments=args,
    )


def test_body_exception_becomes_structured_error() -> None:
    payload = asyncio.run(boom.on_invoke_tool(_ctx("boom", "{}"), "{}"))
    result = json.loads(payload)
    assert result == {"error_type": "RuntimeError", "message": "kaboom"}


def test_invalid_args_become_structured_error() -> None:
    # ``n`` is required and must be int; pass a string to trip validation.
    payload = asyncio.run(
        takes_int.on_invoke_tool(
            _ctx("takes_int", '{"n": "not-an-int"}'), '{"n": "not-an-int"}'
        )
    )
    result = json.loads(payload)
    assert set(result) == {"error_type", "message"}
    assert result["message"]


def test_long_message_gets_truncated() -> None:
    @safe_tool
    async def shouty(wrapper: RunContextWrapper[Any]) -> Dict[str, Any]:
        """Raises a noisy multi-line error.

        Args: (none)
        """
        raise RuntimeError(
            "first useful line goes here — short and on-topic\n"
            "(Background on this error at: https://example.com/docs/asdf)\n" + "x" * 500
        )

    payload = asyncio.run(shouty.on_invoke_tool(_ctx("shouty", "{}"), "{}"))
    result = json.loads(payload)
    assert result["error_type"] == "RuntimeError"
    # First line wins, SQLAlchemy-style suffix is dropped, no 500-char trail.
    assert result["message"].startswith("first useful line")
    assert "Background on" not in result["message"]
    assert len(result["message"]) <= 160


def test_safe_tool_rejects_explicit_failure_handler() -> None:
    with pytest.raises(TypeError, match="failure_error_function"):

        @safe_tool(failure_error_function=lambda ctx, exc: "nope")
        async def _t(wrapper: RunContextWrapper[Any]) -> Dict[str, Any]:
            """No-op.

            Args: (none)
            """
            return {}
