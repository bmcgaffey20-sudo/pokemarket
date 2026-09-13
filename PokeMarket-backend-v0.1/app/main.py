from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .ai import get_ai_provider
from .config import get_settings
from .schemas import (
    CardSearchResponse,
    HealthResponse,
    ScanAnalysisResponse,
    ScanAnalyzeRequest,
)
from .tcgdex import TCGdexClient

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Backend API for PokeMarket.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

tcgdex = TCGdexClient(settings.tcgdex_base_url)


@app.get("/api/v1/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
    )


@app.get("/api/v1/cards/search", response_model=CardSearchResponse)
async def search_cards(q: str) -> CardSearchResponse:
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")

    try:
        cards = await tcgdex.search_cards(query)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"TCGdex request failed: {exc}",
        ) from exc

    return CardSearchResponse(query=query, cards=cards)


@app.get("/api/v1/cards/{card_id}")
async def get_card(card_id: str):
    try:
        return await tcgdex.get_card(card_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"TCGdex request failed: {exc}",
        ) from exc


@app.post("/api/v1/scan/analyze", response_model=ScanAnalysisResponse)
async def analyze_scan(request: ScanAnalyzeRequest):
    try:
        provider = get_ai_provider(settings.ai_provider)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result = await provider.analyze(
        image_count=request.image_count,
        views=request.views,
        include_condition=request.include_condition,
        include_authenticity=request.include_authenticity,
    )

    return ScanAnalysisResponse(
        scan_id=result["scan_id"],
        status="complete",
        provider=provider.name,
        identification=result["identification"],
        condition=result["condition"],
        authenticity=result["authenticity"],
        tcgdex=None,
        warnings=result.get("warnings", []),
    )


@app.post("/api/v1/scan/upload")
async def upload_scan_images(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="At least one image is required.")

    allowed = {"image/jpeg", "image/png", "image/webp"}
    accepted = []

    for item in files:
        if item.content_type not in allowed:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported image type: {item.content_type}",
            )
        accepted.append(
            {
                "filename": item.filename,
                "content_type": item.content_type,
            }
        )

    return {
        "scan_id": str(uuid4()),
        "status": "received",
        "image_count": len(accepted),
        "files": accepted,
        "note": "Images are not persisted in v0.1.",
    }
