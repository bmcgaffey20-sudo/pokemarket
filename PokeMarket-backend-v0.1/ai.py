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
        "name",
        "set",
        "number",
        "language",
        "variant",
        "tcgdex_id",
        "confidence",
        "observations",
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
                "estimated_condition",
                "confidence",
                "surface",
                "corners",
                "edges",
                "centering",
                "warnings",
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
                        "unable_to_assess",
                    ],
                },
                "confidence": {"type": "number"},
                "red_flags": {"type": "array", "items": {"type": "string"}},
                "manual_review_recommended": {"type": "boolean"},
            },
            "required": [
                "screening_result",
                "confidence",
                "red_flags",
                "manual_review_recommended",
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
            raise ValueError(
                "GEMINI_API_KEY is required when AI_PROVIDER=gemini"
            )

        self.key = settings.gemini_api_key

        # Render can still control the preferred model with GEMINI_MODEL.
        # Fallback models are retained, but retries are intentionally conservative
        # so a single scan does not hammer Gemini after a 429.
        preferred = settings.gemini_model or "gemini-3.8-flash"
        self.models = []

        for model in [
            preferred,
            "gemini-3.7-flash",
            "gemini-3.6-flash",
        ]:
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

    @staticmethod
    def _parse_retry_after(response):
        """
        Return Retry-After in seconds when Google provides it.
        Supports both integer and decimal values. Returns None if absent/invalid.
        """
        value = response.headers.get("Retry-After")
        if not value:
            return None

        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return None

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

        if response.status_code in {408, 429, 500, 502, 503, 504}:
            raise TransientGeminiError(
                model=model,
                status_code=response.status_code,
                body=response.text[:500],
                retry_after=self._parse_retry_after(response),
            )

        response.raise_for_status()
        body = response.json()

        text = body["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

    async def _generate_json(self, prompt, schema, images):
        errors = []

        # Keep retries deliberately low. A scan already performs two AI stages,
        # so aggressive retry loops can quickly turn one scan into many requests.
        max_attempts_per_model = 2

        for model_index, model in enumerate(self.models):
            for attempt in range(max_attempts_per_model):
                try:
                    return await self._call_model(
                        model=model,
                        prompt=prompt,
                        schema=schema,
                        images=images,
                    )

                except TransientGeminiError as exc:
                    errors.append(str(exc))

                    # 429 = quota/rate limiting. Back off much more heavily.
                    if exc.status_code == 429:
                        if exc.retry_after is not None:
                            delay = max(exc.retry_after, 8.0)
                        else:
                            delay = 10.0 * (attempt + 1)

                        delay += random.uniform(0.5, 2.0)

                    # Capacity/server errors should retry more gently.
                    elif exc.status_code in {500, 502, 503, 504}:
                        delay = (3.0 * (attempt + 1)) + random.uniform(0.3, 1.2)

                    else:
                        delay = (2.0 * (attempt + 1)) + random.uniform(0.2, 0.8)

                    # Retry the same model once.
                    if attempt < max_attempts_per_model - 1:
                        await asyncio.sleep(delay)
                        continue

                    # Before moving to a fallback model, add a small cooldown.
                    if model_index < len(self.models) - 1:
                        await asyncio.sleep(min(delay, 12.0))

                except (KeyError, IndexError, json.JSONDecodeError) as exc:
                    errors.append(
                        f"{model}: invalid structured response: {exc}"
                    )

                    if attempt < max_attempts_per_model - 1:
                        await asyncio.sleep(2.0 + random.uniform(0.2, 0.8))
                        continue

        raise RuntimeError(
            "All Gemini models/retries failed. "
            + " | ".join(errors[-8:])
        )

    async def identify(self, images):
        return await self._generate_json(
            IDENTIFICATION_PROMPT,
            IDENTIFICATION_SCHEMA,
            images,
        )

    async def assess(self, images, identification, tcgdex_reference):
        # A single card scan uses Gemini twice. Give the service a short cooldown
        # between the identification request and the assessment request.
        await asyncio.sleep(3.0)

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
                default=str,
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
    def __init__(
        self,
        model,
        status_code,
        body,
        retry_after=None,
    ):
        self.model = model
        self.status_code = status_code
        self.body = body
        self.retry_after = retry_after

        retry_text = (
            f", retry_after={retry_after}s"
            if retry_after is not None
            else ""
        )

        super().__init__(
            f"{model} returned transient HTTP {status_code}"
            f"{retry_text}: {body}"
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
