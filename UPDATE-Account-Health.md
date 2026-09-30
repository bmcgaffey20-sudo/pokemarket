# Account Health update

Backend 2.25.0-account-health; Android 0.28.0-account-health (version code 138).

Account page now has Account Health. Admin panel has Account warnings with paginated results and links to user moderation (ban, suspend 7/30 days, restore).

Targets: on-time tracking >=96%; buyer disputes/refunds <=2%; chargebacks <=2%; successful transactions >=95%. All-time rates; no history is N/A, no warning. Strict boundaries use unrounded rates. Tracking includes paid overdue orders; successful transactions uses finalized outcomes, excluding unfinished sales. Requested returns count as incidents even if later denied; each affected sale counts once. No automatic suspension is performed.

Enable charge.dispute.created, charge.dispute.updated, charge.dispute.closed on the existing Stripe webhook endpoint (/api/v1/payments/webhook). Historical disputes predating this update are not automatically imported. Shared cart-charge disputes affect every order attached to the disputed payment intent because Stripe does not identify a specific cart item. Disputes are retained in the database with no addresses or names. This update does not change payout mechanics.

Deploy all backend files together. New payment_disputes table is created automatically on startup. Rebuild Android with JDK17. Preserve your local.properties and app/google-services.json. Backend test suite: 103 passed. Android source inspected; no Android SDK available here, so APK compilation must run in Android Studio.
