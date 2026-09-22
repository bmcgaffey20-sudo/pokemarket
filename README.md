# PokeMarket Backend 2.13.0 — beta control center

Read `UPDATE-2.13.0.md` for this release's deployment notes. The older update
files remain as a history of the account, publishing, cloud-photo, and deletion
changes that are already included here.

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

The service creates the application tables on first connection, including:

- `users`: email, display name, password hash parameters, seller tier, activity
  counters, and listing limit.

- `listings`: draft metadata, card identity, condition, price, status, AI result,
  and timestamps.
- `listing_images`: label, permanent R2 object key, MIME type, byte size, order,
  and whether the image is a defect close-up.
- `orders`: buyer, seller, listing, Stripe payment state, 4% marketplace
  commission, delivery-protection hold, and seller payout state.
- `scan_jobs`: durable queued/processing/completed AI scan state and results.
- `device_tokens`: account-owned Android Firebase notification tokens.

Originals use versioned private keys such as:

`listings/<listing-id>/originals/<upload-id>/required_front_straight.jpg`

Versioned keys prevent a replacement upload from overwriting the buyer-evidence
originals before PostgreSQL commits. After the database transaction succeeds,
the prior objects are removed. If the database transaction fails, the newly
uploaded objects are rolled back instead.

Android uses the direct-upload session and completion endpoints so full-quality
originals bypass Render. It then submits `/api/v1/scan/jobs`; a durable worker
reads those private originals from R2 and stores the completed result for polling.
R2 credentials never go in the Android application.

Listing endpoints:

- `PUT /api/v1/listings/{listing_id}` creates or updates listing metadata.
- `GET /api/v1/listings/{listing_id}` retrieves one listing and fresh signed URLs.
- `GET /api/v1/listings` retrieves the most recently updated listings.
- `DELETE /api/v1/listings/{listing_id}` permanently removes an owned card and
  its private R2 objects when it has no checkout or order history.
- Both image-upload endpoints create a seller-owned blank draft listing
  automatically when the listing ID does not yet exist.

Checkout and orders:

- `POST /api/v1/payments/connect/onboard` creates a Stripe Express seller
  onboarding link. A seller must finish this before buyers can pay.
- `POST /api/v1/payments/checkout` reserves a published card and creates a
  Stripe Checkout Session in test mode. The 4% PokeMarket commission is stored
  on the order; shipping is collected separately and is not counted as seller
  commission.
- `POST /api/v1/payments/webhook` confirms payment from Stripe's signed
  `checkout.session.completed` event. Configure `STRIPE_WEBHOOK_SECRET` from
  the Stripe Dashboard webhook endpoint.
- `GET /api/v1/orders` and `GET /api/v1/orders/{id}` show buyer/seller orders.
- Sellers can add tracking; buyers confirm delivery; completion is blocked
  until the 10-day protection hold ends. Only then is the seller transfer
  created and the seller's trust-tier counters updated.
- Buyers can request a return during the protection window. Approval, return
  tracking, receipt confirmation, Stripe refund, dispute escalation, and
  automatic restoration of the returned card to a private draft are included.
- Firebase notifications cover sales, shipping, delivery, returns, refunds,
  completed orders, and administrator seller-tier changes.
- Administrators can list users, manually set seller tiers, and resolve return
  disputes. Access is restricted to accounts listed in `ADMIN_EMAILS`.
- Gemini capacity errors are retried by the durable worker with exponential
  delays, while the original R2 photos remain safely stored.

This version is intentionally flat. Upload the extracted files directly to the
root of your existing backend GitHub repository. There is no `app/` folder in
the backend package.

Render Root Directory: leave blank when these files are at the repository root.
If your repository intentionally keeps the backend in a subfolder, enter that
subfolder name instead.

Render Build Command:
pip install -r requirements.txt

Render Start Command:
uvicorn main:app --host 0.0.0.0 --port $PORT

Render Environment:
ENVIRONMENT=production
AI_PROVIDER=gemini
GEMINI_MODEL=gemini-3.1-flash-lite
GEMINI_API_KEY=<your secret key>
DATABASE_URL=<your Neon pooled PostgreSQL connection string>
DATABASE_POOL_SIZE=3
DATABASE_MAX_OVERFLOW=2
DATABASE_POOL_TIMEOUT_SECONDS=15
AUTH_SECRET=<at least 32 random characters>
LEGACY_CLAIM_CODE=<a different private random setup code>
R2_BUCKET_NAME=pokemart-images
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=<your Cloudflare access key ID>
R2_SECRET_ACCESS_KEY=<your Cloudflare secret access key>
STRIPE_SECRET_KEY=sk_test_<your Stripe test secret>
STRIPE_PUBLISHABLE_KEY=pk_test_<your Stripe test publishable key>
STRIPE_WEBHOOK_SECRET=whsec_<your Stripe test webhook signing secret>
ADMIN_EMAILS=<your signed-in PokeMarket email>
FIREBASE_PROJECT_ID=<your Firebase project ID>
FIREBASE_SERVICE_ACCOUNT_JSON=<single-line Firebase service-account JSON>

Generate `AUTH_SECRET` and `LEGACY_CLAIM_CODE` separately on Windows with:

`py -c "import secrets; print(secrets.token_urlsafe(48))"`

Run it twice and paste each result directly into the matching Render environment
variable. Never put either value in GitHub or the Android project.

After deployment, `/api/v1/health` should include version
`2.13.0-beta-control`, `database:"connected"`, `auth:"configured"`,
`payments:"configured"`, and `push_notifications:"configured"` when Firebase
is configured.

At startup, SQLAlchemy safely adds the order shipping-address and payout fields
used by this release, the return fields, administrator flag, notification-token
table, and Gemini retry timestamp. Add Alembic migrations before later
production schema changes.

Do not upload .pyc files, __pycache__, download, or download (1).
