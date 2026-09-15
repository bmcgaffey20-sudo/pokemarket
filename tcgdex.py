import httpx


class TCGdexClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    async def search_cards(self, query):
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.base_url}/en/cards",
                params={"name": query},
            )
            response.raise_for_status()
            data = response.json()

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data", data.get("cards", []))
        return []

    async def get_card(self, card_id):
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.base_url}/en/cards/{card_id}"
            )
            response.raise_for_status()
            return response.json()

    async def resolve_candidate(self, name, card_number, tcgdex_id):
        # Strongest path: AI supplied an exact ID that TCGdex recognizes.
        if tcgdex_id:
            try:
                exact = await self.get_card(tcgdex_id)
                return exact, "exact_id"
            except Exception:
                pass

        if not name:
            return None, "no_match"

        cards = await self.search_cards(name)
        if not cards:
            return None, "no_match"

        wanted_name = name.strip().lower()
        wanted_number = (card_number or "").strip().lstrip("0")

        ranked = []

        for card in cards:
            score = 0
            candidate_name = str(card.get("name", "")).strip().lower()
            candidate_number = str(card.get("localId", "")).strip().lstrip("0")

            exact_name = candidate_name == wanted_name
            exact_number = bool(wanted_number) and candidate_number == wanted_number

            if exact_name:
                score += 10
            elif wanted_name in candidate_name or candidate_name in wanted_name:
                score += 3

            if exact_number:
                score += 10

            ranked.append((score, exact_name, exact_number, card))

        ranked.sort(key=lambda x: x[0], reverse=True)
        score, exact_name, exact_number, best = ranked[0]

        if exact_name and exact_number:
            return best, "name_and_number"

        if exact_name:
            return best, "name_only_candidate"

        return None, "no_match"
