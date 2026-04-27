"""Oraculo de gas Polygon + cotacao MATIC/USD.

Implementacao na Fase 2: stub que retorna valores configurados via
settings. Estrutura ja preparada pra plugar fontes reais (CoinGecko free
API, Chainlink on-chain, etc.) na Fase 4/5 sem mudar callers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from bot.config.settings import Settings


@dataclass
class GasSnapshot:
    """Custo estimado de uma transacao Polygon em USD."""

    gas_per_tx_usd: float
    matic_price_usd: float | None  # None = nao disponivel
    timestamp: float


class GasOracle:
    """Oraculo basico. Cache simples de N segundos.

    Uso:
        oracle = GasOracle.from_settings(settings)
        snap = await oracle.snapshot()
    """

    def __init__(
        self,
        *,
        gas_per_tx_usd: float,
        matic_price_usd: float | None = None,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._configured_gas = gas_per_tx_usd
        self._configured_matic = matic_price_usd
        self._cache_ttl = cache_ttl_seconds
        self._cached: GasSnapshot | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "GasOracle":
        return cls(gas_per_tx_usd=settings.avg_gas_polygon_usd)

    async def snapshot(self, *, force_refresh: bool = False) -> GasSnapshot:
        """Retorna snapshot atual (cacheado).

        Hoje retorna o valor configurado. Quando plugarmos fonte real
        (CoinGecko/Chainlink), so essa funcao muda.
        """
        now = time.time()
        if (
            not force_refresh
            and self._cached is not None
            and (now - self._cached.timestamp) < self._cache_ttl
        ):
            return self._cached

        snap = GasSnapshot(
            gas_per_tx_usd=self._configured_gas,
            matic_price_usd=self._configured_matic,
            timestamp=now,
        )
        self._cached = snap
        return snap

    def set_static(
        self,
        *,
        gas_per_tx_usd: float | None = None,
        matic_price_usd: float | None = None,
    ) -> None:
        """Util pra testes ou ajustes manuais sem reiniciar."""
        if gas_per_tx_usd is not None:
            self._configured_gas = gas_per_tx_usd
        if matic_price_usd is not None:
            self._configured_matic = matic_price_usd
        self._cached = None
