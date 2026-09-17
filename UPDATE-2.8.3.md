# PokeMarket backend 2.8.3

This release fixes the completed-checkout flow.

- Reads Stripe webhooks from the exact raw request body so Stripe JSON is no
  longer rejected by FastAPI with HTTP 422.
- Verifies the `Stripe-Signature` header before processing payment events.
- Keeps the first-successful-payment inventory behavior introduced in 2.8.2.
- Adds `/checkout/success` and `/checkout/cancelled` browser return pages.
- Return pages reopen the Android app through `pokemarket://account`.

After deployment, verify `/api/v1/health` reports:

`"version":"2.8.3-checkout-return"`

Then perform one new Stripe test purchase. Render should show both:

- `POST /api/v1/payments/webhook` → `200 OK`
- `GET /checkout/success?...` → `200 OK`
