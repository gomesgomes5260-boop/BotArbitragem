"""Tests do IntraMarketScanner com Gamma e CLOB mockados."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from bot.clients.polymarket_clob import ClobClient
from bot.clients.polymarket_gamma import GammaClient
from bot.data.opportunity_store import OpportunityStore
from bot.economics.cost_model import CostModel
from bot.strategies.intra_market import IntraMarketDetector
from bot.strategies.scanner import IntraMarketScanner


GAMMA_FIXTURE = [
    {
        "id": "1",
        "question": "Arb obvia",
        "slug": "a",
        "conditionId": "0xa",
        "active": True,
        "volumeNum": 50000,
        "clobTokenIds": '["yes1", "no1"]',
        "outcomes": '["Yes", "No"]',
    },
    {
        "id": "2",
        "question": "Sem arb",
        "slug": "b",
        "conditionId": "0xb",
        "active": True,
        "volumeNum": 50000,
        "clobTokenIds": '["yes2", "no2"]',
        "outcomes": '["Yes", "No"]',
    },
]

BOOKS_FIXTURE = [
    {  # arb: 0.40 + 0.50 = 0.90
        "asset_id": "yes1",
        "asks": [{"price": "0.40", "size": "100"}],
        "bids": [],
    },
    {
        "asset_id": "no1",
        "asks": [{"price": "0.50", "size": "100"}],
        "bids": [],
    },
    {  # sem arb: 0.55 + 0.50 = 1.05
        "asset_id": "yes2",
        "asks": [{"price": "0.55", "size": "100"}],
        "bids": [],
    },
    {
        "asset_id": "no2",
        "asks": [{"price": "0.50", "size": "100"}],
        "bids": [],
    },
]


def _gamma_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/markets":
        if req.url.params.get("offset") in (None, "0"):
            return httpx.Response(200, json=GAMMA_FIXTURE)
        return httpx.Response(200, json=[])
    return httpx.Response(404)


def _clob_handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/books" and req.method == "POST":
        return httpx.Response(200, json=BOOKS_FIXTURE)
    return httpx.Response(404)


def _zero_cost_model() -> CostModel:
    return CostModel(
        taker_fee_bps=0,
        maker_fee_bps=0,
        avg_gas_polygon_usd=0.0,
        txs_per_arb=2,
        monthly_rpc_usd=0.0,
        monthly_vps_usd=0.0,
        monthly_other_usd=0.0,
    )


@pytest.mark.asyncio
async def test_scanner_finds_arb_and_persists(tmp_path: Path) -> None:
    gamma_client = httpx.AsyncClient(transport=httpx.MockTransport(_gamma_handler), base_url="http://mock-gamma")
    clob_client = httpx.AsyncClient(transport=httpx.MockTransport(_clob_handler), base_url="http://mock-clob")
    gamma = GammaClient(client=gamma_client)
    clob = ClobClient(client=clob_client)
    detector = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)
    store = OpportunityStore(tmp_path / "scan.db")
    store.init_schema()

    scanner = IntraMarketScanner(gamma, clob, detector, store)
    await scanner.refresh_cache(min_volume=0.0, max_pages=1)
    result = await scanner.scan_once()

    assert result.markets_scanned == 2
    assert result.opportunities_found == 1
    assert result.opportunities[0].market.condition_id == "0xa"

    assert store.count_opportunities() == 1


@pytest.mark.asyncio
async def test_scanner_no_markets() -> None:
    def empty_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/markets":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    gamma = GammaClient(client=httpx.AsyncClient(transport=httpx.MockTransport(empty_handler), base_url="http://mock"))
    clob = ClobClient(client=httpx.AsyncClient(transport=httpx.MockTransport(_clob_handler), base_url="http://mock"))
    detector = IntraMarketDetector(_zero_cost_model())

    scanner = IntraMarketScanner(gamma, clob, detector, store=None)
    await scanner.refresh_cache(min_volume=0.0, max_pages=1)
    result = await scanner.scan_once()

    assert result.markets_scanned == 0
    assert result.opportunities_found == 0
