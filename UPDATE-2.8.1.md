# PokeMarket backend 2.8.1 — Stripe Connect return sync

- Added Stripe Connect return and expired-link pages that reopen the Android app.
- `/api/v1/auth/me` now checks the seller's connected Stripe account and reports
  live onboarding, charge, payout, and outstanding-requirement status.
- A stored Stripe account ID no longer counts as completed payout setup by
  itself.
