# PokeMarket Backend 2.15.0 — beta operations

- Adds administrator sale list, sale detail, audit history, and diagnostics APIs.
- Stores dedicated seller return addresses in PokeMarket and snapshots them on
  each order; Stripe identity data is not copied.
- Hides the seller address from a buyer until a return is approved.
- Redacts buyer and seller shipping-address snapshots after 30 days while
  retaining financial totals, payment state, tracking metadata, and audit events.
- Emails one CSV for each completed seven-day UTC sales period. Set
  `ADMIN_REPORT_EMAIL` in Render to the private recipient address.
- Sends three-day and one-day deadline reminders for delivery confirmation,
  seller payout holds, and return receipt confirmation.
- Normalizes USPS/UPS/FedEx tracking numbers and prevents reuse across orders.
- Adds seller-address and reporting columns with safe startup migrations.

Required new Render variable:

`ADMIN_REPORT_EMAIL=<private report recipient>`

Recommended setting:

`SALE_DETAIL_RETENTION_DAYS=30`

On Render's free tier, reminders, reports, redaction, and overdue settlements run
while the service is awake and catch up on the next request after a sleep.
