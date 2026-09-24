import asyncio
import gc
import hashlib
import hmac
import logging
import traceback
import uuid
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    import stripe
except ImportError:  # Tests can run without payment SDK installed.
    stripe = None

from ai import GeminiUnavailableError, get_ai_provider, new_scan_id
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
    normalize_rarity,
)
from schemas import (
    AuthResponse,
    CardSearchResponse,
    HealthResponse,
    LoginRequest,
    ListingImageUploadResponse,
    ListingResponse,
    ListingDeleteResponse,
    ListingUpsertRequest,
    RegisterRequest,
    ScanAnalysisResponse,
    UserResponse,
    CheckoutRequest,
    CheckoutResponse,
    OrderResponse,
    CheckoutProviderResponse,
    TrackingRequest,
    DirectUploadSessionRequest,
    DirectUploadSessionResponse,
    DirectUploadCompleteRequest,
    ScanJobRequest,
    ScanJobAccepted,
    ScanJobStatus,
    ReturnRequest,
    ReturnReviewRequest,
    SellerTierUpdateRequest,
    DeviceTokenRequest,
    DeviceTokenResponse,
    SellerAddressRequest,
    SellerAddressResponse,
)
from notifications import send_push
from reporting import completed_week, send_weekly_sales_report
from storage import R2ConfigurationError, R2Storage, R2UploadError
from tcgdex import TCGdexClient
from recovery import install_recovery, limit_auth, send_action_email
from tracking import TrackingValidationError, classify_tracking_number
from community import install_community, cleanup_message_uploads
from alerts import install_alerts


settings = get_settings()
tcgdex = TCGdexClient(settings.tcgdex_base_url)

logger = logging.getLogger("pokemarket")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name, version="2.21.0-activity-badges")
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
    if request.url.path.startswith(("/api/v1/auth/", "/api/v1/admin/", "/api/v1/account/", "/api/v1/messages", "/api/v1/orders", "/api/v1/activity/", "/api/v1/notifications", "/account/")):
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
    database = Database(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
    )
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
    # ADMIN_EMAILS is the source of truth. Synchronizing both promotion and
    # removal prevents an account from retaining administrator access after it
    # is removed from the Render environment variable.
    should_be_admin = user["email"].lower() in settings.admin_email_list
    if bool(user.get("is_admin")) != should_be_admin:
        user = await asyncio.to_thread(database.set_admin, user["id"], should_be_admin)
    return user


async def require_admin(current_user=Depends(require_user)):
    if not current_user.get("is_admin"):
        raise HTTPException(403, "Administrator access is required.")
    return current_user


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


async def user_with_stripe_status(user):
    """Return public user data with live Stripe Connect readiness flags."""
    result = dict(user)
    result.update(
        stripe_connected=False,
        stripe_details_submitted=False,
        stripe_charges_enabled=False,
        stripe_payouts_enabled=False,
        stripe_requirements_due=[],
    )
    account_id = user.get("stripe_account_id")
    if not account_id or stripe is None or not settings.stripe_secret_key:
        return result
    try:
        stripe.api_key = settings.stripe_secret_key
        account = await asyncio.to_thread(stripe.Account.retrieve, account_id)
        requirements = account.get("requirements") or {}
        currently_due = list(requirements.get("currently_due") or [])
        past_due = list(requirements.get("past_due") or [])
        result.update(
            stripe_details_submitted=bool(account.get("details_submitted")),
            stripe_charges_enabled=bool(account.get("charges_enabled")),
            stripe_payouts_enabled=bool(account.get("payouts_enabled")),
            stripe_requirements_due=sorted(set(currently_due + past_due)),
        )
        result["stripe_connected"] = (
            result["stripe_details_submitted"]
            and result["stripe_charges_enabled"]
            and result["stripe_payouts_enabled"]
            and not result["stripe_requirements_due"]
        )
    except Exception:
        logger.exception("Could not refresh Stripe Connect status for user %s", user.get("id"))
    return result


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
        version="2.21.0-activity-badges",
        ai_provider=settings.ai_provider,
        database=database_status,
        auth="configured" if settings.auth_configured else "not_configured",
        email="configured" if settings.email_configured else "not_configured",
        push_notifications="configured" if settings.firebase_configured else "not_configured",
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
    should_be_admin = user["email"].lower() in settings.admin_email_list
    if bool(user.get("is_admin")) != should_be_admin:
        user = await asyncio.to_thread(database.set_admin, user["id"], should_be_admin)
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
            "is_admin",
        )
    }
    return auth_response(public_user)


@app.get("/api/v1/auth/me", response_model=UserResponse)
async def read_current_user(current_user=Depends(require_user)):
    return UserResponse(**(await user_with_stripe_status(current_user)))


