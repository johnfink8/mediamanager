"""strip_preamble — drop the local model's status line before a dossier.

The openings below are real first paragraphs from live subagent runs.
"""

import pytest

from indexer_utils.ai_tools.shared import strip_preamble

BODY = "DOSSIER: Spa Weekend (2026)\nRotten Tomatoes 29%."


@pytest.mark.parametrize(
    "preamble",
    [
        "All sources gathered; IMDb is bot-walled on every path, so it's flagged "
        "as unconfirmed. Here's the dossier.",
        "All lookups resolved cleanly (no errors). Tallying against each career "
        "sample and writing the dossier.",
        "All sources gathered. Compiling the dossier.",
    ],
)
def test_narration_is_dropped(preamble: str) -> None:
    assert strip_preamble(f"{preamble}\n\n{BODY}") == BODY


@pytest.mark.parametrize(
    "opening",
    [
        "US THEATRICAL RELEASE DOSSIER — WINDOW: 2026-09-09 through 2026-10-07 "
        "(compiled 2026-09-23)",
        "US TV-RELEASE DOSSIER — WINDOW: 2026-09-09 through 2026-10-07 "
        "(compiled 2026-09-23)",
        "Leslie Mann: 16 of 25 works are in your library — followed.",
        "Douglas Booth: 7 of 25 works are in your library — selective.",
    ],
)
def test_real_openings_are_kept(opening: str) -> None:
    text = f"{opening}\n\n{BODY}"
    assert strip_preamble(text) == text


def test_a_lone_paragraph_is_never_emptied() -> None:
    assert strip_preamble("Here's the dossier.") == "Here's the dossier."
