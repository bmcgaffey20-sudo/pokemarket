# PokeMarket backend 2.9.0 — Orders

- Enriches authorized order responses with card titles and buyer/seller display names.
- Collects a US shipping address during Stripe Checkout and saves it after the
  signed payment webhook succeeds.
- Includes shipping in seller proceeds while retaining the 4% item commission.
- Supports seller tracking, buyer delivery confirmation, the 10-day protection
  hold, completion, seller trust progression and Stripe payout release.
- Makes completion and payout retries idempotent so trust counters and transfers
  cannot be duplicated.
- Safely migrates existing databases with the new private shipping fields.

Deploy this backend before installing Android 0.13.0. Verify `/api/v1/health`
reports `"version":"2.9.0-orders"`.