@app.post("/api/v1/notifications/devices", response_model=DeviceTokenResponse)
async def register_notification_device(
    payload: DeviceTokenRequest,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    return await asyncio.to_thread(
        database.register_device_token,
        current_user["id"],
        payload.token,
        payload.platform,
    )


@app.get("/api/v1/account/seller-address", response_model=SellerAddressResponse)
async def read_seller_address(current_user=Depends(require_user), database=Depends(require_database)):
    address = await asyncio.to_thread(database.get_seller_address, current_user["id"])
    if address is None:
        raise HTTPException(404, "User not found.")
    return address


@app.put("/api/v1/account/seller-address", response_model=SellerAddressResponse)
async def save_seller_address(payload: SellerAddressRequest, current_user=Depends(require_user), database=Depends(require_database)):
    address = await asyncio.to_thread(database.update_seller_address, current_user["id"], payload.model_dump())
    if address is None:
        raise HTTPException(404, "User not found.")
    return address


@app.get("/api/v1/admin/users", response_model=list[UserResponse])
async def admin_users(
    limit: int = 100,
    offset: int = 0,
    q: str = "",
    _admin=Depends(require_admin),
    database=Depends(require_database),
):
    if not 1 <= limit <= 200 or offset < 0:
        raise HTTPException(400, "Invalid pagination.")
    return await asyncio.to_thread(database.list_users, limit, offset, q[:200])


@app.patch("/api/v1/admin/users/{user_id}/seller-tier", response_model=UserResponse)
async def admin_set_seller_tier(
    user_id: str,
    payload: SellerTierUpdateRequest,
    background: BackgroundTasks,
    _admin=Depends(require_admin),
    database=Depends(require_database),
):
    user = await asyncio.to_thread(database.set_seller_tier, user_id, payload.seller_tier)
    if user is None:
        raise HTTPException(404, "User not found.")
    background.add_task(
        send_push,
        settings,
        database,
        [user_id],
        "Seller level updated",
        f"Your PokeMarket seller level is now Tier {payload.seller_tier}.",
        {"page": "account"},
    )
    return user


@app.get("/api/v1/admin/returns", response_model=list[OrderResponse])
async def admin_return_disputes(_admin=Depends(require_admin), database=Depends(require_database)):
    return await asyncio.to_thread(database.list_return_disputes)


@app.get("/api/v1/admin/sales")
async def admin_sales(limit: int = 100, offset: int = 0, q: str = "", _admin=Depends(require_admin), database=Depends(require_database)):
    if not 1 <= limit <= 200 or offset < 0:
        raise HTTPException(400, "Invalid pagination.")
    return await asyncio.to_thread(database.list_admin_sales, limit, offset, q[:200])


@app.get("/api/v1/admin/sales/{order_id}")
async def admin_sale_detail(order_id: str, _admin=Depends(require_admin), database=Depends(require_database)):
    sale = await asyncio.to_thread(database.get_admin_sale, order_id)
    if sale is None:
        raise HTTPException(404, "Sale not found.")
    return sale


@app.get("/api/v1/admin/diagnostics")
async def admin_diagnostics(_admin=Depends(require_admin), database=Depends(require_database)):
    result = await asyncio.to_thread(database.admin_diagnostics)
    result.update({
        "service_version": "2.21.0-activity-badges",
        "email_configured": settings.email_configured,
        "push_configured": settings.firebase_configured,
        "payments_configured": settings.stripe_configured,
        "weekly_report_configured": bool(settings.email_configured and settings.admin_report_recipient),
        "report_recipient": settings.admin_report_recipient or None,
        "sale_detail_retention_days": settings.sale_detail_retention_days,
        "confirmation_timeout_days": settings.confirmation_timeout_days,
    })
    return result


install_recovery(app, settings, require_database, require_user)
install_community(app, settings, require_database, require_user, lambda: get_r2_storage())
install_alerts(app, require_database, require_user)


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

    validate_image_labels(labels)
    return result


def validate_image_labels(labels):
    """Apply the same evidence contract to multipart and direct uploads."""
    required = {"required_front_straight", "required_front_slight_left", "required_front_slight_right", "required_back"}
    if len(labels) != len(set(labels)):
        raise HTTPException(400, "Each photo label must be unique.")
    if not required.issubset(labels):
        raise HTTPException(400, "Four selected views are required: top center, slight left, slight right, and back.")
    defect_count = sum(label.startswith("defect_") for label in labels)
    if defect_count > 5 or any(
        label not in required and not label.startswith("defect_")
        for label in labels
    ):
        raise HTTPException(400, "Send four selected views and up to five defect close-ups.")


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


async def perform_scan_analysis(
    images,
    include_condition=True,
    include_authenticity=True,
    scan_id=None,
    market="pokemon",
    grading_status="ungraded",
):
    try:
        provider = get_ai_provider(settings)
        # Identification, condition and authenticity share one image-bearing
        # request. This preserves the complete evidence set while avoiding a
        # second Base64 copy of every photograph leaving Render.
        combined = await provider.analyze(images, market=market, grading_status=grading_status)

    except GeminiUnavailableError:
        # The durable worker handles capacity retry scheduling. Keep this typed
        # exception intact instead of flattening it into a generic HTTP error.
        raise

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

    combined = as_plain_dict(combined) or {}
    identification = as_plain_dict(combined.get("identification")) or {}
    identification["confidence"] = normalize_confidence(
        identification.get("confidence")
    )
    condition = as_plain_dict(combined.get("condition")) or {}
    authenticity = as_plain_dict(combined.get("authenticity")) or {}
    warnings = list(combined.get("warnings") or [])

    # Stage 2: resolve the AI proposal against TCGdex.
    match = None

    try:
        if market == "pokemon":
            raw_match, match_method = await tcgdex.resolve_candidate(
                identification.get("name"), identification.get("number"), identification.get("tcgdex_id"),
            )
        else:
            raw_match, match_method = None, "not_configured_for_market"
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
        "matched_tcgdex" if match else "not_verified" if market == "pokemon" else "visual_identification_only"
    )
    if market != "pokemon":
        identification["tcgdex_id"] = None
        identification.pop("tcgdex_verified_id", None)

    if match:
        verified_id = match.get("id")
        if verified_id:
            identification["tcgdex_verified_id"] = verified_id
        types = match.get("types")
        if isinstance(types, list) and types and isinstance(types[0], str):
            identification["card_type"] = types[0]
    else:
        warnings.append("No sufficiently strong TCGdex match was established." if market == "pokemon" else
                        "Visual identification only: no external catalog or certification verification is configured for this market.")

    if market == "pokemon":
        identification["rarity"] = normalize_rarity(
            (match or {}).get("rarity") or identification.get("rarity") or identification.get("variant")
        )

    if grading_status == "graded":
        label = " ".join(str(identification.get(key) or "").strip() for key in ("grading_company", "grade")).strip()
        condition["estimated_condition"] = f"Graded — {label}" if label else "Graded — review label"
        warnings.append("Grading label is seller/AI-read, not certificate-verified. Inspect slab and certification independently.")
        authenticity["manual_review_recommended"] = True
    else:
        condition = apply_grading_safeguards(condition, images)
        for field in ("grading_company", "grade", "certification_number"):
            identification[field] = None
    authenticity["confidence"] = normalize_confidence(
        authenticity.get("confidence")
    )
    if match:
        warnings.append("Identification was verified against TCGdex after visual analysis.")

    if not include_condition:
        condition = {"status": "disabled"}

    if not include_authenticity:
        authenticity = {"status": "disabled"}

    warnings.append(
        "Authenticity is preliminary visual screening, not certification."
    )

    return ScanAnalysisResponse(
        market=market,
        grading_status=grading_status,
        scan_id=scan_id or new_scan_id(),
        status="complete",
        provider=provider.name,
        identification=identification,
        condition=condition,
        authenticity=authenticity,
        tcgdex=match,
        warnings=warnings,
    )


