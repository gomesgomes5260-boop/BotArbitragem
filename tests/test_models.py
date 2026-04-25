"""Parsing dos modelos Market / OrderBook contra fixtures."""
from __future__ import annotations

from bot.clients.models import BookLevel, Market, OrderBook


GAMMA_MARKET_FIXTURE = {
    "id": "12345",
    "question": "Will Bitcoin be above $100k on Dec 31?",
    "slug": "btc-above-100k-dec-31",
    "conditionId": "0xabcdef0123456789",
    "active": True,
    "closed": False,
    "archived": False,
    "volume": "123456.78",
    "volumeNum": 123456.78,
    "liquidity": "5432.1",
    "liquidityNum": 5432.1,
    "clobTokenIds": '["111111111", "222222222"]',
    "outcomes": '["Yes", "No"]',
    "outcomePrices": '["0.42", "0.58"]',
    "endDate": "2024-12-31T00:00:00Z",
    "startDate": "2024-01-01T00:00:00Z",
    "extra_field_we_dont_care_about": "bla",
}


def test_market_parses_gamma_fixture() -> None:
    m = Market.model_validate(GAMMA_MARKET_FIXTURE)

    assert m.id == "12345"
    assert m.condition_id == "0xabcdef0123456789"
    assert m.is_binary is True
    assert m.clob_token_ids == ["111111111", "222222222"]
    assert m.outcomes == ["Yes", "No"]
    assert m.outcome_prices == [0.42, 0.58]
    assert m.yes_token_id == "111111111"
    assert m.no_token_id == "222222222"
    assert m.best_volume == 123456.78
    assert m.volume == 123456.78


def test_market_handles_missing_optional_fields() -> None:
    minimal = {"id": "1", "question": "Q?", "slug": "q"}
    m = Market.model_validate(minimal)
    assert m.condition_id is None
    assert m.clob_token_ids == []
    assert m.outcomes == []
    assert m.is_binary is False
    assert m.yes_token_id is None
    assert m.best_volume == 0.0


def test_market_handles_garbage_json_strings() -> None:
    bad = {
        "id": "1",
        "question": "Q?",
        "slug": "q",
        "clobTokenIds": "not-valid-json",
        "outcomes": "",
        "outcomePrices": "[abc]",
    }
    m = Market.model_validate(bad)
    assert m.clob_token_ids == []
    assert m.outcomes == []
    assert m.outcome_prices == []


def test_market_volume_string_to_float() -> None:
    m = Market.model_validate({"id": "1", "question": "?", "slug": "?", "volume": "999.99"})
    assert m.volume == 999.99


CLOB_BOOK_FIXTURE = {
    "market": "0xfeed",
    "asset_id": "111111111",
    "timestamp": "1700000000",
    "hash": "0xdead",
    "bids": [
        {"price": "0.40", "size": "100"},
        {"price": "0.42", "size": "200"},
        {"price": "0.41", "size": "50"},
    ],
    "asks": [
        {"price": "0.45", "size": "300"},
        {"price": "0.44", "size": "150"},
    ],
}


def test_orderbook_parses_and_computes_best_levels() -> None:
    book = OrderBook.model_validate(CLOB_BOOK_FIXTURE)

    assert book.asset_id == "111111111"
    assert len(book.bids) == 3
    assert len(book.asks) == 2

    bb = book.best_bid
    assert bb is not None
    assert bb.price == 0.42  # melhor bid = mais alto

    ba = book.best_ask
    assert ba is not None
    assert ba.price == 0.44  # melhor ask = mais baixo

    assert book.midpoint == (0.42 + 0.44) / 2.0


def test_orderbook_empty_levels() -> None:
    book = OrderBook.model_validate({"asset_id": "x", "bids": [], "asks": []})
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.midpoint is None


def test_book_level_string_inputs() -> None:
    lvl = BookLevel.model_validate({"price": "0.5", "size": "100"})
    assert lvl.price == 0.5
    assert lvl.size == 100.0
