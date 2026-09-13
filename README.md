# PokeMarket Backend 2.0 FLAT

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
"version":"2.0.0-flat"

Do not upload .pyc files, __pycache__, download, or download (1).
