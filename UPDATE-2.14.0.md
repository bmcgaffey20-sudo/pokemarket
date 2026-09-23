# PokeMarket Backend 2.14.0 — automatic settlement safeguards

- Outbound tracking starts a durable 10-day buyer confirmation deadline.
- If the buyer neither confirms delivery nor requests a return by the deadline,
  the order completes and the Stripe seller transfer is issued automatically.
- Existing confirmed-delivery protection holds now complete automatically when
  their 10-day deadline expires.
- Return tracking starts a durable 10-day seller confirmation deadline.
- If the seller does not confirm return receipt, the backend confirms receipt,
  refunds the buyer through Stripe, and restores the listing as a private draft.
- Buyers may request a return while a shipped order is awaiting confirmation.
- Stripe transfers and refunds use stable idempotency keys and retry safely.
- Existing in-flight shipped orders receive deadlines based on their last update,
  so deployment does not restart their clocks.
- The settlement worker checks every 60 seconds while Render is awake. On the
  free tier, overdue work runs when the service next wakes.

No new secret is required. Optional Render settings are:

`CONFIRMATION_TIMEOUT_DAYS=10`

`SETTLEMENT_WORKER_POLL_SECONDS=60`

Keep the timeout at 10 for production. A shorter value may be used temporarily
in a staging database to test the automatic paths.
