"""Tests do GasOracle (stub na Fase 2)."""
from __future__ import annotations

import pytest

from bot.config.settings import Settings
from bot.economics.gas_oracle import GasOracle


@pytest.mark.asyncio
async def test_gas_oracle_returns_configured_value() -> None:
    oracle = GasOracle(gas_per_tx_usd=0.07)
    snap = await oracle.snapshot()
    assert snap.gas_per_tx_usd == 0.07
    assert snap.matic_price_usd is None


@pytest.mark.asyncio
async def test_gas_oracle_caches() -> None:
    oracle = GasOracle(gas_per_tx_usd=0.05, cache_ttl_seconds=60.0)
    snap1 = await oracle.snapshot()
    oracle.set_static(gas_per_tx_usd=0.99)
    # apos set_static cache eh limpo, entao vai pegar o novo valor
    snap2 = await oracle.snapshot()
    assert snap2.gas_per_tx_usd == 0.99
    # mas chamadas seguidas batem cache
    snap3 = await oracle.snapshot()
    assert snap3.timestamp == snap2.timestamp


@pytest.mark.asyncio
async def test_gas_oracle_force_refresh() -> None:
    oracle = GasOracle(gas_per_tx_usd=0.05, cache_ttl_seconds=600.0)
    snap1 = await oracle.snapshot()
    snap2 = await oracle.snapshot(force_refresh=True)
    assert snap2.timestamp >= snap1.timestamp


@pytest.mark.asyncio
async def test_gas_oracle_from_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AVG_GAS_POLYGON_USD", "0.12")
    s = Settings()
    oracle = GasOracle.from_settings(s)
    snap = await oracle.snapshot()
    assert snap.gas_per_tx_usd == 0.12
