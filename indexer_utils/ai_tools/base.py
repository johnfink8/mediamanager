from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional


@dataclass
class ToolContext:
    """Per-agent-run context passed into every tool call.

    item_type: "mv" or "tv". Tools scope their data to this type so the model
    can stay agnostic about which Weaviate collection / DB filter is in play.
    candidate: the item being scored {uid, title, year, genres, attributes,
    synopsis}. Tools use this to avoid recommending the candidate itself in
    similarity results.
    """

    item_type: str
    candidate: Dict[str, Any]
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    output: Any
    is_terminal: bool = False


@dataclass
class TerminalToolResult(ToolResult):
    is_terminal: bool = True


ToolExecute = Callable[[Dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    execute: ToolExecute
    is_terminal: bool = False

    def to_openai(self) -> Dict[str, Any]:
        """Render this tool as an OpenAI Chat Completions tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


def truncate_for_log(value: Any, limit: int = 400) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... <truncated {len(text) - limit} chars>"


def safe_input(arguments: Optional[str]) -> Dict[str, Any]:
    """Parse OpenAI tool_call.arguments JSON, tolerating malformed payloads."""
    import json

    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}
