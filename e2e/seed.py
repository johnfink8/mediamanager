"""Seed test data for playwright e2e tests."""

from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session


TEST_UID_PREFIX = "playwright-test-"


def seed() -> None:
    with db_session() as session:
        session.query(IgnoreItem).filter(
            IgnoreItem.uid.like(f"{TEST_UID_PREFIX}%")
        ).delete(synchronize_session=False)
        session.commit()

    items = [
        # Active movies — appear in Movies tab
        IgnoreItem(
            item_type="mv",
            uid="playwright-test-mv-001",
            title="Playwright Test Movie One",
            ignore=False,
            added=False,
        ),
        IgnoreItem(
            item_type="mv",
            uid="playwright-test-mv-002",
            title="Playwright Test Movie Two",
            ignore=False,
            added=False,
        ),
        # Active TV shows — appear in TV tab
        IgnoreItem(
            item_type="tv",
            uid="playwright-test-tv-001",
            title="Playwright Test Show One",
            ignore=False,
            added=False,
        ),
        IgnoreItem(
            item_type="tv",
            uid="playwright-test-tv-002",
            title="Playwright Test Show Two",
            ignore=False,
            added=False,
        ),
        # Ignored movies — appear in Movie History
        IgnoreItem(
            item_type="mv",
            uid="playwright-test-mv-003",
            title="Dismissed Test Movie",
            ignore=True,
            added=False,
        ),
        IgnoreItem(
            item_type="mv",
            uid="playwright-test-mv-004",
            title="Added Test Movie",
            ignore=True,
            added=True,
        ),
        # Ignored TV shows — appear in Show History
        IgnoreItem(
            item_type="tv",
            uid="playwright-test-tv-003",
            title="Dismissed Test Show",
            ignore=True,
            added=False,
        ),
        IgnoreItem(
            item_type="tv",
            uid="playwright-test-tv-004",
            title="Added Test Show",
            ignore=True,
            added=True,
        ),
    ]
    with db_session() as session:
        session.add_all(items)
        session.commit()
    print(f"Seeded {len(items)} test items")


if __name__ == "__main__":
    seed()
