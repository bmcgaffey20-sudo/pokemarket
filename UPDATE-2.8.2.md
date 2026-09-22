# PokeMarket backend 2.8.2 — First successful payment wins

- Opening Stripe Checkout no longer reserves or removes a one-of-one listing.
- Multiple buyers may reach Checkout; the first confirmed successful payment
  atomically claims the card.
- If two payments complete during a race, the losing payment is automatically
  refunded with an idempotent Stripe refund.
- Abandoned Checkout sessions no longer block later buyers.
- No Android update is required for this release.
