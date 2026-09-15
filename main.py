import asyncio
import gc
import logging
import traceback
from functools import lru_cache

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from ai import get_ai_provider, new_scan_id
from config import get_settings
from database import (
    Database,
    DatabaseConfigurationError,
    DatabaseOperationError,
)
from schemas import (
    CardSearchResponse,
    HealthResponse,
    ListingImageUploadResponse,
    ListingResponse,
    ListingUpsertRequest,
    ScanAnalysisResponse,
)
from storage import R2ConfigurationError, R2Storage, R2UploadError
from tcgdex import TCGdexClient


settings = get_settings()
tcgdex = TCGdexClient(settings.tcgdex_base_url)

logger = logging.getLogger("pokemarket")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name, version="2.4.0-postgres-listings")
scan_semaphore = asyncio.Semaphore(1)


async def acquire_scan_slot():
    """Keep the 512 MB Render instance from buffering multiple scans at once."""
    await scan_semaphore.acquire()
    try:
        yield
    finally:
        scan_semaphore.release()
        gc.collect()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health", response_model=HealthResponse)
async def health():
    if not settings.database_url:
        database_status = "not_configured"
    else:
        try:
            database = await asyncio.to_thread(get_database_client)
            await asyncio.to_thread(database.ping)
            database_status = "connected"
        except Exception:
            logger.exception("Database health check failed")
            database_status = "unavailable"
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.environment,
        version="2.4.0-postgres-listings",
        ai_provider=settings.ai_provider,
        database=database_status,
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


async def read_images(files, forced_labels=None):
    if not files:
        raise HTTPException(400, "At least one image is required.")

    if len(files) > settings.max_images:
        raise HTTPException(
            413,
            f"Maximum image count is {settings.max_images}.",
        )

    if forced_labels is not None and len(forced_labels) != len(files):
        raise HTTPException(400, "Every uploaded image must have exactly one label.")

    allowed = {"image/jpeg", "image/png", "image/webp"}
    result = []
    labels = []

    for index, item in enumerate(files):
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

        label = (
            forced_labels[index]
            if forced_labels is not None
            else (item.filename or "unlabelled_photo").rsplit(".", 1)[0]
        )
        labels.append(label)
        result.append((data, item.content_type, label))

    # The Android client sends one best image per required angle, never all
    # camera attempts. Enforce that contract server-side so duplicate frames
    # cannot silently consume memory or dilute the analysis.
    required = {
        "required_front_straight",
        "required_front_slight_left",
        "required_front_slight_right",
        "required_back",
    }
    supplied_required = [label for label in labels if label in required]
    if set(supplied_required) != required or len(supplied_required) != 4:
        raise HTTPException(
            400,
            "Exactly one best photo for each required angle is needed: "
            "straight, slight left, slight right, and back.",
        )
    defect_count = sum(label.startswith("defect_") for label in labels)
    if defect_count > 5 or len(labels) != 4 + defect_count:
        raise HTTPException(400, "Send only the four required views and up to five defect close-ups.")

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
    _scan_slot=Depends(acquire_scan_slot),
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
        raw_match, match_method = await tcgdex.resolve_candidate(
            identification.get("name"),
            identification.get("number"),
            identification.get("tcgdex_id"),
        )
        match = as_plain_dict(raw_match)
        if match:
            identification["verification_method"] = match_method

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


def get_r2_storage():
    try:
        return R2Storage(settings)
    except R2ConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc


@lru_cache
def get_database_client():
    database = Database(settings.database_url)
    database.initialize()
    return database


async def require_database():
    try:
        return await asyncio.to_thread(get_database_client)
    except DatabaseConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        logger.exception("Database initialization failed")
        raise HTTPException(503, "PostgreSQL is unavailable.") from exc


def add_image_urls(record):
    try:
        storage = get_r2_storage()
        for image in record["images"]:
            image["url"] = storage.presign_object(image["object_key"])
    except Exception:
        logger.exception("Could not generate signed listing image URLs")
        for image in record["images"]:
            image["url"] = None
    return record


async def persist_images(listing_id, files, forced_labels=None):
    try:
        listing_id = R2Storage.validate_listing_id(listing_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    database = await require_database()
    images = await read_images(files, forced_labels=forced_labels)
    storage = get_r2_storage()

    try:
        stored = await asyncio.to_thread(
            storage.upload_images,
            listing_id,
            images,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except R2UploadError as exc:
        logger.exception("R2 upload failed for listing %s", listing_id)
        raise HTTPException(502, str(exc)) from exc

    try:
        _, stale_keys = await asyncio.to_thread(
            database.replace_images,
            listing_id,
            stored,
        )
    except DatabaseOperationError as exc:
        logger.exception("Database image-record transaction failed for %s", listing_id)
        try:
            await asyncio.to_thread(
                storage.delete_objects,
                [image["object_key"] for image in stored],
            )
        except R2UploadError:
            logger.exception("Could not roll back new R2 objects for %s", listing_id)
        raise HTTPException(502, str(exc)) from exc

    if stale_keys:
        try:
            await asyncio.to_thread(storage.delete_objects, stale_keys)
        except R2UploadError:
            logger.exception("Database committed, but stale R2 cleanup failed for %s", listing_id)

    return ListingImageUploadResponse(
        status="stored",
        listing_id=listing_id,
        image_count=len(images),
        bytes=sum(len(image[0]) for image in images),
        persisted=True,
        images=stored,
    )


@app.put("/api/v1/listings/{listing_id}", response_model=ListingResponse)
async def save_listing(listing_id: str, payload: ListingUpsertRequest):
    try:
        listing_id = R2Storage.validate_listing_id(listing_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    database = await require_database()
    try:
        record = await asyncio.to_thread(
            database.upsert_listing,
            listing_id,
            payload.model_dump(exclude_unset=True),
        )
    except DatabaseOperationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return add_image_urls(record)


@app.get("/api/v1/listings/{listing_id}", response_model=ListingResponse)
async def read_listing(listing_id: str):
    try:
        listing_id = R2Storage.validate_listing_id(listing_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    database = await require_database()
    record = await asyncio.to_thread(database.get_listing, listing_id)
    if record is None:
        raise HTTPException(404, "Listing not found.")
    return add_image_urls(record)


@app.get("/api/v1/listings", response_model=list[ListingResponse])
async def read_listings(limit: int = 50, offset: int = 0):
    if not 1 <= limit <= 100:
        raise HTTPException(400, "limit must be between 1 and 100.")
    if offset < 0:
        raise HTTPException(400, "offset cannot be negative.")
    database = await require_database()
    records = await asyncio.to_thread(database.list_listings, limit, offset)
    return [add_image_urls(record) for record in records]


@app.post(
    "/api/v1/listings/{listing_id}/images",
    response_model=ListingImageUploadResponse,
)
async def upload_listing_images(
    listing_id: str,
    front_straight: UploadFile = File(...),
    front_slight_left: UploadFile = File(...),
    front_slight_right: UploadFile = File(...),
    back: UploadFile = File(...),
):
    files = [front_straight, front_slight_left, front_slight_right, back]
    labels = [
        "required_front_straight",
        "required_front_slight_left",
        "required_front_slight_right",
        "required_back",
    ]
    return await persist_images(listing_id, files, forced_labels=labels)


@app.post("/api/v1/scan/upload", response_model=ListingImageUploadResponse)
async def upload_only(
    files: list[UploadFile] = File(...),
    listing_id: str = Form(...),
):
    """Compatibility route for the Android scan client."""
    return await persist_images(listing_id, files)
