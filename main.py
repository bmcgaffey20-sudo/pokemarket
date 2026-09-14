import logging
import traceback

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from ai import get_ai_provider, new_scan_id
from config import get_settings
from schemas import CardSearchResponse, HealthResponse, ScanAnalysisResponse
from tcgdex import TCGdexClient


settings = get_settings()
tcgdex = TCGdexClient(settings.tcgdex_base_url)

logger = logging.getLogger("pokemarket")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name, version="2.2.0-rigorous-scan")

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
        version="2.2.0-rigorous-scan",
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

        label = (item.filename or "unlabelled_photo").rsplit(".", 1)[0]
        result.append((data, item.content_type, label))

    return result


def as_plain_dict(value):
    """
    Normalize either a regular dict or a Pydantic model into a plain dict.
    TCGdex helpers may return model objects, so don't assume .get() exists.
    """
    if value is None:
        return None

    if isinstance(value, dict):
        return value

    if hasattr(value, "model_dump"):
        return value.model_dump()

    if hasattr(value, "dict"):
        return value.dict()

    try:
        return dict(value)
    except Exception:
        return {
            key: getattr(value, key)
            for key in dir(value)
            if not key.startswith("_")
            and not callable(getattr(value, key, None))
        }


def normalize_confidence(value):
    """Keep model confidence consistently in the API's documented 0..1 range."""
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    if confidence > 1.0:
        confidence /= 100.0
    return max(0.0, min(1.0, confidence))


def apply_grading_safeguards(condition, images):
    """Enforce structural-damage caps even if the model returns an optimistic label."""
    condition = dict(condition or {})
    condition["confidence"] = normalize_confidence(condition.get("confidence"))

    defects = list(condition.get("major_defects") or [])
    structural_terms = ("bend", "crease", "dent", "indent", "fold", "ripple")
    structural = bool(condition.get("structural_damage_detected"))

    for defect in defects:
        description = " ".join(
            str(defect.get(key, ""))
            for key in ("type", "severity", "location", "evidence")
        ).lower()
        if any(term in description for term in structural_terms):
            structural = True
            break

    condition["structural_damage_detected"] = structural
    condition["major_defects"] = defects
    grade = str(condition.get("estimated_condition") or "").strip().lower()
    warnings = list(condition.get("warnings") or [])

    if structural and grade in {"near mint", "nm", "lightly played", "lp"}:
        condition["estimated_condition"] = "Moderately Played"
        warnings.append(
            "Structural damage safeguard applied: a bend, crease, or dent "
            "prevents a Near Mint or Lightly Played estimate."
        )

    has_defect_closeup = any(
        str(image[2]).lower().startswith("defect_")
        for image in images
        if len(image) > 2
    )
    if not has_defect_closeup:
        warnings.append(
            "No defect close-up was supplied; small creases, dents, and surface "
            "damage may remain hidden."
        )
        if grade in {"near mint", "nm"}:
            condition["confidence"] = min(condition["confidence"], 0.75)

    condition["warnings"] = list(dict.fromkeys(warnings))
    return condition


@app.post("/api/v1/scan/analyze", response_model=ScanAnalysisResponse)
async def analyze_scan(
    files: list[UploadFile] = File(...),
    include_condition: bool = Form(True),
    include_authenticity: bool = Form(True),
):
    images = await read_images(files)

    try:
        provider = get_ai_provider(settings)

        # Stage 1: identify the card from the supplied photographs.
        identification = await provider.identify(images)

    except ValueError as exc:
        logger.error(
            "AI configuration/value error during identification: %s: %s",
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
            "AI identification failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        raise HTTPException(
            502,
            f"AI identification failed: {type(exc).__name__}: {exc}",
        ) from exc

    identification = as_plain_dict(identification) or {}
    identification["confidence"] = normalize_confidence(
        identification.get("confidence")
    )
    warnings = []

    # Stage 2: resolve the AI proposal against TCGdex.
    match = None

    try:
        raw_match = await tcgdex.resolve_candidate(
            identification.get("name"),
            identification.get("number"),
            identification.get("tcgdex_id"),
        )
        match = as_plain_dict(raw_match)

    except Exception as exc:
        logger.exception(
            "TCGdex verification failed after AI identification: %s: %s",
            type(exc).__name__,
            exc,
        )
        warnings.append(
            f"TCGdex verification failed: {type(exc).__name__}: {exc}"
        )

    identification["verification_status"] = (
        "matched_tcgdex" if match else "not_verified"
    )

    if match:
        verified_id = match.get("id")
        if verified_id:
            identification["tcgdex_verified_id"] = verified_id
    else:
        warnings.append(
            "No sufficiently strong TCGdex match was established."
        )

    # Stage 3: condition/authenticity assessment grounded with TCGdex data.
    try:
        assessment = await provider.assess(
            images,
            identification,
            match,
        )

    except Exception as exc:
        logger.exception(
            "AI assessment failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        raise HTTPException(
            502,
            f"AI assessment failed: {type(exc).__name__}: {exc}",
        ) from exc

    assessment = as_plain_dict(assessment) or {}

    condition = as_plain_dict(assessment.get("condition")) or {}
    authenticity = as_plain_dict(assessment.get("authenticity")) or {}
    condition = apply_grading_safeguards(condition, images)
    authenticity["confidence"] = normalize_confidence(
        authenticity.get("confidence")
    )
    warnings.extend(list(assessment.get("warnings") or []))

    if not include_condition:
        condition = {"status": "disabled"}

    if not include_authenticity:
        authenticity = {"status": "disabled"}

    warnings.append(
        "Authenticity is preliminary visual screening, not certification."
    )

    return ScanAnalysisResponse(
        scan_id=new_scan_id(),
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
        "status": "received",
        "image_count": len(images),
        "bytes": sum(len(image[0]) for image in images),
        "persisted": False,
    }
