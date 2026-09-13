from fastapi import FastAPI,File,Form,HTTPException,UploadFile
from fastapi.middleware.cors import CORSMiddleware
from .config import get_settings
from .ai import get_provider
from .tcgdex import TCGdexClient
from .schemas import HealthResponse,CardSearchResponse,ScanAnalysisResponse
s=get_settings();tcg=TCGdexClient(s.tcgdex_base_url)
app=FastAPI(title=s.app_name,version="2.0.0")
app.add_middleware(CORSMiddleware,allow_origins=s.cors_origin_list,allow_credentials=False,allow_methods=["*"],allow_headers=["*"])
@app.get("/api/v1/health",response_model=HealthResponse)
async def health():return HealthResponse(status="ok",service=s.app_name,environment=s.environment,version="2.0.0",ai_provider=s.ai_provider)
@app.get("/api/v1/cards/search",response_model=CardSearchResponse)
async def search(q:str):
    if not q.strip():raise HTTPException(400,"Empty query")
    try:return CardSearchResponse(query=q,cards=await tcg.search_cards(q))
    except Exception as e:raise HTTPException(502,f"TCGdex failed: {e}") from e
@app.get("/api/v1/cards/{card_id}")
async def card(card_id:str):
    try:return await tcg.get_card(card_id)
    except Exception as e:raise HTTPException(502,f"TCGdex failed: {e}") from e
async def images(files):
    if not files or len(files)>s.max_images:raise HTTPException(400,"Invalid image count")
    out=[]
    for f in files:
        if f.content_type not in {"image/jpeg","image/png","image/webp"}:raise HTTPException(415,"Unsupported image type")
        b=await f.read()
        if not b or len(b)>s.max_image_bytes:raise HTTPException(413,"Empty or oversized image")
        out.append((b,f.content_type))
    return out
@app.post("/api/v1/scan/analyze",response_model=ScanAnalysisResponse)
async def analyze(files:list[UploadFile]=File(...),include_condition:bool=Form(True),include_authenticity:bool=Form(True)):
    ims=await images(files)
    try:p=get_provider(s);sid,a=await p.analyze(ims)
    except ValueError as e:raise HTTPException(500,str(e)) from e
    except Exception as e:raise HTTPException(502,f"AI analysis failed: {e}") from e
    ident=dict(a.identification);cond=dict(a.condition);auth=dict(a.authenticity);warn=list(a.warnings)
    if not include_condition:cond={"status":"disabled"}
    if not include_authenticity:auth={"status":"disabled"}
    match=None
    try:match=await tcg.resolve(ident.get("name"),ident.get("number"),ident.get("tcgdex_id"))
    except Exception:warn.append("TCGdex verification failed.")
    ident["verification_status"]="matched_tcgdex" if match else "not_verified"
    if match:ident["tcgdex_verified_id"]=match.get("id")
    else:warn.append("No sufficiently strong TCGdex match was established.")
    warn.append("Authenticity is preliminary visual screening, not certification.")
    return ScanAnalysisResponse(scan_id=sid,status="complete",provider=p.name,identification=ident,condition=cond,authenticity=auth,tcgdex=match,warnings=warn)
@app.post("/api/v1/scan/upload")
async def upload(files:list[UploadFile]=File(...)):
    ims=await images(files);return {"status":"received","image_count":len(ims),"bytes":sum(len(x[0]) for x in ims),"persisted":False}
