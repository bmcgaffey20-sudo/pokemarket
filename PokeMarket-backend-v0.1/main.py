import logging
import traceback

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from ai import get_ai_provider
from config import get_settings
from schemas import CardSearchResponse, HealthResponse, ScanAnalysisResponse
from tcgdex import TCGdexClient

settings = get_settings()
tcgdex = TCGdexClient(settings.tcgdex_base_url)

logger = logging.getLogger("pokemarket")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name, version="2.0.0-flat")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/v1/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
        version="2.0.0-flat",
        ai_provider=settings.ai_provider,
    )

@app.get("/api/v1/cards/search", response_model=CardSearchResponse)
async def search_cards(q: str):
    query = q.strip()
    if not query:
        raise HTTPException(400, "Search query cannot be empty.")
    try:
        cards = await tcgdex.search_cards(query)
    except Exception as exc:
        raise HTTPException(502, f"TCGdex request failed: {exc}") from exc
    return CardSearchResponse(query=query, cards=cards)

@app.get("/api/v1/cards/{card_id}")
async def get_card(card_id: str):
    try:
        return await tcgdex.get_card(card_id)
    except Exception as exc:
        raise HTTPException(502, f"TCGdex request failed: {exc}") from exc

async def read_images(files):
    if not files:
        raise HTTPException(400, "At least one image is required.")
    if len(files) > settings.max_images:
        raise HTTPException(413, f"Maximum image count is {settings.max_images}.")

    allowed = {"image/jpeg","image/png","image/webp"}
    result = []
    for item in files:
        if item.content_type not in allowed:
            raise HTTPException(415, f"Unsupported image type: {item.content_type}")
        data = await item.read()
        if not data:
            raise HTTPException(400, "Uploaded image is empty.")
        if len(data) > settings.max_image_bytes:
            raise HTTPException(413, "Uploaded image is too large.")
        result.append((data, item.content_type))
    return result

@app.post("/api/v1/scan/analyze", response_model=ScanAnalysisResponse)
async def analyze_scan(
    files: list[UploadFile] = File(...),
    include_condition: bool = Form(True),
    include_authenticity: bool = Form(True),
):
    images = await read_images(files)
    try:
        provider = get_ai_provider(settings)
        scan_id, ai_result = await provider.analyze(images)
    except ValueError as exc:
        logger.error(
            "AI configuration/value error during /api/v1/scan/analyze: %s: %s",
            type(exc).__name__,
            exc,
        )
        traceback.print_exc()
        raise HTTPException(
            500,
            f"AI configuration error: {type(exc).__name__}: {exc}",
        ) from exc
    except Exception as exc:
        logger.exception(
            "AI analysis failed during /api/v1/scan/analyze: %s: %s",
            type(exc).__name__,
            exc,
        )
        raise HTTPException(
            502,
            f"AI analysis failed: {type(exc).__name__}: {exc}",
        ) from exc

    identification = dict(ai_result.identification)
    condition = dict(ai_result.condition)
    authenticity = dict(ai_result.authenticity)
    warnings = list(ai_result.warnings)

    if not include_condition:
        condition = {"status":"disabled"}
    if not include_authenticity:
        authenticity = {"status":"disabled"}

    match = None
    try:
        match = await tcgdex.resolve_candidate(
            identification.get("name"),
            identification.get("number"),
            identification.get("tcgdex_id"),
        )
    except Exception as exc:
        logger.exception(
            "TCGdex verification failed after AI identification: %s: %s",
            type(exc).__name__,
            exc,
        )
        warnings.append(f"TCGdex verification failed: {type(exc).__name__}: {exc}")

    identification["verification_status"] = "matched_tcgdex" if match else "not_verified"
    if match:
        identification["tcgdex_verified_id"] = match.get("id")
    else:
        warnings.append("No sufficiently strong TCGdex match was established.")

    warnings.append("Authenticity is preliminary visual screening, not certification.")

    return ScanAnalysisResponse(
        scan_id=scan_id,
        status="complete",
        provider=provider.name,
        identification=identification,
        condition=condition,
        authenticity=authenticity,
        tcgdex=match,
        warnings=warnings,
    )

@app.post("/api/v1/scan/upload")
async def upload_only(files: list[UploadFile] = File(...)):
    images = await read_images(files)
    return {
        "status":"received",
        "image_count":len(images),
        "bytes":sum(len(data) for data, _ in images),
        "persisted":False,
    }