@app.post("/api/v1/scan/analyze", response_model=ScanAnalysisResponse)
async def analyze_scan(
    files: list[UploadFile] = File(...),
    include_condition: bool = Form(True),
    include_authenticity: bool = Form(True),
    market: Literal["pokemon", "magic", "sports"] = Form("pokemon"),
    grading_status: Literal["graded", "ungraded"] = Form("ungraded"),
    _scan_slot=Depends(acquire_scan_slot),
    _current_user=Depends(require_user),
):
    """Compatibility endpoint for older Android builds."""
    images = await read_images(files)
    try:
        return await perform_scan_analysis(
            images,
            include_condition=include_condition,
            include_authenticity=include_authenticity,
            market=market,
            grading_status=grading_status,
        )
    except GeminiUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc


def get_r2_storage():
    try:
        return R2Storage(settings)
    except R2ConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc


scan_worker_task = None
settlement_worker_task = None


async def settle_seller_payout(order, database):
    """Issue or retry a seller transfer with a stable Stripe idempotency key."""
    if not order or order.get("payout_status") == "paid":
        return order
    seller = await asyncio.to_thread(database.get_user, order["seller_id"])
    account_id = seller.get("stripe_account_id") if seller else None
    if not account_id or order["seller_amount_cents"] <= 0:
        logger.warning("Automatic payout is waiting for seller account on order %s", order["id"])
        return order
    try:
        stripe_client = require_stripe()
        transfer = await asyncio.to_thread(
            stripe_client.Transfer.create,
            amount=order["seller_amount_cents"],
            currency=order["currency"].lower(),
            destination=account_id,
            metadata={"order_id": order["id"], "listing_id": order["listing_id"]},
            idempotency_key=f"pokemarket-order-payout-{order['id']}",
        )
        return await asyncio.to_thread(database.mark_payout, order["id"], "paid", transfer["id"])
    except Exception:
        logger.exception("Automatic seller payout failed for order %s", order["id"])
        return await asyncio.to_thread(database.mark_payout, order["id"], "failed")


async def settle_buyer_refund(order, database):
    """Issue or retry a return refund with a stable Stripe idempotency key."""
    if not order or order.get("status") == "refunded":
        return order
    if not order.get("stripe_payment_intent_id"):
        logger.error("Automatic return refund has no payment intent for order %s", order["id"])
        return order
    try:
        stripe_client = require_stripe()
        refund = await asyncio.to_thread(
            stripe_client.Refund.create,
            payment_intent=order["stripe_payment_intent_id"],
            metadata={"order_id": order["id"], "reason": "return_confirmation_timeout"},
            idempotency_key=f"pokemarket-return-{order['id']}",
        )
        return await asyncio.to_thread(database.mark_order_refunded, order["id"], refund["id"])
    except Exception:
        logger.exception("Automatic buyer refund failed for order %s", order["id"])
        return order


