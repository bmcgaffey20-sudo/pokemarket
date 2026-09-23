"""Shared marketplace classification rules, independent of external services."""

MARKETS = {"pokemon", "magic", "sports"}
GRADING_STATUSES = {"graded", "ungraded"}
SELLER_PRICE_LIMITS = {1: 8_000, 2: 10_000, 3: 20_000, 4: 25_000, 5: 100_000}


def price_tier(price_cents):
    """Minimum selling tier required by the asking price (not seller identity)."""
    amount = price_cents or 0
    for tier, limit in SELLER_PRICE_LIMITS.items():
        if amount <= limit:
            return tier
    return 5
