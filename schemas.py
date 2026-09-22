from datetime import datetime
from typing import Any, Literal

import re

from pydantic import BaseModel, Field, field_validator

class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    version: str
    ai_provider: str
    database: str
    auth: str
    email: str = "not_configured"
    payments: str = "not_configured"

class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=10, max_length=128)
    legacy_claim_code: str | None = Field(default=None, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        normalized = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
            raise ValueError("Enter a valid email address.")
        return normalized

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value):
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Display name cannot be blank.")
        return normalized

class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_login_email(cls, value):
        return value.strip().lower()

class UserResponse(BaseModel):
    id: str
    email: str
    email_verified: bool = False
    display_name: str
    created_at: datetime
    seller_tier: int
    completed_buys: int
    successful_sales: int
    successful_sales_over_100: int
    max_listing_cents: int | None
    stripe_connected: bool = False
    stripe_details_submitted: bool = False
    stripe_charges_enabled: bool = False
    stripe_payouts_enabled: bool = False
    stripe_requirements_due: list[str] = Field(default_factory=list)

class CheckoutRequest(BaseModel):
    listing_id: str = Field(min_length=1, max_length=64)
    success_url: str | None = Field(default=None, max_length=1000)
    cancel_url: str | None = Field(default=None, max_length=1000)
    shipping_cents: int = Field(default=0, ge=0, le=100_000)

class CheckoutResponse(BaseModel):
    order_id: str
    checkout_url: str
    stripe_session_id: str
    item_cents: int
    shipping_cents: int
    commission_cents: int
    seller_amount_cents: int
    currency: str

class OrderResponse(BaseModel):
    id: str
    listing_id: str
    buyer_id: str
    seller_id: str
    status: str
    item_cents: int
    shipping_cents: int
    commission_cents: int
    seller_amount_cents: int
    currency: str
    stripe_checkout_session_id: str | None = None
    stripe_payment_intent_id: str | None = None
    tracking_number: str | None = None
    shipping_name: str | None = None
    shipping_line1: str | None = None
    shipping_line2: str | None = None
    shipping_city: str | None = None
    shipping_state: str | None = None
    shipping_postal_code: str | None = None
    shipping_country: str | None = None
    listing_title: str | None = None
    card_name: str | None = None
    set_name: str | None = None
    card_number: str | None = None
    buyer_display_name: str | None = None
    seller_display_name: str | None = None
    delivered_at: datetime | None = None
    hold_until: datetime | None = None
    completed_at: datetime | None = None
    payout_status: str = "pending"
    stripe_transfer_id: str | None = None
    stripe_refund_id: str | None = None
    created_at: datetime
    updated_at: datetime

class TrackingRequest(BaseModel):
    tracking_number: str = Field(min_length=3, max_length=128)

class CheckoutProviderResponse(BaseModel):
    url: str
    stripe_account_id: str

class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserResponse
    legacy_listings_claimed: int = 0

class CardSearchResponse(BaseModel):
    query: str
    cards: list[dict[str, Any]]

class AIAnalysisResult(BaseModel):
    identification: dict[str, Any]
    condition: dict[str, Any]
    authenticity: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)

class ScanAnalysisResponse(BaseModel):
    scan_id: str
    status: str
    provider: str
    identification: dict[str, Any]
    condition: dict[str, Any]
    authenticity: dict[str, Any]
    tcgdex: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)

class ScanJobRequest(BaseModel):
    listing_id: str = Field(min_length=1, max_length=64)
    include_condition: bool = True
    include_authenticity: bool = True

class ScanJobAccepted(BaseModel):
    job_id: str
    listing_id: str
    status: Literal["queued", "processing"]

class ScanJobStatus(BaseModel):
    job_id: str
    listing_id: str
    status: Literal["queued", "processing", "complete", "failed"]
    result: ScanAnalysisResponse | None = None
    error: str | None = None

class StoredImage(BaseModel):
    label: str
    object_key: str
    content_type: str
    size_bytes: int
    url: str

class ListingImageUploadResponse(BaseModel):
    status: str
    listing_id: str
    image_count: int
    bytes: int
    persisted: bool
    images: list[StoredImage]

class DirectUploadItemRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    size_bytes: int = Field(gt=0, le=12_000_000)

class DirectUploadSessionRequest(BaseModel):
    images: list[DirectUploadItemRequest] = Field(min_length=4, max_length=9)

class DirectUploadTarget(BaseModel):
    label: str
    object_key: str
    content_type: str
    size_bytes: int
    upload_url: str

class DirectUploadSessionResponse(BaseModel):
    listing_id: str
    upload_id: str
    expires_in: int
    uploads: list[DirectUploadTarget]

class DirectUploadCompleteItem(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    object_key: str = Field(min_length=1, max_length=1000)
    content_type: Literal["image/jpeg", "image/png", "image/webp"]
    size_bytes: int = Field(gt=0, le=12_000_000)

class DirectUploadCompleteRequest(BaseModel):
    upload_id: str = Field(min_length=32, max_length=32)
    images: list[DirectUploadCompleteItem] = Field(min_length=4, max_length=9)

class ListingUpsertRequest(BaseModel):
    scan_id: str | None = Field(default=None, max_length=64)
    status: Literal["draft", "published", "sold", "archived"] = "draft"
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    price_cents: int | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    card_name: str | None = Field(default=None, max_length=200)
    set_name: str | None = Field(default=None, max_length=200)
    card_number: str | None = Field(default=None, max_length=64)
    tcgdex_id: str | None = Field(default=None, max_length=128)
    estimated_condition: str | None = Field(default=None, max_length=64)
    ai_result: dict[str, Any] | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value):
        return value.upper()

class ListingImageRecord(BaseModel):
    id: int
    label: str
    object_key: str
    content_type: str
    size_bytes: int
    sort_order: int
    is_defect: bool
    created_at: datetime
    url: str | None = None

class ListingResponse(BaseModel):
    id: str
    seller_id: str
    scan_id: str | None = None
    status: str
    title: str | None = None
    description: str | None = None
    price_cents: int | None = None
    currency: str
    card_name: str | None = None
    set_name: str | None = None
    card_number: str | None = None
    tcgdex_id: str | None = None
    estimated_condition: str | None = None
    ai_result: dict[str, Any] | None = None
    photos_persisted: bool
    created_at: datetime
    updated_at: datetime
    images: list[ListingImageRecord] = Field(default_factory=list)
