from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from ai import get_ai_provider, new_scan_id
from config import get_settings
from schemas import CardSearchResponse, HealthResponse, ScanAnalysisResponse
from tcgdex import TCGdexClient


settings = get_settings()
tcgdex = TCGdexClient(settings.tcgdex_base_url)

app = FastAPI(
    title=settings.app_name,
    version="2.1.0-flat",
    description="PokeMarket grounded two-pass AI card scanning backend",
)

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
        version="2.1.0-flat",
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


async def read_images(files: list[UploadFile]):
    if not files:
        raise HTTPException(400, "At least one image is required.")

    if len(files) > settings.max_images:
        raise HTTPException(
            413,
            f"Maximum image count is {settings.max_images}.",
        )

    allowed = {"image/jpeg", "image/png", "image/webp"}
    result = []

    for item in files:
        if item.content_type not in allowed:
            raise HTTPException(
                415,
                f"Unsupported image type: {item.content_type}",
            )

        data = await item.read()

        if not data:
            raise HTTPException(400, "Uploaded image is empty.")

        if len(data) > settings.max_image_bytes:
            raise HTTPException(413, "Uploaded image is too large.")

        result.append((data, item.content_type))

    return result


@app.post("/api/v1/scan/analyze", response_model=ScanAnalysisResponse)
async def analyze_scan(
    files: Annotated[
        list[UploadFile],
        File(description="Upload one or more Pokemon card photos"),
    ],
    include_condition: Annotated[bool, Form()] = True,
    include_authenticity: Annotated[bool, Form()] = True,
):
    images = await read_images(files)
    scan_id = new_scan_id()

    try:
        provider = get_ai_provider(settings)

        # PASS 1: identify only. No authenticity judgments yet.
        identification = await provider.identify(images)

    except ValueError as exc:
        raise HTTPException(500, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502,
            f"AI identification failed: {exc}",
        ) from exc

    tcgdex_match = None
    match_type = "no_match"

    try:
        tcgdex_match, match_type = await tcgdex.resolve_candidate(
            identification.get("name"),
            identification.get("number"),
            identification.get("tcgdex_id"),
        )
    except Exception:
        match_type = "lookup_failed"

    # More honest status names than the old "matched_tcgdex = verified".
    if match_type == "exact_id":
        identification["verification_status"] = "tcgdex_exact_id_match"
    elif match_type == "name_and_number":
        identification["verification_status"] = "tcgdex_name_number_match"
    elif match_type == "name_only_candidate":
        identification["verification_status"] = "tcgdex_name_only_candidate"
    elif match_type == "lookup_failed":
        identification["verification_status"] = "tcgdex_lookup_failed"
    else:
        identification["verification_status"] = "not_matched"

    if tcgdex_match:
        identification["tcgdex_verified_id"] = tcgdex_match.get("id")

    # PASS 2: condition + authenticity grounded on TCGdex reference.
    try:
        assessment = await provider.assess(
            images,
            identification,
            tcgdex_match,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"AI grounded assessment failed: {exc}",
        ) from exc

    condition = dict(assessment["condition"])
    authenticity = dict(assessment["authenticity"])
    warnings = list(assessment.get("warnings", []))

    if not include_condition:
        condition = {"status": "disabled"}

    if not include_authenticity:
        authenticity = {"status": "disabled"}

    if match_type == "name_only_candidate":
        warnings.append(
            "TCGdex matched the card name, but not an exact collector number."
        )

    if match_type in {"no_match", "lookup_failed"}:
        warnings.append(
            "No strong TCGdex reference match was available; authenticity confidence should remain conservative."
        )

    warnings.append(
        "A TCGdex match confirms that an official card record exists; it does not prove the photographed physical card is genuine."
    )
    warnings.append(
        "Authenticity is preliminary visual screening, not certification."
    )

    return ScanAnalysisResponse(
        scan_id=scan_id,
        status="complete",
        provider=provider.name,
        identification=identification,
        condition=condition,
        authenticity=authenticity,
        tcgdex=tcgdex_match,
        warnings=warnings,
    )


@app.post("/api/v1/scan/upload")
async def upload_only(
    files: Annotated[
        list[UploadFile],
        File(description="Upload one or more Pokemon card photos"),
    ],
):
    images = await read_images(files)

    return {
        "status": "received",
        "image_count": len(images),
        "bytes": sum(len(data) for data, _ in images),
        "persisted": False,
    }
