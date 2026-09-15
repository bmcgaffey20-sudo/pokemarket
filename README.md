# PokeMarket Backend 2.4.0 — PostgreSQL Listings + R2 Originals

Adds labelled defect close-ups, strict condition inspection, explicit major-defect
reporting, confidence normalization, and a server-side safeguard that prevents a
Near Mint or Lightly Played result when a bend, crease, or dent is detected.

Card photos are stored permanently in the private Cloudflare R2 bucket, while
PostgreSQL stores each listing record and its R2 object keys. The database never
stores the image binary or temporary signed URL.

## Database records

The service creates two tables on first connection:

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
- Both image-upload endpoints create a blank draft listing automatically when
  the listing ID does not yet exist.

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
DATABASE_URL=<your Render Postgres internal database URL>
R2_BUCKET_NAME=pokemart-images
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=<your Cloudflare access key ID>
R2_SECRET_ACCESS_KEY=<your Cloudflare secret access key>

Create a Render PostgreSQL database in the same region as the web service, then
copy its Internal Database URL into the web service's `DATABASE_URL` environment
variable. Do not put this URL in GitHub or the Android application.

After deployment, `/api/v1/health` should include:

`"version":"2.4.0-postgres-listings","database":"connected"`

For this first database-backed release, SQLAlchemy creates missing tables at
startup. Add Alembic migrations before making later production schema changes.

Do not upload .pyc files, __pycache__, download, or download (1).
