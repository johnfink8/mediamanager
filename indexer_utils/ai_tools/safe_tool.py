"""``@safe_tool`` — drop-in replacement for ``@function_tool`` with saner errors.

The SDK's default failure handler returns a generic prose string ("An error
occurred while running the tool. Please try again. Error: ..."). Our tools
themselves already use a ``{"error": "..."}`` dict shape for in-band failures
(missing required field, no rows matched, etc). Mixing the two shapes means
the model sees two different error vocabularies depending on whether the
failure was caught in user code or surfaced by the SDK.

``@safe_tool`` swaps in a ``failure_error_function`` that emits the same
JSON-shaped error payload — ``{"error": ..., "error_type": ..., "tool": ...}``
— so every error a tool can produce reads the same way to the model. It also
logs the exception with its traceback, which the default handler drops.
"""

import json
import logging
from typing import Any, Callable

from agents import RunContextWrapper, function_tool
from agents.tool import FunctionTool

logger = logging.getLogger(__name__)

# Cap the model-facing message tight. The full traceback already went to
# stderr via logger; the model only needs enough to decide retry vs. skip.
_MAX_MESSAGE_CHARS = 160


def _short_message(error: Exception) -> str:
    """First non-empty line of ``str(error)``, capped, with SQLAlchemy's docs
    suffix stripped — that 'Background on this error at: https://...' tail
    is pure noise to a downstream LLM.
    """
    text = str(error).strip()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("(Background on this error"):
            continue
        if len(line) > _MAX_MESSAGE_CHARS:
            line = line[: _MAX_MESSAGE_CHARS - 1] + "…"
        return line
    return error.__class__.__name__


def _make_failure_handler(
    tool_name: str,
) -> Callable[[RunContextWrapper[Any], Exception], str]:
    def handle(ctx: RunContextWrapper[Any], error: Exception) -> str:
        logger.error("tool %s failed", tool_name, exc_info=error)
        return json.dumps(
            {
                "error_type": error.__class__.__name__,
                "message": _short_message(error),
            }
        )

    return handle


def safe_tool(
    func: Callable[..., Any] | None = None, **kwargs: Any
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    """Decorator: same surface as ``@function_tool`` with structured failures.

    Use as ``@safe_tool`` or ``@safe_tool(...)``. Forwards all keyword args
    (``strict_mode``, ``is_enabled``, ``timeout``, etc.) to ``@function_tool``;
    ``failure_error_function`` is the only one we set ourselves and it can't
    be overridden from a caller (that's the whole point).
    """
    if "failure_error_function" in kwargs:
        raise TypeError(
            "safe_tool sets failure_error_function itself; pass it via "
            "function_tool directly if you need a per-tool override."
        )

    def wrap(f: Callable[..., Any]) -> FunctionTool:
        return function_tool(
            f,
            failure_error_function=_make_failure_handler(f.__name__),
            **kwargs,
        )

    if func is not None and callable(func):
        return wrap(func)
    return wrap
