from agents import set_default_openai_client, set_default_openai_key
from decouple import config
from openai import AsyncOpenAI

from .agent import AgentRunResult, Recommendation, build_agent, run_recommendation
from .base import ToolContext

# The SDK reads OPENAI_API_KEY / OPENAI_BASE_URL from os.environ on first
# use; this project stores config in .env via python-decouple, which doesn't
# export to the process environment. Inject both explicitly so SDK runs hit
# the configured gateway. The Agent constructors above don't touch the
# client, so the setters still run before any Runner.run call.
_api_key = config("OPENAI_API_KEY", default="")
if _api_key:
    set_default_openai_key(_api_key, use_for_tracing=False)
    set_default_openai_client(
        AsyncOpenAI(
            api_key=_api_key,
            base_url=config("OPENAI_BASE_URL", default=None),
        ),
        use_for_tracing=False,
    )

__all__ = [
    "AgentRunResult",
    "Recommendation",
    "ToolContext",
    "build_agent",
    "run_recommendation",
]
