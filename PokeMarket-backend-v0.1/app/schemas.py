from typing import Any, Literal

from pydantic import BaseModel, Field


CaptureView = Literal["top_straight", "top_left", "top_right", "back"]


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str


class CardSearchResponse(BaseModel):
    query: str
    cards: list[dict[str, Any]]


class ScanAnalyzeRequest(BaseModel):
    image_count: int = Field(ge=1, le=10)
    views: list[CaptureView] = Field(min_length=1)
    include_condition: bool = True
    include_authenticity: bool = True


class ScanAnalysisResponse(BaseModel):
    scan_id: str
    status: str
    provider: str
    identification: dict[str, Any]
    condition: dict[str, Any]
    authenticity: dict[str, Any]
    tcgdex: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
