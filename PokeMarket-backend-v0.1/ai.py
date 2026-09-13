import base64
import json
from uuid import uuid4

import httpx


IDENTIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "set": {"type": ["string", "null"]},
        "number": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
        "variant": {"type": ["string", "null"]},
        "tcgdex_id": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "observations": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "name", "set", "number", "language", "variant",
        "tcgdex_id", "confidence", "observations"
    ],
}

ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "condition": {
            "type": "object",
            "properties": {
                "estimated_condition": {"type": ["string", "null"]},
                "confidence": {"type": "number"},
                "surface": {"type": "string"},
                "corners": {"type": "string"},
                "edges": {"type": "string"},
                "centering": {"type": "string"},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "estimated_condition", "confidence", "surface",
                "corners", "edges", "centering", "warnings"
            ],
        },
        "authenticity": {
            "type": "object",
            "properties": {
                "screening_result": {
                    "type": "string",
                    "enum": [
                        "no_obvious_red_flags",
                        "possible_red_flags",
                        "unable_to_assess"
                    ],
                },
                "confidence": {"type": "number"},
                "red_flags": {"type": "array", "items": {"type": "string"}},
                "manual_review_recommended": {"type": "boolean"},
            },
            "required": [
                "screening_result", "confidence",
                "red_flags", "manual_review_recommended"
            ],
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["condition", "authenticity", "warnings"],
}


IDENTIFICATION_PROMPT = """
You are the identification stage of PokeMarket's Pokemon trading card scanner.

Use all supplied photographs as views of ONE card.

Your job in this stage is ONLY to identify the card:
- card name
- set name if visible/reasonably known
- collector/card number
- language
- variant
- exact TCGdex ID ONLY when you are highly confident

Rules:
- Do NOT decide whether the card is authentic in this stage.
- Do NOT call a card fake, custom, proxy, counterfeit, or unofficial merely
  because its design, copyright year, set code, wording, or artwork seems unfamiliar.
- New and recent official releases may be outside your memorized knowledge.
- Read visible printed information carefully.
- If uncertain, lower confidence instead of inventing details.
- Return only JSON matching the response schema.
"""


ASSESSMENT_PROMPT = """
You are the condition and authenticity-screening stage of PokeMarket.

The backend has already performed card identification and looked up a candidate
record in TCGdex. You MUST treat the supplied TCGdex reference data as evidence
that the card identity/design may be an official published card.

Important grounding rules:
- Do NOT flag a copyright year, set code, collector number, card name, or layout
  as fake merely because it is unfamiliar to you when it is consistent with the
  supplied TCGdex reference.
- Do NOT call a matched TCGdex card "non-existent".
- A TCGdex match verifies that an official card record exists; it does NOT prove
  the photographed physical card is genuine.
- Authenticity screening should focus on PHOTO-VISIBLE discrepancies between the
  photographed item and the grounded reference: typography, borders, symbols,
  printing quality, card back, holo/texture behavior, proportions, alignment,
  obvious reproduction artifacts, etc.
- If the supplied photos are insufficient, return unable_to_assess rather than
  confidently inventing red flags.
- Missing back photos must reduce authenticity and condition confidence.

Condition rules:
- Estimate visible condition conservatively from centering, corners, edges,
  surface wear, whitening, scratches, dents, creases and print defects.
- This is NOT an official PSA/CGC/BGS grade.

Authenticity rules:
- This is preliminary visual screening only, never certification.
- "no_obvious_red_flags" means only that the photos do not show obvious problems.
- Return only JSON matching the response schema.
"""


class StubProvider:
    name = "stub"

    async def identify(self, images):
        return {
            "name": None,
            "set": None,
            "number": None,
            "language": None,
            "variant": None,
            "tcgdex_id": None,
            "confidence": 0.0,
            "observations": [],
            "status": "needs_ai_provider",
        }

    async def assess(self, images, identification, tcgdex_reference):
        return {
            "condition": {
                "estimated_condition": None,
                "confidence": 0.0,
                "surface": "",
                "corners": "",
                "edges": "",
                "centering": "",
                "warnings": [],
            },
            "authenticity": {
                "screening_result": "unable_to_assess",
                "confidence": 0.0,
                "red_flags": [],
                "manual_review_recommended": True,
            },
            "warnings": ["AI_PROVIDER is stub."],
        }


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings):
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when AI_PROVIDER=gemini")
        self.key = settings.gemini_api_key
        self.model = settings.gemini_model

    def _image_parts(self, images):
        parts = []
        for data, mime in images:
            parts.append({
                "inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(data).decode("ascii"),
                }
            })
        return parts

    async def _generate_json(self, prompt, schema, images):
        parts = [{"text": prompt}]
        parts.extend(self._image_parts(images))

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "thinkingConfig": {"thinkingLevel": "medium"},
            },
        }

        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/{self.model}:generateContent"
        )

        last_error = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    response = await client.post(
                        url,
                        headers={
                            "x-goog-api-key": self.key,
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()

                text = body["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
            except Exception as exc:
                last_error = exc
                if attempt == 2:
                    raise
                import asyncio
                await asyncio.sleep(1.5 * (attempt + 1))

        raise last_error

    async def identify(self, images):
        return await self._generate_json(
            IDENTIFICATION_PROMPT,
            IDENTIFICATION_SCHEMA,
            images,
        )

    async def assess(self, images, identification, tcgdex_reference):
        grounded_prompt = ASSESSMENT_PROMPT + "\n\n"
        grounded_prompt += "AI identification proposal:\n"
        grounded_prompt += json.dumps(identification, ensure_ascii=False, indent=2)

        if tcgdex_reference:
            grounded_prompt += "\n\nTCGdex reference record:\n"
            grounded_prompt += json.dumps(tcgdex_reference, ensure_ascii=False, indent=2)
        else:
            grounded_prompt += (
                "\n\nTCGdex reference record: NONE FOUND. "
                "Because no reference match was found, do not automatically label "
                "the card counterfeit. Reduce confidence and recommend manual review."
            )

        return await self._generate_json(
            grounded_prompt,
            ASSESSMENT_SCHEMA,
            images,
        )


def get_ai_provider(settings):
    provider = settings.ai_provider.strip().lower()

    if provider == "stub":
        return StubProvider()

    if provider == "gemini":
        return GeminiProvider(settings)

    raise ValueError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")


def new_scan_id():
    return str(uuid4())
