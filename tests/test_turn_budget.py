"""TurnBudget — turns-remaining note on every call, tools removed on the last.

Drives a real ``Runner.run`` with a scripted model that calls a tool
whenever one is offered, so the run only ends because the budget took the
tools away. No DB, no network.
"""

from typing import Any, List

import pytest
from agents import Agent, RunConfig, Runner, function_tool
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from indexer_utils.ai_tools.turn_budget import (
    FINAL_MAX_TOKENS,
    FINAL_RETRIES,
    TurnBudget,
)


@function_tool
def ping() -> str:
    """Return pong."""
    return "pong"


class GreedyModel(Model):
    """Calls the first offered tool every turn; answers only when it can't."""

    def __init__(self) -> None:
        self.calls: List[dict[str, Any]] = []

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
    ) -> ModelResponse:
        n = len(self.calls) + 1
        self.calls.append(
            {
                "input": list(input),
                "tools": [t.name for t in tools],
                "settings": model_settings,
            }
        )
        if tools:
            out: List[Any] = [
                ResponseFunctionToolCall(
                    id=f"fc{n}",
                    call_id=f"call{n}",
                    name=tools[0].name,
                    arguments="{}",
                    type="function_call",
                )
            ]
        else:
            out = [
                ResponseOutputMessage(
                    id=f"msg{n}",
                    content=[
                        ResponseOutputText(
                            text="final answer", type="output_text", annotations=[]
                        )
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ]
        return ModelResponse(output=out, usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class StubbornModel(GreedyModel):
    """Keeps calling ``ping`` on the no-tools turn for ``stubborn`` attempts."""

    def __init__(self, stubborn: int) -> None:
        super().__init__()
        self.stubborn = stubborn

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        tools = args[3]
        response = await super().get_response(*args, **kwargs)
        if not tools and self.stubborn > 0:
            self.stubborn -= 1
            response.output.append(
                ResponseFunctionToolCall(
                    id="stray",
                    call_id="stray",
                    name="ping",
                    arguments="{}",
                    type="function_call",
                )
            )
        return response


def _developer_notes(items: List[Any]) -> List[str]:
    return [
        i["content"]
        for i in items
        if isinstance(i, dict) and i.get("role") == "developer"
    ]


async def test_tools_removed_on_final_turn_and_one_note_per_call() -> None:
    model = GreedyModel()
    budget = TurnBudget(4)
    agent = budget.prepare(Agent(name="t", instructions="x", model=model, tools=[ping]))

    # Turn 3's tool call must still execute even though turn 4 offers no
    # tools — the SDK re-checks tool availability at execution time.
    result = await Runner.run(
        agent, "go", max_turns=4, run_config=RunConfig(tracing_disabled=True)
    )

    assert result.final_output == "final answer"
    assert [c["tools"] for c in model.calls] == [["ping"], ["ping"], ["ping"], []]
    for turn, call in enumerate(model.calls, start=1):
        # exactly one note (earlier ones aren't persisted), and it is last
        notes = _developer_notes(call["input"])
        assert len(notes) == 1 and f"{turn} of 4" in notes[0]
        assert call["input"][-1]["role"] == "developer"
    # only the final call answers without thinking, under an output cap
    for call in model.calls[:-1]:
        assert call["settings"].max_tokens is None
    final = model.calls[-1]["settings"]
    assert final.extra_body["chat_template_kwargs"] == {"enable_thinking": False}
    assert final.max_tokens == FINAL_MAX_TOKENS
    # the prefix up to the note grows append-only, so it stays cacheable
    first = model.calls[0]["input"][:-1]
    assert model.calls[1]["input"][: len(first)] == first


async def test_full_context_ends_the_run_early() -> None:
    model = GreedyModel()
    # A window so small the first tool result already fills it.
    budget = TurnBudget(10, context_tokens=60)
    agent = budget.prepare(Agent(name="t", instructions="x", model=model, tools=[ping]))

    result = await Runner.run(
        agent, "go", max_turns=10, run_config=RunConfig(tracing_disabled=True)
    )

    assert result.final_output == "final answer"
    assert [c["tools"] for c in model.calls] == [["ping"], []]
    assert "context is nearly full" in _developer_notes(model.calls[1]["input"])[0]


@pytest.mark.parametrize("stubborn", [1, 5])
async def test_stray_tool_calls_on_final_turn_never_overrun(stubborn: int) -> None:
    model = StubbornModel(stubborn)
    budget = TurnBudget(2)
    agent = budget.prepare(Agent(name="t", instructions="x", model=model, tools=[ping]))

    result = await Runner.run(
        agent, "go", max_turns=2, run_config=RunConfig(tracing_disabled=True)
    )

    # answered instead of raising MaxTurnsExceeded, even when the model
    # never stops calling tools
    assert result.final_output == "final answer"
    finals = model.calls[1:]
    assert len(finals) == min(stubborn, FINAL_RETRIES) + 1
    # each retry carries the discarded-calls note
    for call in finals[1:]:
        assert any("were discarded" in n for n in _developer_notes(call["input"]))


async def test_final_call_gets_research_as_prose() -> None:
    model = GreedyModel()
    budget = TurnBudget(3)
    agent = budget.prepare(Agent(name="t", instructions="x", model=model, tools=[ping]))

    await Runner.run(
        agent, "go", max_turns=3, run_config=RunConfig(tracing_disabled=True)
    )

    final = model.calls[-1]["input"]
    kinds = {i.get("type") for i in final if isinstance(i, dict)}
    # nothing left for the model to imitate: no tool calls, no reasoning
    assert not kinds & {"function_call", "function_call_output", "reasoning"}
    assert final[0] == {"content": "go", "role": "user"}
    research = final[-2]["content"]
    assert research.startswith("Research gathered so far")
    assert research.count("### ping({})\npong") == 2
    assert final[-1]["role"] == "developer"


def test_source_agent_is_not_mutated() -> None:
    model = GreedyModel()
    source = Agent(name="t", model=model, tools=[ping])
    budgeted = TurnBudget(1).prepare(source)
    assert source.model is model
    assert budgeted.model is not model
    assert budgeted.tools == [ping]


def test_model_name_needs_a_provider() -> None:
    with pytest.raises(ValueError):
        TurnBudget(1).prepare(Agent(name="t", model="qwen3.8"))


def test_note_wording_by_position() -> None:
    b = TurnBudget(5)
    assert "4 more turns" in b.note(1)
    assert "last turn with tools" in b.note(4)
    assert "FINAL TURN" in b.note(5)
    assert "nothing runs after this message" in b.note(5)
    assert "About 12k of" in b.note(1, context_tokens=12_345)
    assert "nearly full" in b.note(1, context_tokens=b.finalize_at)
