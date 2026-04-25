"""Cache em memoria de mercados ativos da Polymarket.

Refresca periodicamente via Gamma API. O detector da Fase 2 vai consumir
isto pra saber quais token_ids monitorar no WS.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from loguru import logger

from bot.clients.models import Market
from bot.clients.polymarket_gamma import GammaClient


@dataclass
class MarketCache:
    """Cache de mercados binarios ativos.

    Nao se auto-refresca - o caller controla via `refresh()`. Mantem assim
    pra deixar o ciclo de vida explicito (sem threads/tasks ocultos).
    """

    min_volume: float = 0.0
    max_pages: int = 10
    page_size: int = 100

    _markets_by_condition: dict[str, Market] = field(default_factory=dict)
    _markets_by_token: dict[str, Market] = field(default_factory=dict)
    _last_refresh_ts: float = 0.0

    async def refresh(self, gamma: GammaClient) -> int:
        """Atualiza cache. Retorna numero de mercados carregados."""
        markets = await gamma.list_active_binary_markets(
            min_volume=self.min_volume,
            max_pages=self.max_pages,
            page_size=self.page_size,
        )
        by_condition: dict[str, Market] = {}
        by_token: dict[str, Market] = {}
        for m in markets:
            if m.condition_id:
                by_condition[m.condition_id] = m
            for tid in m.clob_token_ids:
                by_token[tid] = m
        self._markets_by_condition = by_condition
        self._markets_by_token = by_token
        self._last_refresh_ts = time.time()
        logger.info(
            "MarketCache atualizado: {} mercados binarios, vol >= {}", len(markets), self.min_volume
        )
        return len(markets)

    def is_stale(self, ttl_seconds: float) -> bool:
        return (time.time() - self._last_refresh_ts) > ttl_seconds

    @property
    def markets(self) -> list[Market]:
        return list(self._markets_by_condition.values())

    @property
    def all_token_ids(self) -> list[str]:
        return list(self._markets_by_token.keys())

    @property
    def last_refresh_ts(self) -> float:
        return self._last_refresh_ts

    def get_by_condition(self, condition_id: str) -> Market | None:
        return self._markets_by_condition.get(condition_id)

    def get_by_token(self, token_id: str) -> Market | None:
        return self._markets_by_token.get(token_id)

    def filter_by_volume(self, min_volume: float) -> list[Market]:
        return [m for m in self.markets if m.best_volume >= min_volume]
