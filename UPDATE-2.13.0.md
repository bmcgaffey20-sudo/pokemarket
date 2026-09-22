# PokeMarket Backend 2.13.0 — beta control center

This release adds the complete return lifecycle, Stripe return refunds, payout
holds, disputed-return administration, user and seller-tier administration,
Firebase Cloud Messaging delivery, and durable Gemini capacity retries.

New Render variables:

- `ADMIN_EMAILS`: comma-separated PokeMarket account emails allowed to open Admin.
- `FIREBASE_PROJECT_ID`: Firebase project ID.
- `FIREBASE_SERVICE_ACCOUNT_JSON`: the complete service-account JSON on one line.
- `GEMINI_JOB_MAX_ATTEMPTS`: recommended value `3`.
- `GEMINI_RETRY_BASE_SECONDS`: recommended value `12`.

Deploy this backend before installing Android 0.16.0. Database columns and the
device-token table are added automatically without deleting existing records.
