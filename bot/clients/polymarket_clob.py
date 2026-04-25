"""Wrapper assincrono para o CLOB REST da Polymarket (order books).

Endpoints publicos (book, midpoint) nao requerem autenticacao - so leitura.
Endpoints privados (criar ordem, cancelar) virao na Fase 5.
"""
from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from bot.clients.models import OrderBook

DEFAULT_BASE_URL = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 15.0
DEFAULT_USER_AGENT = "BotArbitragem/0.1 (+https://github.com/gomesgomes5260-boop/BotArbitragem)"


class ClobClient:
    """Cliente HTTP read-only para o CLOB.

    Uso:
        async with ClobClient() as cc:
            book = await cc.get_book(token_id)
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        )

    async def __aenter__(self) -> "ClobClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_book(self, token_id: str) -> OrderBook:
        """GET /book?token_id=... -> OrderBook do outcome especifico."""
        resp = await self._client.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        data = resp.json()
        return OrderBook.model_validate(data)

    async def get_books(self, token_ids: list[str]) -> list[OrderBook]:
        """POST /books com [{token_id}, ...] -> books em batch.

        Util pra puxar YES e NO de um mercado em uma chamada so.
        """
        if not token_ids:
            return []
        payload = [{"token_id": tid} for tid in token_ids]
        resp = await self._client.post("/books", json=payload)
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            logger.warning("CLOB /books retornou shape inesperado: {}", type(raw).__name__)
            return []
        out: list[OrderBook] = []
        for item in raw:
            try:
                out.append(OrderBook.model_validate(item))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Falha ao parsear book: {}", exc)
        return out

    async def get_midpoint(self, token_id: str) -> float | None:
        """GET /midpoint?token_id=... -> preco do midpoint."""
        resp = await self._client.get("/midpoint", params={"token_id": token_id})
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        mid = data.get("mid")
        if mid is None:
            return None
        try:
            return float(mid)
        except (TypeError, ValueError):
            return None
