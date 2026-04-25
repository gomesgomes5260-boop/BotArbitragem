"""Tests do MarketCache com GammaClient mockado."""
from __future__ import annotations

import pytest
import httpx

from bot.clients.polymarket_gamma import GammaClient
from bot.data.market_cache import MarketCache


GAMMA_FIXTURE = [
    {
        "id": "1",
        "question": "Will A?",
        "slug": "a",
        "conditionId": "0xa",
        "active": True,
        "volume": 50000,
        "volumeNum": 50000,
        "clobTokenIds": '["t1", "t2"]',
        "outcomes": '["Yes", "No"]',
    },
    {
        "id": "2",
        "question": "Will B?",
        "slug": "b",
        "conditionId": "0xb",
        "active": True,
        "volume": 1000,
        "volumeNum": 1000,
        "clobTokenIds": '["t3", "t4"]',
        "outcomes": '["Yes", "No"]',
    },
]


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path != "/markets":
        return httpx.Response(404)
    if request.url.params.get("offset") in (None, "0"):
        return httpx.Response(200, json=GAMMA_FIXTURE)
    return httpx.Response(200, json=[])


@pytest.mark.asyncio
async def test_cache_refresh_indexes_by_condition_and_token() -> None:
    transport = httpx.MockTransport(_handler)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    gc = GammaClient(client=httpx_client)
    cache = MarketCache(min_volume=0.0, max_pages=1)

    n = await cache.refresh(gc)

    assert n == 2
    assert cache.get_by_condition("0xa") is not None
    assert cache.get_by_condition("0xb") is not None
    assert cache.get_by_token("t1") is not None
    assert cache.get_by_token("t1").condition_id == "0xa"
    assert cache.get_by_token("t4").condition_id == "0xb"
    assert set(cache.all_token_ids) == {"t1", "t2", "t3", "t4"}


@pytest.mark.asyncio
async def test_cache_filter_by_volume() -> None:
    transport = httpx.MockTransport(_handler)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    gc = GammaClient(client=httpx_client)
    cache = MarketCache(min_volume=0.0, max_pages=1)
    await cache.refresh(gc)

    high = cache.filter_by_volume(10000.0)
    assert len(high) == 1
    assert high[0].condition_id == "0xa"


@pytest.mark.asyncio
async def test_cache_min_volume_at_refresh_time() -> None:
    transport = httpx.MockTransport(_handler)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    gc = GammaClient(client=httpx_client)
    cache = MarketCache(min_volume=10000.0, max_pages=1)
    await cache.refresh(gc)

    # so o de vol 50000 deve ter sido carregado
    assert len(cache.markets) == 1
    assert cache.get_by_condition("0xa") is not None
    assert cache.get_by_condition("0xb") is None


@pytest.mark.asyncio
async def test_cache_is_stale() -> None:
    transport = httpx.MockTransport(_handler)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    gc = GammaClient(client=httpx_client)
    cache = MarketCache(min_volume=0.0, max_pages=1)

    assert cache.is_stale(60.0) is True
    await cache.refresh(gc)
    assert cache.is_stale(60.0) is False
    assert cache.is_stale(0.0) is True