async def process_due_settlements(database):
    """Finalize expired confirmations and retry incomplete Stripe settlements."""
    purchases = await asyncio.to_thread(database.list_due_purchase_settlements)
    for candidate in purchases:
        transitioned = candidate["status"] != "completed"
        order = await asyncio.to_thread(database.complete_due_order, candidate["id"])
        if order is None:
            continue
        order = await settle_seller_payout(order, database)
        if transitioned:
            body = (
                "The 10-day confirmation deadline ended and the seller payout was released."
                if order.get("payout_status") == "paid"
                else "The 10-day confirmation deadline ended. The order completed and the seller payout will retry automatically."
            )
            await send_push(
                settings,
                database,
                [order["buyer_id"], order["seller_id"]],
                "Order automatically completed",
                body,
                {"page": "orders", "order_id": order["id"]},
            )

    returns = await asyncio.to_thread(database.list_due_return_settlements)
    for candidate in returns:
        transitioned = candidate["status"] == "return_shipped"
        order = await asyncio.to_thread(database.begin_due_return_refund, candidate["id"])
        if order is None:
            continue
        order = await settle_buyer_refund(order, database)
        if transitioned:
            body = (
                "The 10-day return deadline ended and the buyer refund was issued."
                if order.get("status") == "refunded"
                else "The 10-day return deadline ended. Return receipt was confirmed and the buyer refund will retry automatically."
            )
            await send_push(settings, database, [order["buyer_id"], order["seller_id"]], "Return automatically confirmed", body, {"page": "orders", "order_id": order["id"]})


async def process_operational_safeguards(database):
    reminders = await asyncio.to_thread(database.due_deadline_reminders)
    labels = {
        "delivery": ("Delivery confirmation deadline", "Confirm delivery or report a problem. The order will automatically complete when the deadline expires."),
        "hold": ("Seller payout hold ending", "Buyer protection is ending. The seller payout will automatically release when the deadline expires unless a return is open."),
        "return": ("Return confirmation deadline", "Confirm the returned card was received. The buyer will automatically be refunded when the deadline expires."),
    }
    for reminder in reminders:
        title, body = labels[reminder["kind"]]
        marked = await asyncio.to_thread(database.mark_deadline_reminder, reminder["order_id"], reminder["kind"], reminder["days"])
        if marked:
            await send_push(settings, database, [reminder["user_id"]], title, f"{reminder['days']} day reminder: {body}", {"page": "orders", "order_id": reminder["order_id"]})

    if settings.email_configured and settings.admin_report_recipient:
        period_start, period_end = completed_week()
        sent = await asyncio.to_thread(database.report_already_sent, period_start, period_end)
        if not sent:
            rows = await asyncio.to_thread(database.list_admin_sales, 10_000, 0, "", period_start, period_end)
            subject = await send_weekly_sales_report(settings, rows, period_start, period_end)
            await asyncio.to_thread(database.record_report_sent, period_start, period_end, settings.admin_report_recipient, subject)

    await asyncio.to_thread(database.redact_expired_sale_addresses, settings.sale_detail_retention_days)
    await asyncio.to_thread(database.prune_listing_view_deduplication, 90)
    await asyncio.to_thread(cleanup_message_uploads, database, settings, get_r2_storage)

async def settlement_worker_loop():
    deadlines_ready = False
    while True:
        try:
            database = await asyncio.to_thread(get_database_client)
            if not deadlines_ready:
                await asyncio.to_thread(
                    database.backfill_confirmation_deadlines,
                    settings.confirmation_timeout_days,
                )
                deadlines_ready = True
            await process_due_settlements(database)
            await process_operational_safeguards(database)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic settlement worker failed")
        await asyncio.sleep(max(5, settings.settlement_worker_poll_seconds))


async def scan_worker_loop():
    """Durable single-consumer queue for memory-heavy Gemini scans."""
    while True:
        job = None
        try:
            database = await asyncio.to_thread(get_database_client)
            job = await asyncio.to_thread(database.claim_next_scan_job)
            if job is None:
                await asyncio.sleep(settings.scan_worker_poll_seconds)
                continue

            listing = await asyncio.to_thread(
                database.get_listing,
                job["listing_id"],
                job["user_id"],
            )
            if listing is None or not listing.get("photos_persisted"):
                raise RuntimeError("The queued listing photos are no longer available.")

            storage = get_r2_storage()
            images = await asyncio.to_thread(
                storage.download_images,
                listing["images"],
                settings.max_image_bytes,
            )
            async with scan_semaphore:
                result = await perform_scan_analysis(
                    images,
                    include_condition=job["include_condition"],
                    include_authenticity=job["include_authenticity"],
                    scan_id=job["listing_id"],
                    market=job["market"],
                    grading_status=job["grading_status"],
                )
            await asyncio.to_thread(
                database.finish_scan_job,
                job["job_id"],
                result.model_dump(mode="json"),
                None,
            )
        except asyncio.CancelledError:
            raise
        except GeminiUnavailableError as exc:
            logger.warning("Gemini capacity delay for scan job %s: %s", job and job.get("job_id"), exc.details)
            if job and job["attempts"] < settings.gemini_job_max_attempts:
                delay = settings.gemini_retry_base_seconds * (2 ** (job["attempts"] - 1))
                await asyncio.to_thread(
                    database.requeue_scan_job,
                    job["job_id"],
                    delay,
                    str(exc),
                )
            elif job:
                await asyncio.to_thread(
                    database.finish_scan_job,
                    job["job_id"],
                    None,
                    "Gemini remained busy after automatic retries. Your photos are saved; start the analysis again shortly.",
                )
            await asyncio.sleep(settings.scan_worker_poll_seconds)
        except Exception as exc:
            logger.exception("Background scan job failed")
            if "job" in locals() and job:
                try:
                    await asyncio.to_thread(
                        database.finish_scan_job,
                        job["job_id"],
                        None,
                        f"{type(exc).__name__}: {exc}"[:2000],
                    )
                except Exception:
                    logger.exception("Could not record scan-job failure")
            await asyncio.sleep(settings.scan_worker_poll_seconds)
        finally:
            gc.collect()


