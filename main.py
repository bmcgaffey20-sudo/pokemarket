import asyncio
import gc
import hmac
import logging
import traceback
import uuid
from functools import lru_cache
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    import stripe
except ImportError:  # Tests can run without payment SDK installed.
    stripe = None

from ai import get_ai_provider, new_scan_id
from auth import (
    AuthConfigurationError,
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    normalize_email,
    verify_password,
)
from config import get_settings
from database import (
    Database,
    DatabaseConfigurationError,
    DatabaseOperationError,
    ListingOwnershipError,
    ListingValidationError,
    UserAlreadyExistsError,
)
from schemas import (
    AuthResponse,
    CardSearchResponse,
    HealthResponse,
    LoginRequest,
    ListingImageUploadResponse,
    ListingResponse,
    ListingUpsertRequest,
    RegisterRequest,
    ScanAnalysisResponse,
    UserResponse,
    CheckoutRequest,
    CheckoutResponse,
    OrderResponse,
    CheckoutProviderResponse,
    TrackingRequest,
)
from storage import R2ConfigurationError, R2Storage, R2UploadError
from tcgdex import TCGdexClient
from recovery import install_recovery, limit_auth, send_action_email


settings = get_settings()
tcgdex = TCGdexClient(settings.tcgdex_base_url)

logger = logging.getLogger("pokemarket")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name, version="2.8.0-checkout")
scan_semaphore = asyncio.Semaphore(1)
auth_scheme = HTTPBearer(auto_error=False)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, exc):
    # Never echo passwords, reset tokens or other submitted values in errors.
    errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.middleware("http")
async def private_account_responses(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/api/v1/auth/", "/account/")):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


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


async def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(auth_scheme),
    database=Depends(require_database),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Sign in is required.", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_access_token(credentials.credentials, settings.auth_secret)
    except AuthConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except InvalidTokenError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
    user = await asyncio.to_thread(database.get_user, payload["sub"])
    if user is None:
        raise HTTPException(401, "Account no longer exists.", headers={"WWW-Authenticate": "Bearer"})
    if payload.get("ver", 0) != user["session_version"]:
        raise HTTPException(401, "Your session was invalidated. Sign in again.", headers={"WWW-Authenticate": "Bearer"})
    return user


def auth_response(user, legacy_listings_claimed=0):
    try:
        token = create_access_token(
            user["id"],
            settings.auth_secret,
            settings.access_token_ttl_seconds,
            user.get("session_version", 0),
        )
    except AuthConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    return AuthResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_seconds,
        user=UserResponse(**user),
        legacy_listings_claimed=legacy_listings_claimed,
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
        version="2.8.0-checkout",
        ai_provider=settings.ai_provider,
        database=database_status,
        auth="configured" if settings.auth_configured else "not_configured",
        email="configured" if settings.email_configured else "not_configured",
        payments="configured" if settings.stripe_configured and stripe is not None else "not_configured",
    )


@app.post("/api/v1/auth/register", response_model=AuthResponse, status_code=201)
async def register(payload: RegisterRequest, request: Request, background: BackgroundTasks, database=Depends(require_database)):
    await limit_auth(database, request, "register", payload.email, 5)
    if not settings.auth_configured:
        raise HTTPException(503, "AUTH_SECRET must be configured before accounts can be created.")
    claim_legacy = bool(payload.legacy_claim_code)
    if claim_legacy and (
        not settings.legacy_claim_code
        or not hmac.compare_digest(
            payload.legacy_claim_code.encode("utf-8"),
            settings.legacy_claim_code.encode("utf-8"),
        )
    ):
        raise HTTPException(403, "The existing-listing setup code is incorrect.")
    password_salt, password_hash, iterations = await asyncio.to_thread(
        hash_password,
        payload.password,
        settings.password_hash_iterations,
    )
    try:
        user, claimed = await asyncio.to_thread(
            database.create_user,
            str(uuid.uuid4()),
            normalize_email(payload.email),
            payload.display_name,
            password_hash,
            password_salt,
            iterations,
            claim_legacy,
        )
    except UserAlreadyExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(502, str(exc)) from exc
    if settings.email_configured:
        background.add_task(send_action_email, settings, database, user["email"], "verify")
    return auth_response(user, legacy_listings_claimed=claimed)


