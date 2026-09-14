# PokeMarket Backend 2.2 — Rigorous Scan

Adds labelled defect close-ups, strict condition inspection, explicit major-defect
reporting, confidence normalization, and a server-side safeguard that prevents a
Near Mint or Lightly Played result when a bend, crease, or dent is detected.

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

After deployment, /api/v1/health should show:
"version":"2.2.0-rigorous-scan"

Do not upload .pyc files, __pycache__, download, or download (1).
