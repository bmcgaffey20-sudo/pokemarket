# PokeMarket backend 2.8.0 — Stripe checkout and order protection

- Added Stripe Checkout test-mode sessions and signed webhook confirmation.
- Added Stripe Express seller onboarding for marketplace payouts.
- Added PostgreSQL orders with item amount, 4% commission, shipping, payment
  status, tracking, delivery confirmation, 10-day buyer-protection hold, and
  payout status.
- Added seller payout transfer after the hold and trust-tier progression after
  successful orders.
- Added authenticated order list/detail, tracking, delivery, and completion
  routes.
- Android 0.12.0 opens hosted Stripe Checkout from marketplace listings.
