"""decision() — the one label the agent reads for an item's outcome."""

import pytest

from indexer_utils.ai_tools.shared import decision
from indexer_utils.models import IgnoreItem


@pytest.mark.parametrize(
    "added, ignore, shown, label",
    [
        (True, True, True, "added"),
        (True, True, False, "added"),  # Plex-scanned: kept, never shown
        (False, True, True, "rejected"),
        (False, True, False, "auto_filtered"),  # a filter rule, unseen
        (False, False, True, "pending"),
    ],
)
def test_decision(added: bool, ignore: bool, shown: bool, label: str) -> None:
    item = IgnoreItem(uid="u", item_type="mv", added=added, ignore=ignore, shown=shown)
    assert decision(item) == label
