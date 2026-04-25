"""Wrapper assincrono para a Gamma API da Polymarket (mercados, eventos).

Read-only e publica - nao requer autenticacao.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from loguru import logger

from bot.clients.models import Market

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"
DEFAULT_TIMEOUT = 15.0
DEFAULT_PAGE_LIMIT = 100
DEFAULT_USER_AGENT = "BotArbitragem/0.1 (+https://github.com/gomesgomes5260-boop/BotArbitragem)"


class GammaClient:
    """Cliente HTTP para a Gamma API.

    Usa context manager assincrono:
        async with GammaClient() as gc:
            markets = await gc.list_active_markets()
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        )

    async def __aenter__(self) -> "GammaClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def list_markets_page(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        archived: bool | None = False,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        order: str = "volume",
        ascending: bool = False,
    ) -> list[Market]:
        """Uma pagina (ate `limit`) de mercados. Use `iter_markets` para todos."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if archived is not None:
            params["archived"] = str(archived).lower()

        raw = await self._get("/markets", params=params)
        if not isinstance(raw, list):
            logger.warning("Gamma /markets retornou shape inesperado: {}", type(raw).__name__)
            return []

        markets: list[Market] = []
        for item in raw:
            try:
                markets.append(Market.model_validate(item))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Falha ao parsear mercado id={}: {}", item.get("id"), exc)
        return markets

    async def iter_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        archived: bool | None = False,
        page_size: int = DEFAULT_PAGE_LIMIT,
        max_pages: int | None = None,
    ) -> AsyncIterator[Market]:
        """Itera por todas as paginas ate esgotar (ou `max_pages`)."""
        offset = 0
        page = 0
        while True:
            batch = await self.list_markets_page(
                active=active,
                closed=closed,
                archived=archived,
                limit=page_size,
                offset=offset,
            )
            if not batch:
                return
            for m in batch:
                yield m
            page += 1
            offset += page_size
            if max_pages is not None and page >= max_pages:
                return
            if len(batch) < page_size:
                return

    async def list_active_binary_markets(
        self,
        *,
        min_volume: float = 0.0,
        max_pages: int = 5,
        page_size: int = DEFAULT_PAGE_LIMIT,
    ) -> list[Market]:
        """Conveniencia: mercados ativos, binarios (YES/NO), com volume minimo."""
        result: list[Market] = []
        async for m in self.iter_markets(
            active=True, closed=False, archived=False, page_size=page_size, max_pages=max_pages
        ):
            if not m.is_binary:
                continue
            if m.best_volume < min_volume:
                continue
            result.append(m)
        return result

    async def get_market(self, condition_id: str) -> Market | None:
        """Busca um mercado pelo conditionId. Retorna None se nao achar."""
        raw = await self._get("/markets", params={"condition_ids": condition_id})
        if isinstance(raw, list) and raw:
            return Market.model_validate(raw[0])
        if isinstance(raw, dict):
            return Market.model_validate(raw)
        return None
