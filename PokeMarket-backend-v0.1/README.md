# PokeMarket Backend 2.0
Real multimodal scan pipeline: Android images -> Gemini -> TCGdex verification -> structured condition/authenticity screening.

## Upgrade existing Render service
Replace the contents of your current `PokeMarket-backend-v0.1` folder with these files (or rename this folder to that existing Render Root Directory). Keep the same Render service.

Render environment variables:
- ENVIRONMENT=production
- AI_PROVIDER=gemini
- GEMINI_MODEL=gemini-3.8-flash
- GEMINI_API_KEY=<your secret key>

Never put GEMINI_API_KEY in GitHub.

Build: `pip install -r requirements.txt`
Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

After deploy, `/api/v1/health` reports version 2.0.0.

`POST /api/v1/scan/analyze` accepts repeated multipart `files` fields (JPEG/PNG/WebP), plus optional `include_condition` and `include_authenticity` booleans.

Condition is an estimate, not an official grade. Authenticity is preliminary visual screening, not certification.
