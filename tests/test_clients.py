"""Testa GammaClient e ClobClient com httpx mockado (sem rede real)."""
from __future__ import annotations

import pytest
import httpx

from bot.clients.polymarket_clob import ClobClient
from bot.clients.polymarket_gamma import GammaClient


GAMMA_MARKETS_RESPONSE = [
    {
        "id": "1",
        "question": "Will A?",
        "slug": "a",
        "conditionId": "0xa",
        "active": True,
        "closed": False,
        "archived": False,
        "volume": "20000",
        "volumeNum": 20000,
        "clobTokenIds": '["t1", "t2"]',
        "outcomes": '["Yes", "No"]',
    },
    {
        "id": "2",
        "question": "Will B?",
        "slug": "b",
        "conditionId": "0xb",
        "active": True,
        "closed": False,
        "archived": False,
        "volume": "5000",
        "volumeNum": 5000,
        "clobTokenIds": '["t3", "t4"]',
        "outcomes": '["Yes", "No"]',
    },
    {
        # multi-outcome - deve ser filtrado de active_binary
        "id": "3",
        "question": "Three-way?",
        "slug": "c",
        "conditionId": "0xc",
        "active": True,
        "volume": 100000,
        "volumeNum": 100000,
        "clobTokenIds": '["t5", "t6", "t7"]',
        "outcomes": '["X", "Y", "Z"]',
    },
]


def _gamma_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/markets":
        if request.url.params.get("offset") in (None, "0"):
            return httpx.Response(200, json=GAMMA_MARKETS_RESPONSE)
        return httpx.Response(200, json=[])
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_gamma_list_markets_page_parses() -> None:
    transport = httpx.MockTransport(_gamma_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    gc = GammaClient(client=client)

    markets = await gc.list_markets_page()

    assert len(markets) == 3
    assert markets[0].condition_id == "0xa"
    assert markets[2].is_binary is False


@pytest.mark.asyncio
async def test_gamma_active_binary_filters() -> None:
    transport = httpx.MockTransport(_gamma_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    gc = GammaClient(client=client)

    markets = await gc.list_active_binary_markets(min_volume=10000.0, max_pages=1)

    # multi-outcome (id=3) e binario com vol baixo (id=2) excluidos
    assert len(markets) == 1
    assert markets[0].id == "1"


CLOB_BOOK_RESPONSE = {
    "asset_id": "tok-yes",
    "bids": [{"price": "0.40", "size": "100"}],
    "asks": [{"price": "0.45", "size": "200"}],
}

CLOB_BOOKS_RESPONSE = [
    CLOB_BOOK_RESPONSE,
    {
        "asset_id": "tok-no",
        "bids": [{"price": "0.50", "size": "100"}],
        "asks": [{"price": "0.55", "size": "200"}],
    },
]


def _clob_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/book" and request.method == "GET":
        return httpx.Response(200, json=CLOB_BOOK_RESPONSE)
    if request.url.path == "/books" and request.method == "POST":
        return httpx.Response(200, json=CLOB_BOOKS_RESPONSE)
    if request.url.path == "/midpoint":
        return httpx.Response(200, json={"mid": "0.425"})
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_clob_get_book() -> None:
    transport = httpx.MockTransport(_clob_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    cc = ClobClient(client=client)

    book = await cc.get_book("tok-yes")

    assert book.asset_id == "tok-yes"
    assert book.best_ask is not None
    assert book.best_ask.price == 0.45
    assert book.best_bid is not None
    assert book.best_bid.price == 0.40


@pytest.mark.asyncio
async def test_clob_get_books_batch() -> None:
    transport = httpx.MockTransport(_clob_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    cc = ClobClient(client=client)

    books = await cc.get_books(["tok-yes", "tok-no"])

    assert len(books) == 2
    assert books[1].asset_id == "tok-no"
    assert books[1].best_bid is not None
    assert books[1].best_bid.price == 0.50


@pytest.mark.asyncio
async def test_clob_get_midpoint() -> None:
    transport = httpx.MockTransport(_clob_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    cc = ClobClient(client=client)

    mid = await cc.get_midpoint("tok-yes")

    assert mid == 0.425


@pytest.mark.asyncio
async def test_clob_get_books_empty_list() -> None:
    transport = httpx.MockTransport(_clob_handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://mock")
    cc = ClobClient(client=client)

    assert await cc.get_books([]) == []
