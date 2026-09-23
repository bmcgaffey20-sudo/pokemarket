# PokeMarket Backend 2.17.0 — popular cards homepage

- The top-viewed endpoint returns at most 20 published listings with views, ranked by public view count. When no published card has a view, it falls back to recently updated published listings.
- Public listings now include `card_type`, sourced first from verified TCGdex types and otherwise from the AI identification. Card types are derived from existing scan JSON; no database migration or Render variable is required.
- Marketplace search includes listing title, Pokémon name, set, and collector number.
- Future scans identify the printed card type when visible, and leave it unknown when inconclusive.

Deploy this backend before Android 0.20.0. Confirm `/api/v1/health` reports `2.17.0-popular-home`.
