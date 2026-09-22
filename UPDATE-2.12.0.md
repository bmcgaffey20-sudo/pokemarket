# PokeMarket Backend 2.12.0 — complete listings

- Repairs the temporary 0.15.0 split between photo-only and metadata-only drafts.
- Keeps photos, AI analysis, title, description, condition, and price on one listing.
- Adds authenticated permanent listing deletion with R2 object cleanup.
- Blocks deletion when checkout or order history must be retained.

Deploy this backend before installing Android 0.15.1.
