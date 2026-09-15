# PokeMarket Backend 2.3 — Cloudflare R2 Storage

Adds labelled defect close-ups, strict condition inspection, explicit major-defect
reporting, confidence normalization, and a server-side safeguard that prevents a
Near Mint or Lightly Played result when a bend, crease, or dent is detected.

Card photos can now be stored permanently in the private Cloudflare R2 bucket.
Both `POST /api/v1/scan/upload` and
`POST /api/v1/listings/{listing_id}/images` accept the four labelled required
photos plus up to five `defect_*` close-ups. The response contains private,
one-hour signed URLs; R2 credentials never go in the Android application.

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
R2_BUCKET_NAME=pokemarket-images
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=<your Cloudflare access key ID>
R2_SECRET_ACCESS_KEY=<your Cloudflare secret access key>

After deployment, /api/v1/health should show:
"version":"2.3.0-r2-storage"

Do not upload .pyc files, __pycache__, download, or download (1).