@app.on_event("startup")
async def start_scan_worker():
    global scan_worker_task, settlement_worker_task
    try:
        database = await asyncio.to_thread(get_database_client)
        await asyncio.to_thread(database.requeue_interrupted_scan_jobs)
        await asyncio.to_thread(
            database.backfill_confirmation_deadlines,
            settings.confirmation_timeout_days,
        )
    except Exception:
        logger.exception("Background-worker initialization will retry")
    scan_worker_task = asyncio.create_task(scan_worker_loop())
    settlement_worker_task = asyncio.create_task(settlement_worker_loop())


@app.on_event("shutdown")
async def stop_scan_worker():
    global scan_worker_task, settlement_worker_task
    for task in (scan_worker_task, settlement_worker_task):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    scan_worker_task = None
    settlement_worker_task = None


@app.post("/api/v1/scan/jobs", response_model=ScanJobAccepted, status_code=202)
async def queue_scan_job(
    payload: ScanJobRequest,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    try:
        listing_id = R2Storage.validate_listing_id(payload.listing_id)
        job = await asyncio.to_thread(
            database.create_scan_job,
            str(uuid.uuid4()),
            listing_id,
            current_user["id"],
            payload.include_condition,
            payload.include_authenticity,
            payload.market,
            payload.grading_status,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ListingValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ListingOwnershipError as exc:
        raise HTTPException(404, str(exc)) from exc
    except DatabaseOperationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return ScanJobAccepted(**job)


@app.get("/api/v1/scan/jobs/{job_id}", response_model=ScanJobStatus)
async def read_scan_job(
    job_id: str,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    try:
        uuid.UUID(job_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid scan job ID.") from exc
    job = await asyncio.to_thread(database.get_scan_job, job_id, current_user["id"])
    if job is None:
        raise HTTPException(404, "Scan job not found.")
    return ScanJobStatus(**job)


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


@app.get("/api/v1/marketplace/{listing_id}/thumbnail")
async def listing_thumbnail(listing_id: str, label: str | None = None, original: bool = False, database=Depends(require_database)):
    records = await asyncio.to_thread(database.marketplace, 1, 0, "", listing_id)
    if not records:
        raise HTTPException(404, "Published listing not found.")
    images = records[0].get("images", [])
    photo = next((image for image in images if image.get("label") == (label or "required_front_straight")), None)
    if label is None:
        photo = photo or next(iter(images), None)
    if not photo:
        raise HTTPException(404, "Listing photo is unavailable.")
    try:
        storage = get_r2_storage()
        url = (await asyncio.to_thread(storage.presign_object, photo["object_key"]) if original else
               await asyncio.to_thread(storage.listing_thumbnail, photo["object_key"], settings.max_image_bytes))
    except Exception:
        logger.exception("Listing preview generation failed for %s", listing_id)
        raise HTTPException(503, "Photo preview could not load. Please retry.")
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


@app.get("/api/v1/marketplace")
async def browse_marketplace(limit: int = 20, offset: int = 0, q: str = "", market: Literal["pokemon", "magic", "sports"] | None = None, grading_status: Literal["graded", "ungraded"] | None = None, database=Depends(require_database)):
    if not 1 <= limit <= 50 or offset < 0 or len(q) > 200:
        raise HTTPException(400, "Invalid pagination or search query.")
    records = await asyncio.to_thread(database.marketplace, limit, offset, q, market=market, grading_status=grading_status)
    return [public_image_urls(record) for record in records]


@app.get("/api/v1/marketplace/top")
async def top_viewed_marketplace(limit: int = 20, market: Literal["pokemon", "magic", "sports"] | None = None, grading_status: Literal["graded", "ungraded"] | None = None, database=Depends(require_database)):
    if not 1 <= limit <= 25:
        raise HTTPException(400, "limit must be between 1 and 25.")
    records = await asyncio.to_thread(database.marketplace, limit, 0, "", None, True, market, grading_status)
    return [public_image_urls(record) for record in records]


@app.get("/api/v1/marketplace/{listing_id}")
async def marketplace_detail(listing_id: str, request: Request, database=Depends(require_database)):
    forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    remote = forwarded or (request.client.host if request.client else "unknown")
    user_agent = request.headers.get("user-agent", "unknown")[:300]
    install_key = request.headers.get("x-pokemarket-viewer", "").strip()[:128]
    daily_source = f"app:{install_key}" if install_key else f"web:{remote}|{user_agent}"
    viewer_hash = hashlib.sha256(daily_source.encode("utf-8")).hexdigest()
    count = await asyncio.to_thread(database.increment_listing_view, listing_id, viewer_hash)
    if count is None:
        raise HTTPException(404, "This listing is no longer available.")
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


@app.delete("/api/v1/listings/{listing_id}", response_model=ListingDeleteResponse)
async def delete_listing(
    listing_id: str,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    try:
        listing_id = R2Storage.validate_listing_id(listing_id)
        deletion = await asyncio.to_thread(
            database.prepare_listing_deletion,
            listing_id,
            current_user["id"],
        )
        object_keys = deletion["object_keys"]
        if object_keys:
            await asyncio.to_thread(get_r2_storage().delete_objects, object_keys)
        deleted_count = await asyncio.to_thread(
            database.delete_owned_listings,
            deletion["listing_ids"],
            current_user["id"],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ListingOwnershipError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (DatabaseOperationError, R2UploadError) as exc:
        logger.exception("Listing deletion failed for %s", listing_id)
        raise HTTPException(502, str(exc)) from exc
    return ListingDeleteResponse(
        status="deleted",
        listing_id=listing_id,
        deleted_listings=deleted_count,
        deleted_objects=len(object_keys),
    )


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
    await asyncio.to_thread(database.repair_split_listings, current_user["id"])
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
    present = [(file, label) for file, label in zip(files, labels) if file is not None]
    files = [file for file, _ in present]
    labels = [label for _, label in present]
    return await persist_images(
        listing_id,
        files,
        current_user["id"],
        database,
        forced_labels=labels,
    )


@app.post(
    "/api/v1/listings/{listing_id}/direct-upload-session",
    response_model=DirectUploadSessionResponse,
)
async def create_direct_upload_session(
    listing_id: str,
    payload: DirectUploadSessionRequest,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    """Issue short-lived R2 PUT URLs; original image bytes bypass Render."""
    labels = [image.label for image in payload.images]
    validate_image_labels(labels)
    if len(set(labels)) != len(labels):
        raise HTTPException(400, "Every image label must be unique.")
    try:
        await asyncio.to_thread(database.ensure_listing_owner, listing_id, current_user["id"], True)
        storage = get_r2_storage()
        upload_id, uploads = await asyncio.to_thread(
            storage.create_direct_upload_session,
            listing_id,
            [image.model_dump() for image in payload.images],
        )
    except (ValueError, ListingValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except ListingOwnershipError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (DatabaseOperationError, R2UploadError) as exc:
        raise HTTPException(502, str(exc)) from exc
    return DirectUploadSessionResponse(
        listing_id=listing_id,
        upload_id=upload_id,
        expires_in=settings.r2_presigned_url_expiry_seconds,
        uploads=uploads,
    )


@app.post(
    "/api/v1/listings/{listing_id}/direct-upload-complete",
    response_model=ListingImageUploadResponse,
)
async def complete_direct_upload(
    listing_id: str,
    payload: DirectUploadCompleteRequest,
    current_user=Depends(require_user),
    database=Depends(require_database),
):
    """Verify directly uploaded originals and atomically attach them to a listing."""
    labels = [image.label for image in payload.images]
    validate_image_labels(labels)
    if len(set(labels)) != len(labels):
        raise HTTPException(400, "Every image label must be unique.")
    storage = get_r2_storage()
    image_dicts = [image.model_dump() for image in payload.images]
    try:
        await asyncio.to_thread(database.ensure_listing_owner, listing_id, current_user["id"], True)
        verified = await asyncio.to_thread(
            storage.verify_direct_uploads,
            listing_id,
            payload.upload_id,
            image_dicts,
            settings.max_image_bytes,
        )
        _, stale_keys = await asyncio.to_thread(
            database.replace_images,
            listing_id,
            verified,
            current_user["id"],
        )
    except (ValueError, ListingValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except ListingOwnershipError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (DatabaseOperationError, R2UploadError) as exc:
        raise HTTPException(502, str(exc)) from exc
    if stale_keys:
        try:
            await asyncio.to_thread(storage.delete_objects, stale_keys)
        except R2UploadError:
            logger.exception("Could not delete stale R2 objects for %s", listing_id)
    return ListingImageUploadResponse(
        status="stored",
        listing_id=listing_id,
        image_count=len(verified),
        bytes=sum(image["size_bytes"] for image in verified),
        persisted=True,
        images=verified,
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


def connect_return_page(title: str, message: str, action: str) -> HTMLResponse:
    app_url = f"pokemarket://account?stripe={action}"
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #faf5ff; color: #18181b; }}
    main {{ max-width: 34rem; margin: 12vh auto; padding: 2rem; text-align: center; }}
    a {{ display: block; padding: 1rem; border-radius: 999px; background: #4f46e5; color: white;
         text-decoration: none; font-weight: 700; font-size: 1.1rem; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>{message}</p>
    <a href="{app_url}">Return to PokeMarket</a>
  </main>
  <script>window.location.replace({app_url!r});</script>
</body>
</html>"""
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.get("/api/v1/payments/connect/return", response_class=HTMLResponse)
async def connect_return():
    return connect_return_page(
        "Seller payout information submitted",
        "Return to PokeMarket. The app will ask Stripe for the current verification and payout status.",
        "return",
    )


@app.get("/api/v1/payments/connect/refresh", response_class=HTMLResponse)
async def connect_refresh():
    return connect_return_page(
        "Seller payout link expired",
        "Return to PokeMarket and tap Set Up Seller Payouts to continue with a fresh secure link.",
        "refresh",
    )


def checkout_return_page(title: str, message: str, action: str) -> HTMLResponse:
    app_url = f"pokemarket://account?checkout={action}"
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #faf5ff; color: #18181b; }}
    main {{ max-width: 34rem; margin: 12vh auto; padding: 2rem; text-align: center; }}
    a {{ display: block; padding: 1rem; border-radius: 999px; background: #4f46e5; color: white;
         text-decoration: none; font-weight: 700; font-size: 1.1rem; }}
  </style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p>{message}</p>
    <a href="{app_url}">Return to PokeMarket</a>
  </main>
  <script>window.location.replace({app_url!r});</script>
</body>
</html>"""
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@app.get("/checkout/success", response_class=HTMLResponse)
async def checkout_success(session_id: str | None = None):
    # The signed Stripe webhook, not this browser redirect, is authoritative.
    # session_id is accepted only because Stripe includes it in the return URL.
    return checkout_return_page(
        "Payment submitted",
        "Your payment was submitted securely. Return to PokeMarket to view the order status.",
        "success",
    )


@app.get("/checkout/cancelled", response_class=HTMLResponse)
async def checkout_cancelled():
    return checkout_return_page(
        "Checkout cancelled",
        "No purchase was completed. The card remains available unless another buyer completes payment.",
        "cancelled",
    )


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
            database.create_pending_order,
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
            shipping_address_collection={"allowed_countries": ["US"]},
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
    request: Request,
    background: BackgroundTasks,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    database=Depends(require_database),
):
    stripe_client = require_stripe()
    if not settings.stripe_webhook_secret:
        raise HTTPException(503, "STRIPE_WEBHOOK_SECRET is not configured yet.")
    if not stripe_signature:
        raise HTTPException(400, "Stripe-Signature header is required.")
    # Stripe signs the exact bytes it sends. Reading Request.body() avoids
    # FastAPI/Pydantic attempting to parse the JSON before signature checking.
    payload = await request.body()
    try:
        event = stripe_client.Webhook.construct_event(payload, stripe_signature, settings.stripe_webhook_secret)
    except Exception as exc:
        raise HTTPException(400, "Invalid Stripe webhook signature.") from exc
    event_type = event.get("type")
    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        checkout = event["data"]["object"]
        # Delayed payment methods also emit checkout.session.completed before
        # funds succeed. They must wait for async_payment_succeeded.
        if event_type == "checkout.session.completed" and checkout.get("payment_status") != "paid":
            return {"received": True}
        payment_intent_id = checkout.get("payment_intent")
        result = await asyncio.to_thread(
            database.claim_order_payment,
            checkout.get("id"),
            payment_intent_id,
        )
        if result and result["won"]:
            collected = checkout.get("collected_information") or {}
            shipping = checkout.get("shipping_details") or collected.get("shipping_details")
            if shipping:
                await asyncio.to_thread(database.save_shipping_details, checkout.get("id"), shipping)
            order = result["order"]
            background.add_task(send_push, settings, database, [order["seller_id"]], "Card sold", "You received a new PokeMarket order.", {"page": "orders", "order_id": order["id"]})
        if result and not result["won"] and not result["already_processed"]:
            order = result["order"]
            if not payment_intent_id:
                logger.error("Losing checkout %s has no payment intent to refund", checkout.get("id"))
                raise HTTPException(502, "Paid checkout could not be refunded yet.")
            try:
                refund = await asyncio.to_thread(
                    stripe_client.Refund.create,
                    payment_intent=payment_intent_id,
                    metadata={"order_id": order["id"], "reason": "listing_already_sold"},
                    idempotency_key=f"pokemarket-losing-order-{order['id']}",
                )
                await asyncio.to_thread(database.mark_order_refunded, order["id"], refund["id"])
            except Exception as exc:
                logger.exception("Automatic refund failed for losing order %s", order["id"])
                # A non-2xx response makes Stripe retry the webhook. The refund
                # request is idempotent, so a retry cannot issue two refunds.
                raise HTTPException(502, "Automatic refund is pending retry.") from exc
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
async def add_order_tracking(order_id: str, payload: TrackingRequest, background: BackgroundTasks, current_user=Depends(require_user), database=Depends(require_database)):
    visible = await asyncio.to_thread(database.get_order, order_id, current_user["id"])
    if visible is None or visible["seller_id"] != current_user["id"]:
        raise HTTPException(404, "Order not found.")
    try:
        tracking_number, carrier = classify_tracking_number(payload.tracking_number, payload.carrier)
        order = await asyncio.to_thread(
            database.set_tracking,
            order_id,
            current_user["id"],
            tracking_number,
            carrier,
            settings.confirmation_timeout_days,
        )
    except (ListingValidationError, TrackingValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    background.add_task(send_push, settings, database, [order["buyer_id"]], "Your card has shipped", "The seller added tracking to your PokeMarket order.", {"page": "orders", "order_id": order_id})
    return order


@app.post("/api/v1/orders/{order_id}/confirm-delivery", response_model=OrderResponse)
async def confirm_order_delivery(order_id: str, background: BackgroundTasks, current_user=Depends(require_user), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(database.confirm_delivery, order_id, current_user["id"], settings.seller_hold_days)
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    background.add_task(send_push, settings, database, [order["seller_id"]], "Delivery confirmed", "Buyer protection has started for your sale.", {"page": "orders", "order_id": order_id})
    return order


@app.post("/api/v1/orders/{order_id}/returns", response_model=OrderResponse)
async def request_order_return(order_id: str, payload: ReturnRequest, background: BackgroundTasks, current_user=Depends(require_user), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(database.request_return, order_id, current_user["id"], payload.reason, payload.notes)
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    background.add_task(send_push, settings, database, [order["seller_id"]], "Return requested", "A buyer requested a return. Review it in Orders.", {"page": "orders", "order_id": order_id})
    return order


@app.post("/api/v1/orders/{order_id}/returns/review", response_model=OrderResponse)
async def review_order_return(order_id: str, payload: ReturnReviewRequest, background: BackgroundTasks, current_user=Depends(require_user), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(database.review_return, order_id, current_user["id"], payload.approved)
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    title = "Return approved" if payload.approved else "Return escalated"
    body = "Add return tracking in Orders." if payload.approved else "The return needs administrator review."
    background.add_task(send_push, settings, database, [order["buyer_id"]], title, body, {"page": "orders", "order_id": order_id})
    return order


@app.post("/api/v1/orders/{order_id}/returns/tracking", response_model=OrderResponse)
async def add_return_tracking(order_id: str, payload: TrackingRequest, background: BackgroundTasks, current_user=Depends(require_user), database=Depends(require_database)):
    visible = await asyncio.to_thread(database.get_order, order_id, current_user["id"])
    if visible is None or visible["buyer_id"] != current_user["id"]:
        raise HTTPException(404, "Order not found.")
    try:
        tracking_number, carrier = classify_tracking_number(payload.tracking_number, payload.carrier)
        order = await asyncio.to_thread(
            database.set_return_tracking,
            order_id,
            current_user["id"],
            tracking_number,
            carrier,
            settings.confirmation_timeout_days,
        )
    except (ListingValidationError, TrackingValidationError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    background.add_task(send_push, settings, database, [order["seller_id"]], "Return shipped", "The buyer added return tracking.", {"page": "orders", "order_id": order_id})
    return order


@app.post("/api/v1/orders/{order_id}/returns/received", response_model=OrderResponse)
async def confirm_return_received(order_id: str, background: BackgroundTasks, current_user=Depends(require_user), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(database.begin_return_refund, order_id, current_user["id"])
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    if not order.get("stripe_payment_intent_id"):
        raise HTTPException(409, "This order has no Stripe payment to refund.")
    try:
        stripe_client = require_stripe()
        refund = await asyncio.to_thread(
            stripe_client.Refund.create,
            payment_intent=order["stripe_payment_intent_id"],
            metadata={"order_id": order_id, "reason": "approved_return"},
            idempotency_key=f"pokemarket-return-{order_id}",
        )
        order = await asyncio.to_thread(database.mark_order_refunded, order_id, refund["id"])
    except Exception as exc:
        logger.exception("Return refund failed for order %s", order_id)
        raise HTTPException(502, "Stripe refund is pending. Tap confirm again shortly.") from exc
    background.add_task(send_push, settings, database, [order["buyer_id"], order["seller_id"]], "Return refunded", "The return is complete and the refund was issued.", {"page": "orders", "order_id": order_id})
    return order


@app.post("/api/v1/admin/orders/{order_id}/returns/resolve", response_model=OrderResponse)
async def admin_resolve_return(order_id: str, payload: ReturnReviewRequest, background: BackgroundTasks, _admin=Depends(require_admin), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(
            database.resolve_return_dispute,
            order_id,
            payload.approved,
            settings.confirmation_timeout_days,
        )
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    background.add_task(send_push, settings, database, [order["buyer_id"], order["seller_id"]], "Return dispute updated", "An administrator resolved the return review.", {"page": "orders", "order_id": order_id})
    return order


@app.post("/api/v1/orders/{order_id}/complete", response_model=OrderResponse)
async def complete_order(order_id: str, background: BackgroundTasks, current_user=Depends(require_user), database=Depends(require_database)):
    try:
        order = await asyncio.to_thread(database.complete_order, order_id, current_user["id"])
    except ListingValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    if order is None:
        raise HTTPException(404, "Order not found.")
    order = await settle_seller_payout(order, database)
    background.add_task(send_push, settings, database, [order["buyer_id"], order["seller_id"]], "Order completed", "The PokeMarket order is complete.", {"page": "orders", "order_id": order_id})
    return order
