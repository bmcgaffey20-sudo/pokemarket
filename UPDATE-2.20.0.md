# Public user profiles

Deploy backend 2.20.0 before Android 0.23.0. Existing gallery, messaging and feedback
features remain included. No new environment variables are required.

Public profiles expose only user ID, display name, seller tier, join date, calendar
membership age, incident-free transaction counts and paginated seller feedback.
Emails, addresses, payment identifiers and private messages are excluded.

An incident-free buy/sale is a completed non-self order with no recorded return
reason. Pending, refunded and disputed orders are excluded. This is a count of
PokeMarket's recorded history, not a guarantee or an external background check.
Seller-tier counters are not used because those can include resolved returns.

Membership duration uses calendar years, months and remaining days, including
leap-year and month-end handling, from the account's join date in UTC.

Image moderation status: valid-file checks are present, but explicit-content
screening is NOT enabled by this release. Before broader rollout, add a separate
server-side moderation gate to every listing and message image, private quarantine,
blocked high-confidence adult content, manual review for ambiguous results, reporting
and admin enforcement. If the moderation provider is unavailable, keep uploads
pending rather than releasing them. Cloud Vision SafeSearch is one possible provider:
https://docs.cloud.google.com/vision/docs/detecting-safe-search
