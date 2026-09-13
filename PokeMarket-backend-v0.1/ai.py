import base64,json,httpx
from uuid import uuid4
from .schemas import AIAnalysisResult
SCHEMA={"type":"object","properties":{
"identification":{"type":"object","properties":{"name":{"type":["string","null"]},"set":{"type":["string","null"]},"number":{"type":["string","null"]},"language":{"type":["string","null"]},"variant":{"type":["string","null"]},"tcgdex_id":{"type":["string","null"]},"confidence":{"type":"number"},"observations":{"type":"array","items":{"type":"string"}}},"required":["name","set","number","language","variant","tcgdex_id","confidence","observations"]},
"condition":{"type":"object","properties":{"estimated_condition":{"type":["string","null"]},"confidence":{"type":"number"},"surface":{"type":"string"},"corners":{"type":"string"},"edges":{"type":"string"},"centering":{"type":"string"},"warnings":{"type":"array","items":{"type":"string"}}},"required":["estimated_condition","confidence","surface","corners","edges","centering","warnings"]},
"authenticity":{"type":"object","properties":{"screening_result":{"type":"string","enum":["no_obvious_red_flags","possible_red_flags","unable_to_assess"]},"confidence":{"type":"number"},"red_flags":{"type":"array","items":{"type":"string"}},"manual_review_recommended":{"type":"boolean"}},"required":["screening_result","confidence","red_flags","manual_review_recommended"]},
"warnings":{"type":"array","items":{"type":"string"}}},"required":["identification","condition","authenticity","warnings"]}
PROMPT="""Analyze all photographs as views of one Pokemon trading card. Identify name, set, collector number, language and variant only when supported by visible evidence. Estimate visible condition conservatively from centering, corners, edges, surface, whitening, scratches, dents and creases. Perform preliminary authenticity screening for obvious visual inconsistencies. Never guarantee authenticity and never claim an official grading-company grade. Return only JSON matching the schema."""
class Stub:
    name="stub"
    async def analyze(self,images):
        return str(uuid4()),AIAnalysisResult(identification={"status":"needs_ai_provider","name":None,"set":None,"number":None,"language":None,"variant":None,"tcgdex_id":None,"confidence":0,"observations":[]},condition={"status":"not_analyzed","estimated_condition":None,"confidence":0,"surface":"","corners":"","edges":"","centering":"","warnings":[]},authenticity={"screening_result":"unable_to_assess","confidence":0,"red_flags":[],"manual_review_recommended":True},warnings=["AI_PROVIDER is stub."])
class Gemini:
    name="gemini"
    def __init__(self,s):
        if not s.gemini_api_key:raise ValueError("GEMINI_API_KEY is required")
        self.key=s.gemini_api_key;self.model=s.gemini_model
    async def analyze(self,images):
        parts=[{"text":PROMPT}]
        for b,m in images:parts.append({"inline_data":{"mime_type":m,"data":base64.b64encode(b).decode()}})
        payload={"contents":[{"role":"user","parts":parts}],"generationConfig":{"responseMimeType":"application/json","responseJsonSchema":SCHEMA,"thinkingConfig":{"thinkingLevel":"medium"}}}
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=120) as c:
            r=await c.post(url,headers={"x-goog-api-key":self.key,"Content-Type":"application/json"},json=payload);r.raise_for_status();d=r.json()
        parsed=json.loads(d["candidates"][0]["content"]["parts"][0]["text"])
        return str(uuid4()),AIAnalysisResult.model_validate(parsed)
def get_provider(s):
    if s.ai_provider.lower()=="stub":return Stub()
    if s.ai_provider.lower()=="gemini":return Gemini(s)
    raise ValueError(f"Unsupported AI_PROVIDER: {s.ai_provider}")
