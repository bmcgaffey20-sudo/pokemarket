# PokeMarket Backend v0.1

FastAPI backend foundation for the PokeMarket Android app.

## Includes
- Health endpoint
- TCGdex card search and lookup
- Multipart card-image upload endpoint
- Pluggable AI provider
- Safe development stub provider
- Environment configuration
- Tests
- Render deployment config

## Architecture
Android CameraX -> HTTPS -> PokeMarket API -> AI + TCGdex -> database/storage later

TCGdex does not require an API key.

## Local setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

API docs: http://127.0.0.1:8000/docs

## GitHub

Create a repository such as `pokemarket-backend` and upload this folder.

Never commit `.env` or API keys.

## Render

Build command:
`pip install -r requirements.txt`

Start command:
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`

`render.yaml` is included.

## Endpoints

- `GET /api/v1/health`
- `GET /api/v1/cards/search?q=charizard`
- `GET /api/v1/cards/{card_id}`
- `POST /api/v1/scan/upload`
- `POST /api/v1/scan/analyze`

## Next phase

- Supabase/Postgres
- User accounts/authentication
- Seller Trust
- Persistent image storage
- Real multimodal card identification
- Condition analysis
- Authenticity screening
- Inventory and marketplace listings
- Orders/payments/shipping
