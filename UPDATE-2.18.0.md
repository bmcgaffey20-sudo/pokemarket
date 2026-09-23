# PokeMarket Backend 2.18.0 — multi-market marketplace

Deploy this backend before Android 0.21.0. After Render finishes, confirm
`/api/v1/health` reports `2.18.0-multi-market`.

## Included

- Pokémon, Magic: The Gathering, and Sports listing categories, each divided
  into Graded and Ungraded inventory.
- Category-aware scan jobs and Gemini prompts. Pokémon retains TCGdex grounding;
  Magic and Sports use conservative visual identification without claiming an
  external catalog or certificate verification.
- Graded listings require a seller-reported grading company, label grade, and
  certification number before publication.
- Marketplace and top-viewed endpoints accept `market` and `grading_status`
  filters. Existing checkout, Stripe holds, returns, notifications, reporting,
  cloud photos, deletion, and admin controls continue to use the same listing
  and order records across every market.
- Public listings expose a computed asking-price tier: Tier 1 through $80,
  Tier 2 through $100, Tier 3 through $200, Tier 4 through $250, and Tier 5
  above $250. This is the minimum seller tier required by the asking price.

## Safe database migration

Startup adds the new listing and scan-job fields with Pokémon/Ungraded defaults,
so existing cards remain available. No manual SQL migration is required. The
new `(market, grading_status, status, view_count)` index supports filtered home
and browse feeds.

## Verification

Run `pytest -q`. This release includes boundary tests for all five price tiers,
all six market/grading scan paths, filtered publication, and required slab data.