@app.post("/api/v1/auth/login", response_model=AuthResponse)
async def login(payload: LoginRequest, request: Request, database=Depends(require_database)):
    await limit_auth(database, request, "login", normalize_email(payload.email), 10)
    if not settings.auth_configured:
        raise HTTPException(503, "AUTH_SECRET must be configured before sign-in is available.")
    user = await asyncio.to_thread(database.get_user_by_email, normalize_email(payload.email))
    valid = False
    if user is not None:
        valid = await asyncio.to_thread(
            verify_password,
            payload.password,
            user["password_salt"],
            user["password_hash"],
            user["password_iterations"],
        )
    else:
        await asyncio.to_thread(hash_password, payload.password, settings.password_hash_iterations)
    if not valid:
        raise HTTPException(401, "Email or password is incorrect.")
    await asyncio.to_thread(database.record_login, user["id"])
    public_user = {
        key: user[key]
        for key in (
            "id",
            "email",
            "email_verified",
            "session_version",
            "display_name",
            "created_at",
            "seller_tier",
            "completed_buys",
            "successful_sales",
            "successful_sales_over_100",
            "max_listing_cents",
        )
    }
    return auth_response(public_user)


@app.get("/api/v1/auth/me", response_model=UserResponse)
async def read_current_user(current_user=Depends(require_user)):
    return UserResponse(**current_user)


install_recovery(app, settings, require_database, require_user)


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
    _current_user=Depends(require_user),
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


