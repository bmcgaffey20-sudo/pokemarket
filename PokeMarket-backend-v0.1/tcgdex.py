import httpx
class TCGdexClient:
    def __init__(self,base_url): self.base_url=base_url.rstrip("/")
    async def search_cards(self,q):
        async with httpx.AsyncClient(timeout=20) as c:
            r=await c.get(f"{self.base_url}/en/cards",params={"name":q});r.raise_for_status();d=r.json()
        return d if isinstance(d,list) else d.get("data",d.get("cards",[]))
    async def get_card(self,cid):
        async with httpx.AsyncClient(timeout=20) as c:
            r=await c.get(f"{self.base_url}/en/cards/{cid}");r.raise_for_status();return r.json()
    async def resolve(self,name,number,cid):
        if cid:
            try:return await self.get_card(cid)
            except Exception:pass
        if not name:return None
        cards=await self.search_cards(name);want=(number or "").lstrip("0").strip();best=None;score=-1
        for x in cards:
            s=10 if str(x.get("name","")).lower()==name.lower() else 0
            if want and str(x.get("localId","")).lstrip("0")==want:s+=8
            if s>score:score,best=s,x
        return best if score>=10 else None
