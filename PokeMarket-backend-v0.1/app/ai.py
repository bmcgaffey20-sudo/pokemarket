from typing import Any
from uuid import uuid4


class CardAIProvider:
    name = "base"

    async def analyze(
        self,
        image_count: int,
        views: list[str],
        include_condition: bool,
        include_authenticity: bool,
    ) -> dict[str, Any]:
        raise NotImplementedError


class StubCardAIProvider(CardAIProvider):
    name = "stub"

    async def analyze(
        self,
        image_count: int,
        views: list[str],
        include_condition: bool,
        include_authenticity: bool,
    ) -> dict[str, Any]:
        return {
            "scan_id": str(uuid4()),
            "identification": {
                "status": "needs_ai_provider",
                "name": None,
                "set": None,
                "number": None,
                "language": None,
                "confidence": 0.0,
            },
            "condition": (
                {
                    "status": "not_analyzed",
                    "estimated_condition": None,
                    "confidence": 0.0,
                }
                if include_condition
                else {"status": "disabled"}
            ),
            "authenticity": (
                {
                    "status": "not_analyzed",
                    "confidence": 0.0,
                    "manual_review_recommended": True,
                }
                if include_authenticity
                else {"status": "disabled"}
            ),
            "warnings": [
                "AI image analysis is not configured yet.",
                "No authenticity or grading claim should be made from this result.",
            ],
        }


def get_ai_provider(provider_name: str) -> CardAIProvider:
    if provider_name.lower() == "stub":
        return StubCardAIProvider()
    raise ValueError(f"Unsupported AI provider: {provider_name}")
