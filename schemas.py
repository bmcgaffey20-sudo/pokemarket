from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    version: str
    ai_provider: str
    database: str

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
