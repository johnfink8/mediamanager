from agents import set_default_openai_key
from decouple import config

from .agent import AgentRunResult, Recommendation, build_agent, run_recommendation
from .base import ToolContext

# The SDK reads OPENAI_API_KEY from os.environ on first use; this project
# stores secrets in .env via python-decouple, which doesn't export to the
# process environment. Inject the resolved key explicitly so SDK runs work
# without forcing callers to mirror .env into the environment. The Agent
# constructors above don't touch the client, so the setter still runs
# before any Runner.run call.
_api_key = config("OPENAI_API_KEY", default="")
if _api_key:
    set_default_openai_key(_api_key, use_for_tracing=False)

__all__ = [
    "AgentRunResult",
    "Recommendation",
    "ToolContext",
    "build_agent",
    "run_recommendation",
]
