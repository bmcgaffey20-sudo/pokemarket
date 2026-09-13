from typing import Any

import httpx


class TCGdexClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def search_cards(self, query: str) -> list[dict[str, Any]]:
        url = f"{self.base_url}/en/cards"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params={"name": query})
            response.raise_for_status()
            data = response.json()

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("data", data.get("cards", []))
        return []

    async def get_card(self, card_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/en/cards/{card_id}"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()
