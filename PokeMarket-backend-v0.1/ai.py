import base64
import json
from uuid import uuid4
import httpx
from schemas import AIAnalysisResult

SCHEMA = {
    "type": "object",
    "properties": {
        "identification": {
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "set": {"type": ["string", "null"]},
                "number": {"type": ["string", "null"]},
                "language": {"type": ["string", "null"]},
                "variant": {"type": ["string", "null"]},
                "tcgdex_id": {"type": ["string", "null"]},
                "confidence": {"type": "number"},
                "observations": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["name","set","number","language","variant","tcgdex_id","confidence","observations"]
        },
        "condition": {
            "type": "object",
            "properties": {
                "estimated_condition": {"type": ["string", "null"]},
                "confidence": {"type": "number"},
                "surface": {"type": "string"},
                "corners": {"type": "string"},
                "edges": {"type": "string"},
                "centering": {"type": "string"},
                "warnings": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["estimated_condition","confidence","surface","corners","edges","centering","warnings"]
        },
        "authenticity": {
            "type": "object",
            "properties": {
                "screening_result": {"type": "string", "enum": ["no_obvious_red_flags","possible_red_flags","unable_to_assess"]},
                "confidence": {"type": "number"},
                "red_flags": {"type": "array", "items": {"type": "string"}},
                "manual_review_recommended": {"type": "boolean"}
            },
            "required": ["screening_result","confidence","red_flags","manual_review_recommended"]
        },
        "warnings": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["identification","condition","authenticity","warnings"]
}

PROMPT = """Analyze all supplied photographs as views of one Pokemon trading card.
Identify the card only when supported by visible evidence. Extract card name, set,
collector number, language, variant, and exact TCGdex ID only if reasonably confident.
Estimate condition conservatively from centering, corners, edges, surface wear,
whitening, scratches, dents and creases. Perform only preliminary authenticity
screening for obvious visual red flags. Never guarantee authenticity and never claim
an official PSA/CGC/BGS grade. Return only JSON matching the schema."""

class StubProvider:
    name = "stub"
    async def analyze(self, images):
        return str(uuid4()), AIAnalysisResult(
            identification={"status":"needs_ai_provider","name":None,"set":None,"number":None,"language":None,"variant":None,"tcgdex_id":None,"confidence":0.0,"observations":[]},
            condition={"status":"not_analyzed","estimated_condition":None,"confidence":0.0,"surface":"","corners":"","edges":"","centering":"","warnings":[]},
            authenticity={"screening_result":"unable_to_assess","confidence":0.0,"red_flags":[],"manual_review_recommended":True},
            warnings=["AI_PROVIDER is stub."]
        )

class GeminiProvider:
    name = "gemini"
    def __init__(self, settings):
        if not settings.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when AI_PROVIDER=gemini")
        self.key = settings.gemini_api_key
        self.model = settings.gemini_model

    async def analyze(self, images):
        parts = [{"text": PROMPT}]
        for data, mime in images:
            parts.append({"inline_data":{"mime_type":mime,"data":base64.b64encode(data).decode("ascii")}})

        payload = {
            "contents":[{"role":"user","parts":parts}],
            "generationConfig":{
                "responseMimeType":"application/json",
                "responseJsonSchema":SCHEMA,
                "thinkingConfig":{"thinkingLevel":"medium"}
            }
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, headers={"x-goog-api-key":self.key,"Content-Type":"application/json"}, json=payload)
            r.raise_for_status()
            body = r.json()

        text = body["candidates"][0]["content"]["parts"][0]["text"]
        return str(uuid4()), AIAnalysisResult.model_validate(json.loads(text))

def get_ai_provider(settings):
    p = settings.ai_provider.strip().lower()
    if p == "stub":
        return StubProvider()
    if p == "gemini":
        return GeminiProvider(settings)
    raise ValueError(f"Unsupported AI_PROVIDER: {settings.ai_provider}")
