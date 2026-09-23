# PokeMarket Backend 2.16.0 — viewed listings and rarity tiers

- Adds a public `view_count` to every published listing.
- Counts a view when listing detail is opened, not when a search result renders.
- Deduplicates repeated opens from the same app installation per listing per UTC
  day using a one-way server hash. Browser fallback uses a one-way network/device
  signature; raw IP addresses are not stored.
- Adds `GET /api/v1/marketplace/top?limit=10` for the Top Viewed Cards homepage.
- Adds AI rarity classification and normalizes TCGdex/Gemini labels into:
  `rare`, `double_rare`, `ultra_rare`, `illustration_rare`,
  `special_illustration_rare`, and `hyper_illustration_rare`.
- Existing listings default safely to `rare`; saving a new AI result updates the
  listing's persisted rarity tier.
- Removes view-deduplication rows after 90 days while retaining the aggregate
  public view count.

No new Render environment variable is required. Startup safely adds the new
columns, indexes, and view-deduplication table.
