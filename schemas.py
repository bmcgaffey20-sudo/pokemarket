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
    display_name: str
    created_at: datetime
    seller_tier: int
    completed_buys: int
    successful_sales: int
    successful_sales_over_100: int
    max_listing_cents: int | None

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
