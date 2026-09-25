"""Per-run turn budget: a turns-remaining note on every turn, no tools on the last.

The local model loses track of how many turns it has left and gets cut off
by ``MaxTurnsExceeded`` mid-research, throwing away everything it gathered.
``TurnBudget`` hand-holds it by wrapping the agent's model for one run, so
every model call (one per turn) passes through it:

- A short note is appended to the end of the call's input. The end, so the
  cached prompt prefix is untouched; it is added to the outgoing request
  only, never to the run's history, so each call carries exactly one note,
  the current one. It is a ``developer`` message because Qwen's chat
  template (applied inside vLLM) rejects any ``system`` message that isn't
  first.
- On the final turn the call goes out with no tools, and with the
  conversation rewritten as a plain write-up request (``_as_write_up``),
  so the model's only move is to answer. The final turn comes early when the context fills up:
  once the outgoing input passes ``FINALIZE_FRACTION`` of the model's
  window, that call is the last, leaving room for the model's reasoning
  and its answer. Out of room, the model returns an empty reply and the
  run dies with nothing, however many turns remain. Each note also shows
  how much of the window is used, so the model can pace itself.

This is done at the model rather than with ``FunctionTool.is_enabled``
because the SDK re-checks ``is_enabled`` when it *executes* a tool call, and
that check can't be told apart from the next turn's offer: disabling tools
for the final turn would also reject the calls made on the turn before it.
"""

import dataclasses
import json
import logging
from typing import Any, AsyncIterator, Optional

from agents import Agent
from agents.models.interface import Model, ModelProvider
from decouple import config

logger = logging.getLogger(__name__)

# The chat model's window, input + output (vLLM ``max_model_len``).
CONTEXT_TOKENS = int(config("OPENAI_CONTEXT_TOKENS", default=100_000))
FINALIZE_FRACTION = 0.65
# Conservative chars-per-token for the estimate: it errs toward counting
# more tokens than the tokenizer will, so the final turn comes early
# rather than late.
_CHARS_PER_TOKEN = 3.5

# The final call answers without thinking. Left to reason over a full
# context with no tools, Qwen deliberates until it runs out of window and
# never writes the answer. ``enable_thinking`` is Qwen's chat-template
# switch, passed through by vLLM; the output cap bounds the answer itself
# (a dossier is ~12k chars, ~3.5k tokens).
_NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}
FINAL_MAX_TOKENS = 8_000


FINAL_RETRIES = 2
_NO_TOOLS_RETRY = (
    "Tools are unavailable on this turn: the tool calls in your last reply "
    "were discarded and nothing ran. Reply with the complete final answer "
    "only, written from what you have already gathered."
)


def _as_write_up(items: list[Any]) -> list[Any]:
    """The final call's input: the conversation, tool traffic as plain text.

    As long as the input reads as a tool-calling conversation, the model
    keeps calling tools — even with none offered and a note saying so. So
    the final call gets the task and the research as prose instead: every
    tool call and its result becomes one "research gathered" message,
    alongside any notes the model wrote, in order. Replayed reasoning is
    dropped, which also frees context for the answer.
    """
    kept: list[Any] = []
    research: list[str] = []
    calls: dict[Any, str] = {}
    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind == "function_call":
            calls[item.get("call_id")] = f"{item.get('name')}({item.get('arguments')})"
        elif kind == "function_call_output":
            call = calls.get(item.get("call_id"), "tool call")
            research.append(f"### {call}\n{item.get('output')}")
        elif kind == "message" and item.get("role") == "assistant":
            text = "".join(
                str(part.get("text") or "")
                for part in item.get("content") or []
                if isinstance(part, dict)
            ).strip()
            if text:
                research.append(f"### your note\n{text}")
        else:
            kept.append(item)
    if research:
        kept.append(
            {
                "role": "user",
                "content": "Research gathered so far — the tool calls you made "
                "and what they returned:\n\n" + "\n\n".join(research),
            }
        )
    return kept


def _tool_calls(response: Any) -> list[Any]:
    return [o for o in response.output if getattr(o, "type", None) == "function_call"]


def _final_settings(model_settings: Any) -> Any:
    """``model_settings`` for the final call: no thinking, bounded output."""
    extra = model_settings.extra_body
    return dataclasses.replace(
        model_settings,
        extra_body={**(extra if isinstance(extra, dict) else {}), **_NO_THINKING},
        max_tokens=FINAL_MAX_TOKENS,
    )


