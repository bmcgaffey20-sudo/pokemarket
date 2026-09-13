import asyncio
import base64
import json
import random
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

Identify:
- card name
- set name if visible/reasonably known
- collector/card number
- language
- variant
- exact TCGdex ID ONLY when highly confident

Do NOT make authenticity judgments in this stage.
Do NOT call an unfamiliar recent card fake/custom/proxy.
Read visible printed information carefully.
If uncertain, lower confidence instead of inventing details.
Return only JSON matching the response schema.
"""


ASSESSMENT_PROMPT = """
You are the grounded condition and authenticity-screening stage of PokeMarket.

The backend already identified the card and queried TCGdex. Treat supplied
TCGdex data as evidence that the referenced official card record exists.

Grounding rules:
- Do NOT call a card fake merely because the copyright year, set code, name,
  wording, artwork, or layout is unfamiliar when consistent with TCGdex.
- Do NOT describe a matched TCGdex card as non-existent.
- A TCGdex match does NOT prove the photographed physical card is genuine.
- Authenticity screening must focus on visible discrepancies: typography,
  borders, symbols, print quality, card back, texture/holo behavior, proportions,
  alignment, reproduction artifacts, etc.
- If photos are insufficient, use unable_to_assess rather than inventing red flags.
- Missing back photos must reduce condition/authenticity confidence.

Condition is a conservative visual estimate, not an official PSA/CGC/BGS grade.
Authenticity is preliminary visual screening only, never certification.
Return only JSON matching the response schema.
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

        # Render can still control the preferred model with GEMINI_MODEL.
        # Stable fallbacks protect scans from temporary 503/capacity problems.
        preferred = settings.gemini_model or "gemini-3.8-flash"
        self.models = []
        for model in [preferred, "gemini-3.7-flash", "gemini-3.6-flash"]:
            if model not in self.models:
                self.models.append(model)

    def _image_parts(self, images):
        return [
            {
                "inline_data": {
                    "mime_type": mime,
                    "data": base64.b64encode(data).decode("ascii"),
                }
            }
            for data, mime in images
        ]

    async def _call_model(self, model, prompt, schema, images):
        parts = [{"text": prompt}]
        parts.extend(self._image_parts(images))

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "thinkingConfig": {"thinkingLevel": "low"},
            },
        }

        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/{model}:generateContent"
        )

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                url,
                headers={
                    "x-goog-api-key": self.key,
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        # Only transient errors should be retried/fallen back.
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            raise TransientGeminiError(
                model=model,
                status_code=response.status_code,
                body=response.text[:500],
            )

        response.raise_for_status()
        body = response.json()

        text = body["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

    async def _generate_json(self, prompt, schema, images):
        errors = []

        # Try each stable model. Each model receives exponential-backoff retries.
        for model in self.models:
            for attempt in range(4):
                try:
                    result = await self._call_model(
                        model=model,
                        prompt=prompt,
                        schema=schema,
                        images=images,
                    )
                    return result

                except TransientGeminiError as exc:
                    errors.append(str(exc))

                    # 1s, 2s, 4s (+ jitter), then move to fallback model.
                    if attempt < 3:
                        delay = (2 ** attempt) + random.uniform(0.2, 0.9)
                        await asyncio.sleep(delay)

                except (KeyError, IndexError, json.JSONDecodeError) as exc:
                    # Bad model output may be transient; retry this model once,
                    # then continue through the normal retry/fallback path.
                    errors.append(f"{model}: invalid structured response: {exc}")
                    if attempt < 3:
                        delay = (2 ** attempt) + random.uniform(0.2, 0.9)
                        await asyncio.sleep(delay)

        raise RuntimeError(
            "All Gemini models/retries failed. "
            + " | ".join(errors[-6:])
        )

    async def identify(self, images):
        return await self._generate_json(
            IDENTIFICATION_PROMPT,
            IDENTIFICATION_SCHEMA,
            images,
        )

    async def assess(self, images, identification, tcgdex_reference):
        prompt = ASSESSMENT_PROMPT + "\n\nAI identification proposal:\n"
        prompt += json.dumps(
            identification,
            ensure_ascii=False,
            indent=2,
        )

        if tcgdex_reference:
            prompt += "\n\nTCGdex reference record:\n"
            prompt += json.dumps(
                tcgdex_reference,
                ensure_ascii=False,
                indent=2,
            )
        else:
            prompt += (
                "\n\nTCGdex reference record: NONE FOUND. "
                "Do not automatically label the card counterfeit. "
                "Reduce confidence and recommend manual review when appropriate."
            )

        return await self._generate_json(
            prompt,
            ASSESSMENT_SCHEMA,
            images,
        )


class TransientGeminiError(Exception):
    def __init__(self, model, status_code, body):
        self.model = model
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"{model} returned transient HTTP {status_code}: {body}"
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