async def persist_images(listing_id, files, seller_id, database, forced_labels=None):
    try:
        listing_id = R2Storage.validate_listing_id(listing_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    images = await read_images(files, forced_labels=forced_labels)
    try:
        await asyncio.to_thread(database.ensure_listing_owner, listing_id, seller_id, True)
    except ListingOwnershipError as exc:
        raise HTTPException(404, str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(502, str(exc)) from exc
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
            seller_id,
        )
    except ListingOwnershipError as exc:
        logger.warning("Image ownership check failed for listing %s", listing_id)
        try:
            await asyncio.to_thread(
                storage.delete_objects,
                [image["object_key"] for image in stored],
            )
        except R2UploadError:
            logger.exception("Could not roll back unauthorized R2 upload for %s", listing_id)
        raise HTTPException(404, str(exc)) from exc
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
async def save_listing(
    listing_id: str,
    payload: ListingUpsertRequest,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    try:
        listing_id = R2Storage.validate_listing_id(listing_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if (
        payload.price_cents is not None
        and current_user["max_listing_cents"] is not None
        and payload.price_cents > current_user["max_listing_cents"]
    ):
        limit = current_user["max_listing_cents"] / 100
        raise HTTPException(
            403,
            f"Seller Tier {current_user['seller_tier']} has a ${limit:.2f} listing limit.",
        )
    try:
        record = await asyncio.to_thread(
            database.upsert_listing,
            listing_id,
            payload.model_dump(exclude_unset=True),
            current_user["id"],
        )
    except ListingOwnershipError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ListingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return add_image_urls(record)


@app.post("/api/v1/listings/{listing_id}/publish", response_model=ListingResponse)
async def publish_listing(listing_id: str, current_user=Depends(require_user), database=Depends(require_database)):
    return await publication_action(database, listing_id, current_user["id"], True)


@app.post("/api/v1/listings/{listing_id}/unpublish", response_model=ListingResponse)
async def unpublish_listing(listing_id: str, current_user=Depends(require_user), database=Depends(require_database)):
    return await publication_action(database, listing_id, current_user["id"], False)


async def publication_action(database, listing_id, seller_id, publish):
    try:
        record = await asyncio.to_thread(database.set_publication, listing_id, seller_id, publish)
    except ListingOwnershipError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ListingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return add_image_urls(record)


def public_image_urls(record):
    add_image_urls(record)
    record["images"] = [{"label": image["label"], "url": image["url"]} for image in record["images"]]
    return record


@app.get("/api/v1/marketplace")
async def browse_marketplace(limit: int = 20, offset: int = 0, q: str = "", database=Depends(require_database)):
    if not 1 <= limit <= 50 or offset < 0 or len(q) > 200:
        raise HTTPException(400, "Invalid pagination or search query.")
    records = await asyncio.to_thread(database.marketplace, limit, offset, q)
    return [public_image_urls(record) for record in records]


@app.get("/api/v1/marketplace/{listing_id}")
async def marketplace_detail(listing_id: str, database=Depends(require_database)):
    records = await asyncio.to_thread(database.marketplace, listing_id=listing_id)
    if not records:
        raise HTTPException(404, "This listing is no longer available.")
    return public_image_urls(records[0])


@app.get("/api/v1/listings/{listing_id}", response_model=ListingResponse)
async def read_listing(
    listing_id: str,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    try:
        listing_id = R2Storage.validate_listing_id(listing_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    record = await asyncio.to_thread(database.get_listing, listing_id, current_user["id"])
    if record is None:
        raise HTTPException(404, "Listing not found.")
    return add_image_urls(record)


@app.get("/api/v1/listings", response_model=list[ListingResponse])
async def read_listings(
    limit: int = 50,
    offset: int = 0,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    if not 1 <= limit <= 100:
        raise HTTPException(400, "limit must be between 1 and 100.")
    if offset < 0:
        raise HTTPException(400, "offset cannot be negative.")
    records = await asyncio.to_thread(
        database.list_listings,
        current_user["id"],
        limit,
        offset,
    )
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
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    files = [front_straight, front_slight_left, front_slight_right, back]
    labels = [
        "required_front_straight",
        "required_front_slight_left",
        "required_front_slight_right",
        "required_back",
    ]
    return await persist_images(
        listing_id,
        files,
        current_user["id"],
        database,
        forced_labels=labels,
    )


@app.post("/api/v1/scan/upload", response_model=ListingImageUploadResponse)
async def upload_only(
    files: list[UploadFile] = File(...),
    listing_id: str = Form(...),
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    """Compatibility route for the Android scan client."""
    return await persist_images(listing_id, files, current_user["id"], database)


def require_stripe():
    if stripe is None or not settings.stripe_secret_key:
        raise HTTPException(503, "Stripe test payments are not configured on the backend.")
    stripe.api_key = settings.stripe_secret_key
    return stripe


def safe_checkout_url(value, default):
    candidate = (value or default).strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise HTTPException(422, "Checkout return URLs must be complete http(s) URLs.")
    return candidate


@app.post("/api/v1/payments/connect/onboard", response_model=CheckoutProviderResponse)
async def onboard_seller(current_user=Depends(require_user), database=Depends(require_database)):
    stripe_client = require_stripe()
    account_id = current_user.get("stripe_account_id")
    try:
        if not account_id:
            account = await asyncio.to_thread(
                stripe_client.Account.create,
                type="express",
                country="US",
                email=current_user["email"],
                capabilities={"card_payments": {"requested": True}, "transfers": {"requested": True}},
                business_profile={"name": "PokeMarket seller"},
            )
            account_id = account["id"]
            await asyncio.to_thread(database.set_stripe_account, current_user["id"], account_id)
        base = settings.public_base_url.rstrip("/")
        link = await asyncio.to_thread(
            stripe_client.AccountLink.create,
            account=account_id,
            refresh_url=f"{base}/api/v1/payments/connect/refresh",
            return_url=f"{base}/api/v1/payments/connect/return",
            type="account_onboarding",
        )
    except Exception as exc:
        logger.exception("Stripe seller onboarding failed")
        raise HTTPException(502, "Stripe could not start seller payout setup.") from exc
    return CheckoutProviderResponse(url=link["url"], stripe_account_id=account_id)


@app.post("/api/v1/payments/checkout", response_model=CheckoutResponse)
async def create_checkout_session(
    payload: CheckoutRequest,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    stripe_client = require_stripe()
    order_id = str(uuid.uuid4())
    try:
        order = await asyncio.to_thread(
            database.reserve_order,
            order_id,
            payload.listing_id,
            current_user["id"],
            settings.marketplace_commission_percent,
            payload.shipping_cents,
        )
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(409, str(exc)) from exc

    seller = await asyncio.to_thread(database.get_user, order["seller_id"])
    account_id = seller.get("stripe_account_id") if seller else None
    if not account_id:
        await asyncio.to_thread(database.cancel_order, order_id)
        raise HTTPException(409, "This seller has not completed Stripe payout setup yet.")
    try:
        account = await asyncio.to_thread(stripe_client.Account.retrieve, account_id)
        if not account.get("charges_enabled") or not account.get("payouts_enabled"):
            await asyncio.to_thread(database.cancel_order, order_id)
            raise HTTPException(409, "This seller's payout account is still being verified.")
        listings = await asyncio.to_thread(database.marketplace, listing_id=payload.listing_id)
        listing = listings[0] if listings else {}
        title = listing.get("title") or listing.get("card_name") or "PokeMarket card"
        base = settings.public_base_url.rstrip("/")
        success_url = safe_checkout_url(payload.success_url, f"{base}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}")
        cancel_url = safe_checkout_url(payload.cancel_url, f"{base}/checkout/cancelled")
        line_items = [{
            "price_data": {"currency": order["currency"].lower(), "product_data": {"name": title}, "unit_amount": order["item_cents"]},
            "quantity": 1,
        }]
        if order["shipping_cents"]:
            line_items.append({
                "price_data": {"currency": order["currency"].lower(), "product_data": {"name": "Shipping"}, "unit_amount": order["shipping_cents"]},
                "quantity": 1,
            })
        session = await asyncio.to_thread(
            stripe_client.checkout.Session.create,
            mode="payment",
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=current_user["email"],
            metadata={"order_id": order_id, "listing_id": payload.listing_id},
            payment_intent_data={"metadata": {"order_id": order_id}},
        )
        await asyncio.to_thread(database.attach_checkout_session, order_id, session["id"])
    except HTTPException:
        raise
    except Exception as exc:
        await asyncio.to_thread(database.cancel_order, order_id)
        logger.exception("Stripe checkout session creation failed")
        raise HTTPException(502, "Stripe could not create the checkout session.") from exc
    return CheckoutResponse(
        order_id=order_id,
        checkout_url=session["url"],
        stripe_session_id=session["id"],
        item_cents=order["item_cents"],
        shipping_cents=order["shipping_cents"],
        commission_cents=order["commission_cents"],
        seller_amount_cents=order["seller_amount_cents"],
        currency=order["currency"],
    )


@app.post("/api/v1/payments/webhook")
async def stripe_webhook(
    payload: bytes = Body(...),
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    database=Depends(require_database),
):
    stripe_client = require_stripe()
    if not settings.stripe_webhook_secret:
        raise HTTPException(503, "STRIPE_WEBHOOK_SECRET is not configured yet.")
    if not stripe_signature:
        raise HTTPException(400, "Stripe-Signature header is required.")
    try:
        event = stripe_client.Webhook.construct_event(payload, stripe_signature, settings.stripe_webhook_secret)
    except Exception as exc:
        raise HTTPException(400, "Invalid Stripe webhook signature.") from exc
    if event.get("type") in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        session = event["data"]["object"]
        await asyncio.to_thread(database.mark_order_paid, session.get("id"), session.get("payment_intent"))
    return {"received": True}


@app.get("/api/v1/orders", response_model=list[OrderResponse])
async def read_orders(
    limit: int = 50,
    offset: int = 0,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    if not 1 <= limit <= 100 or offset < 0:
        raise HTTPException(400, "Invalid pagination.")
    return await asyncio.to_thread(database.list_orders, current_user["id"], limit, offset)


@app.get("/api/v1/orders/{order_id}", response_model=OrderResponse)
async def read_order(order_id: str, current_user=Depends(require_user), database=Depends(require_database)):
    order = await asyncio.to_thread(database.get_order, order_id, current_user["id"])
    if order is None:
        raise HTTPException(404, "Order not found.")
    return order


@app.post("/api/v1/orders/{order_id}/tracking", response_model=OrderResponse)
async def add_order_tracking(order_id: str, payload: TrackingRequest, current_user=Depends(require_user), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(database.set_tracking, order_id, current_user["id"], payload.tracking_number.strip())
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    return order


@app.post("/api/v1/orders/{order_id}/confirm-delivery", response_model=OrderResponse)
async def confirm_order_delivery(order_id: str, current_user=Depends(require_user), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(database.confirm_delivery, order_id, current_user["id"], settings.seller_hold_days)
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    return order


@app.post("/api/v1/orders/{order_id}/complete", response_model=OrderResponse)
async def complete_order(order_id: str, current_user=Depends(require_user), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(database.complete_order, order_id, current_user["id"])
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    seller = await asyncio.to_thread(database.get_user, order["seller_id"])
    account_id = seller.get("stripe_account_id") if seller else None
    if account_id and order["seller_amount_cents"] > 0:
        try:
            stripe_client = require_stripe()
            transfer = await asyncio.to_thread(
                stripe_client.Transfer.create,
                amount=order["seller_amount_cents"],
                currency=order["currency"].lower(),
                destination=account_id,
                metadata={"order_id": order["id"], "listing_id": order["listing_id"]},
            )
            order = await asyncio.to_thread(database.mark_payout, order["id"], "paid", transfer["id"])
        except Exception:
            logger.exception("Seller payout failed for order %s", order_id)
            order = await asyncio.to_thread(database.mark_payout, order["id"], "failed")
    return order
