# Publish Listing — backend 2.6.0

Deploy this backend before installing Android 0.10.0 (versionCode 106).
Keep your existing Render environment variables, including DATABASE_URL,
AUTH_SECRET, LEGACY_CLAIM_CODE and all R2/AI settings. No new secret is needed.

Upload every backend source file from this ZIP to the repository root, then
deploy Render. Health should report version 2.6.0-publish-listings and database
connected. The database startup migration adds publication_approved without
deleting accounts, listings or photos. Earlier manually tracked "published"
records become drafts: only explicit publication makes them public.

## New routes

- POST /api/v1/listings/{id}/publish — owner bearer token required.
- POST /api/v1/listings/{id}/unpublish — owner bearer token required.
- GET /api/v1/marketplace?limit=20&offset=0&q= — public, title search.
- GET /api/v1/marketplace/{id} — public, published cards only.

Publication requires title, description, card name, condition, positive USD
price within the seller's tier limit, and all four required persisted photo
records. Published edits are validated too. Replacing photos returns a listing
to draft. Publishing is idempotent; withdrawing retains photos and details.
The public response excludes email, account IDs, AI payloads and object-key
fields. Published photos are exposed through temporary signed URLs; the R2
bucket itself stays private. Withdrawing prevents fresh public links but cannot
revoke previously issued signed URLs, which last until their configured expiry,
or copies already downloaded by viewers.

This is a preview marketplace: no checkout, payments, eBay publishing or purchase
reservation is included. No production service or database was changed by
building this ZIP. Back up the database before deploying schema changes.

## Verification

21 backend tests pass locally using an isolated SQLite test database and mocked
photo URL generation. A live Neon/R2 deployment test is still required.
Run pytest after installing requirements.txt and pytest.
