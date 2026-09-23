# 2.18.3 — carrier selection and tracking links

Shipping and return forms require choosing USPS, UPS or FedEx.
Backend validation checks the selected carrier's supported tracking format.
Carrier-specific selection resolves overlapping USPS/FedEx numeric formats.
Old clients can still omit the carrier and use the previous detection behavior.
Duplicate-tracking safeguards remain in place.
Order and admin sale details link outbound and return numbers to carrier pages.

These are format checks, not live carrier acceptance, service-level, ownership,
destination or delivery verification. No tracking API credentials are required.
Existing deadline and payout behavior is unchanged.

Deploy backend 2.18.3 first, then build Android 0.21.3 with JDK 17.
Keep google-services.json and local.properties. Four capture angles with
two accepted attempts each, the 80% guide, and disabled model selector remain.
