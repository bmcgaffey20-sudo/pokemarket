# PokeMarket Backend 2.7.1 — Mailjet

Read UPDATE-2.7.1.md first for Mailjet setup, deployment and recovery testing.
UPDATE-2.6.0.md documents publishing and public-photo behavior.

Adds labelled defect close-ups, strict condition inspection, explicit major-defect
reporting, confidence normalization, and a server-side safeguard that prevents a
Near Mint or Lightly Played result when a bend, crease, or dent is detected.

Card photos are stored permanently in the private Cloudflare R2 bucket, while
PostgreSQL stores each listing record and its R2 object keys. The database never
stores the image binary or temporary signed URL.

## Accounts and ownership

- `POST /api/v1/auth/register` creates an account.
- `POST /api/v1/auth/login` returns a signed 30-day bearer session.
- `GET /api/v1/auth/me` verifies the current session.
- Passwords are never stored. They are salted and hashed with PBKDF2-HMAC-SHA256
  using 600,000 iterations.
- Listing reads, writes, photo uploads, and AI scans require authentication.
- Every listing has a `seller_id`; list queries return only the signed-in
  seller's records. Cross-account listing IDs return 404.
- The seller profile starts at Tier 1 with an $80 listing limit. Activity
  counters are present for the later trust-tier promotion rules.
- The optional `legacy_claim_code` attaches still-unowned pre-account listings to
  the original owner. The code must match the private Render environment variable.

## Database records

The service creates three tables on first connection:

- `users`: email, display name, password hash parameters, seller tier, activity
  counters, and listing limit.

- `listings`: draft metadata, card identity, condition, price, status, AI result,
  and timestamps.
- `listing_images`: label, permanent R2 object key, MIME type, byte size, order,
  and whether the image is a defect close-up.

Originals use versioned private keys such as:

`listings/<listing-id>/originals/<upload-id>/required_front_straight.jpg`

Versioned keys prevent a replacement upload from overwriting the buyer-evidence
originals before PostgreSQL commits. After the database transaction succeeds,
the prior objects are removed. If the database transaction fails, the newly
uploaded objects are rolled back instead.

`POST /api/v1/listings/{listing_id}/images` exposes four named file controls in
Swagger for a manual storage test. `POST /api/v1/scan/upload` retains the Android
client's labelled multi-file contract, including up to five `defect_*`
close-ups. Responses contain private, one-hour signed URLs; R2 credentials never
go in the Android application.

Listing endpoints:

- `PUT /api/v1/listings/{listing_id}` creates or updates listing metadata.
- `GET /api/v1/listings/{listing_id}` retrieves one listing and fresh signed URLs.
- `GET /api/v1/listings` retrieves the most recently updated listings.
- Both image-upload endpoints create a seller-owned blank draft listing
  automatically when the listing ID does not yet exist.

This version is intentionally flat. Upload every extracted file directly into
your existing GitHub folder:

PokeMarket-backend-v0.1/

There is no app/ folder in this version.

Render Root Directory:
PokeMarket-backend-v0.1

Render Build Command:
pip install -r requirements.txt

Render Start Command:
uvicorn main:app --host 0.0.0.0 --port $PORT

Render Environment:
ENVIRONMENT=production
AI_PROVIDER=gemini
GEMINI_MODEL=gemini-3.8-flash
GEMINI_API_KEY=<your secret key>
DATABASE_URL=<your Neon pooled PostgreSQL connection string>
AUTH_SECRET=<at least 32 random characters>
LEGACY_CLAIM_CODE=<a different private random setup code>
R2_BUCKET_NAME=pokemart-images
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=<your Cloudflare access key ID>
R2_SECRET_ACCESS_KEY=<your Cloudflare secret access key>

Generate `AUTH_SECRET` and `LEGACY_CLAIM_CODE` separately on Windows with:

`py -c "import secrets; print(secrets.token_urlsafe(48))"`

Run it twice and paste each result directly into the matching Render environment
variable. Never put either value in GitHub or the Android project.

After deployment, `/api/v1/health` should include:

`"version":"2.5.0-user-accounts","database":"connected","auth":"configured"`

At startup, SQLAlchemy creates the users table and safely adds `seller_id` to the
existing listings table. Add Alembic migrations before later production schema
changes.

Do not upload .pyc files, __pycache__, download, or download (1).