# mypy.ini skips following ``agents.*``, so ``Model`` reads as ``Any`` here.
class _BudgetedModel(Model):  # type: ignore[misc]
    """Delegates to ``inner``, applying the budget to every request."""

    def __init__(self, inner: Model, budget: "TurnBudget") -> None:
        self._inner = inner
        self._budget = budget

    async def get_response(
        self,
        system_instructions: Any,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        **kwargs: Any,
    ) -> Any:
        input, tools = self._budget.apply(input, tools, system_instructions)
        if tools:
            return await self._inner.get_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                **kwargs,
            )

        # Final call. The model still emits tool calls here — imitating the
        # history, and vLLM's server-side parser turns them into real
        # ``function_call`` items even though no tools were sent — which the
        # SDK treats as a tool turn and runs straight past the turn cap. So
        # retry with a pointed note, then strip whatever calls remain so the
        # reply is taken as the answer.
        model_settings = _final_settings(model_settings)
        for attempt in range(FINAL_RETRIES + 1):
            response = await self._inner.get_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                tracing,
                **kwargs,
            )
            if not _tool_calls(response) or attempt == FINAL_RETRIES:
                break
            logger.warning(
                "turn budget: final call %d/%d attempted %d tool calls; retrying",
                self._budget.calls,
                self._budget.max_turns,
                len(_tool_calls(response)),
            )
            input = [*input, {"role": "developer", "content": _NO_TOOLS_RETRY}]
        response.output = [
            o for o in response.output if getattr(o, "type", None) != "function_call"
        ]
        if not any(
            getattr(item, "type", None) == "message" for item in response.output
        ):
            usage = response.usage
            logger.warning(
                "turn budget: final call %d/%d returned no answer "
                "(input_tokens=%s output_tokens=%s items=%s)",
                self._budget.calls,
                self._budget.max_turns,
                getattr(usage, "input_tokens", None),
                getattr(usage, "output_tokens", None),
                [type(item).__name__ for item in response.output],
            )
        return response

    def stream_response(
        self,
        system_instructions: Any,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        handoffs: Any,
        tracing: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        input, tools = self._budget.apply(input, tools, system_instructions)
        if not tools:
            model_settings = _final_settings(model_settings)
        stream: AsyncIterator[Any] = self._inner.stream_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        )
        return stream


class TurnBudget:
    """Turn accounting for one ``Runner.run``; build a fresh one per run."""

    def __init__(self, max_turns: int, context_tokens: int = CONTEXT_TOKENS) -> None:
        self.max_turns = max_turns
        self.calls = 0
        self.finalize_at = int(context_tokens * FINALIZE_FRACTION)

    def prepare(
        self, agent: Agent[Any], provider: Optional[ModelProvider] = None
    ) -> Agent[Any]:
        """Clone ``agent`` onto a budgeted wrapper of its model.

        ``provider`` resolves a model *name* (the usual case) into the
        ``Model`` to wrap — pass the same provider the run uses.
        """
        inner = agent.model
        if not isinstance(inner, Model):
            if provider is None:
                raise ValueError("a provider is needed to resolve a model name")
            inner = provider.get_model(inner)
        return agent.clone(model=_BudgetedModel(inner, self))

    def note(self, turn: int, context_tokens: int = 0) -> str:
        """The per-turn note; ``context_tokens`` is the estimated input size."""
        used = (
            f" About {context_tokens // 1000}k of {CONTEXT_TOKENS // 1000}k "
            "context tokens used."
        )
        left = self.max_turns - turn
        full = context_tokens >= self.finalize_at
        if full or left <= 0:
            why = (
                f"your context is nearly full ({used.strip()})"
                if full
                else "this is the last turn"
            )
            return (
                f"FINAL TURN ({turn} of {self.max_turns}): {why}. Tools are "
                "gone and nothing runs after this message: what you write now "
                "is delivered as-is as your final answer. Write the complete "
                "final answer in this message, from what you have already "
                "gathered — not a plan, not a summary of what you collected, "
                'not "let me compile".'
            )
        if left == 1:
            return (
                f"Turn {turn} of {self.max_turns}. This is your last turn with "
                "tools: make any final calls now. Next turn tools are removed "
                "and you must give your final answer." + used
            )
        return (
            f"Turn {turn} of {self.max_turns}: {left} more turns after this "
            f"one. Tools are removed on turn {self.max_turns}, where you must "
            "give your final answer — pace your work to finish by then. Every "
            "fetched page stays in your context; tools are also removed early "
            "if it fills up." + used
        )

    def apply(
        self, input: Any, tools: Any, system_instructions: Optional[str] = None
    ) -> tuple[list[Any], list[Any]]:
        """Count one turn; return the outgoing input and tools for it."""
        self.calls += 1
        items = (
            [{"role": "user", "content": input}] if isinstance(input, str) else input
        )
        chars = len(json.dumps(items, default=str)) + len(system_instructions or "")
        context_tokens = int(chars / _CHARS_PER_TOKEN)
        note = {"role": "developer", "content": self.note(self.calls, context_tokens)}
        final = self.calls >= self.max_turns or context_tokens >= self.finalize_at
        if final:
            return [*_as_write_up(items), note], []
        return [*items, note], list(tools)
