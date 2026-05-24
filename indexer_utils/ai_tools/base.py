from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class ToolContext:
    """Per-agent-run context passed into every tool call via ``RunContextWrapper.context``.

    item_type: "mv" or "tv". Tools scope their data to this type so the model
    can stay agnostic about which DB filter is in play.
    candidate: the item being scored {uid, title, year, genres}. Tools use this
    to avoid recommending the candidate itself in similarity results.
    """

    item_type: str
    candidate: Dict[str, Any]
    extras: Dict[str, Any] = field(default_factory=dict)
