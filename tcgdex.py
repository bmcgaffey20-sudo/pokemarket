import httpx

class TCGdexClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    async def search_cards(self, query):
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{self.base_url}/en/cards", params={"name": query})
            r.raise_for_status()
            data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data", data.get("cards", []))
        return []

    async def get_card(self, card_id):
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{self.base_url}/en/cards/{card_id}")
            r.raise_for_status()
            return r.json()

    async def resolve_candidate(self, name, card_number, tcgdex_id):
        if tcgdex_id:
            try:
                return await self.get_card(tcgdex_id)
            except Exception:
                pass
        if not name:
            return None

        cards = await self.search_cards(name)
        wanted_name = name.strip().lower()
        wanted_number = (card_number or "").strip().lstrip("0")
        ranked = []

        for card in cards:
            score = 0
            cname = str(card.get("name", "")).strip().lower()
            cnum = str(card.get("localId", "")).strip().lstrip("0")
            if cname == wanted_name:
                score += 10
            elif wanted_name in cname:
                score += 3
            if wanted_number and cnum == wanted_number:
                score += 8
            ranked.append((score, card))

        if not ranked:
            return None
        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[0][1] if ranked[0][0] >= 10 else None
