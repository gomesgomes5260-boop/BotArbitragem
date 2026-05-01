"""Testes do NotionReporter: idempotencia via cursor + render do dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bot.data.opportunity_store import OpportunityStore, OpportunityRow
from bot.economics.cost_model import CostModel
from bot.monitoring.notion_reporter import (
    NotionReporter,
    build_dashboard_blocks,
    opp_row_to_properties,
    trade_row_to_properties,
)


@pytest.fixture
def store(tmp_path: Path) -> OpportunityStore:
    s = OpportunityStore(tmp_path / "test.db")
    s.init_schema()
    return s


@pytest.fixture
def cost_model() -> CostModel:
    return CostModel(
        taker_fee_bps=20,
        maker_fee_bps=0,
        avg_gas_polygon_usd=0.05,
        txs_per_arb=2,
        monthly_rpc_usd=0,
        monthly_vps_usd=10,
        monthly_other_usd=0,
    )


def _insert_opp(store: OpportunityStore, **kw) -> int:
    defaults = dict(
        market_id="m1",
        condition_id="c1",
        market_question="Will X happen?",
        ask_yes=0.45,
        ask_no=0.50,
        size_max_usdc=100.0,
        size_max_shares=200.0,
        gross_pnl_usdc=10.0,
        estimated_fees_usdc=0.5,
        estimated_gas_usdc=0.10,
    )
    defaults.update(kw)
    return store.insert_opportunity(OpportunityRow(**defaults))


def _insert_trade(
    store: OpportunityStore,
    *,
    opp_id: int | None,
    mode: str = "paper",
    size: float = 100.0,
    gross: float = 1.5,
    net: float = 1.0,
    leg: str = "both_filled",
    market_id: str = "m1",
) -> int:
    """Insere trade direto via SQL (bypassando o helper que precisa de PaperTradeResult)."""
    import sqlite3

    ts = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(store._db_path)) as conn:  # type: ignore[attr-defined]
        cur = conn.execute(
            """
            INSERT INTO trades (
                opportunity_id, timestamp_utc, mode, market_id, size_usdc,
                fill_price_yes, fill_price_no, realized_gross_pnl_usdc,
                taker_fee_paid_usdc, maker_fee_paid_usdc, gas_paid_usdc,
                matic_price_at_trade, net_pnl_usdc, leg_status
            ) VALUES (?, ?, ?, ?, ?, 0.45, 0.50, ?, 0.4, 0.0, 0.1, 0.5, ?, ?)
            """,
            (opp_id, ts, mode, market_id, size, gross, net, leg),
        )
        conn.commit()
        return int(cur.lastrowid or 0)


def test_trade_row_to_properties_maps_all_fields(store: OpportunityStore) -> None:
    opp_id = _insert_opp(store)
    _insert_trade(store, opp_id=opp_id)
    rows = store.trades_after_id(0, limit=10)
    assert len(rows) == 1
    props = trade_row_to_properties(rows[0], question="Will X happen?")
    assert "Name" in props and "title" in props["Name"]
    assert props["Mode"]["select"]["name"] == "paper"
    assert props["Size USDC"]["number"] == 100.0
    assert props["Status"]["select"]["name"] == "both_filled"
    assert props["Fill YES"]["number"] == 0.45
    # Fees soma taker + maker
    assert props["Fees"]["number"] == pytest.approx(0.4)


def test_opp_row_to_properties_maps_all_fields(store: OpportunityStore) -> None:
    _insert_opp(store)
    rows = store.opportunities_after_id(0, limit=10)
    props = opp_row_to_properties(rows[0])
    assert props["Was Traded"]["checkbox"] is False
    assert props["Sum Asks"]["number"] == pytest.approx(0.95)
    assert props["Net PnL"]["number"] == pytest.approx(9.4)


def test_cursor_advances_only_on_success(
    store: OpportunityStore, cost_model: CostModel
) -> None:
    _insert_opp(store)
    _insert_opp(store)
    _insert_opp(store)
    assert store.get_notion_sync_cursor("opportunities") == 0

    client = AsyncMock()
    client.create_database_page = AsyncMock(return_value={"id": "p1"})
    client.replace_page_content = AsyncMock(return_value=None)

    reporter = NotionReporter(
        store, cost_model, client,
        dashboard_page_id="dash",
        trades_db_id="tdb",
        opportunities_db_id="odb",
        max_rows_per_run=10,
    )

    import asyncio
    synced, errors = asyncio.run(reporter.sync_opportunities())
    assert synced == 3
    assert not errors
    assert store.get_notion_sync_cursor("opportunities") == 3
    assert client.create_database_page.await_count == 3

    # Segunda chamada nao deve enviar nada novo
    client.create_database_page.reset_mock()
    synced2, _ = asyncio.run(reporter.sync_opportunities())
    assert synced2 == 0
    assert client.create_database_page.await_count == 0


def test_cursor_does_not_advance_past_failure(
    store: OpportunityStore, cost_model: CostModel
) -> None:
    _insert_opp(store)
    _insert_opp(store)
    _insert_opp(store)

    from bot.monitoring.notion_client import NotionAPIError

    call_count = {"n": 0}

    async def flaky_create(db_id, props, **kw):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise NotionAPIError(400, "boom")
        return {"id": f"p{call_count['n']}"}

    client = AsyncMock()
    client.create_database_page = AsyncMock(side_effect=flaky_create)

    reporter = NotionReporter(
        store, cost_model, client,
        dashboard_page_id="dash",
        trades_db_id="tdb",
        opportunities_db_id="odb",
    )
    import asyncio
    synced, errors = asyncio.run(reporter.sync_opportunities())
    assert synced == 1
    assert len(errors) == 1
    # Cursor avancou so ate o id 1 (primeiro sucesso)
    assert store.get_notion_sync_cursor("opportunities") == 1


def test_dashboard_blocks_render_without_data(
    store: OpportunityStore, cost_model: CostModel
) -> None:
    """Banco vazio: dashboard ainda renderiza (so callout informativo)."""
    blocks = build_dashboard_blocks(store, cost_model)
    assert any(b["type"] == "heading_1" for b in blocks)
    types = [b["type"] for b in blocks]
    assert "callout" in types  # mostra callout pedindo trades
    assert "divider" in types


def test_dashboard_blocks_with_trades(
    store: OpportunityStore, cost_model: CostModel
) -> None:
    opp_id = _insert_opp(store)
    _insert_trade(store, opp_id=opp_id, mode="paper", net=2.0)
    _insert_trade(store, opp_id=opp_id, mode="paper", net=-0.5, leg="partial_yes")
    blocks = build_dashboard_blocks(store, cost_model)
    # Deve ter pelo menos uma table com periodo
    tables = [b for b in blocks if b["type"] == "table"]
    assert len(tables) >= 1
    # Headings principais existem
    headings = [
        b["heading_2"]["rich_text"][0]["text"]["content"]
        for b in blocks
        if b["type"] == "heading_2"
    ]
    assert any("Profit por periodo" in h for h in headings)
    assert any("Top mercados" in h for h in headings)


def test_sync_once_calls_all_three_steps(
    store: OpportunityStore, cost_model: CostModel
) -> None:
    _insert_opp(store)
    _insert_trade(store, opp_id=1)

    client = AsyncMock()
    client.create_database_page = AsyncMock(return_value={"id": "x"})
    client.replace_page_content = AsyncMock(return_value=None)

    reporter = NotionReporter(
        store, cost_model, client,
        dashboard_page_id="dash",
        trades_db_id="tdb",
        opportunities_db_id="odb",
    )

    import asyncio
    res = asyncio.run(reporter.sync_once())
    assert res.trades_synced == 1
    assert res.opportunities_synced == 1
    assert res.dashboard_updated is True
    assert client.replace_page_content.await_count == 1


def test_top_markets_and_daily_series(
    store: OpportunityStore, cost_model: CostModel
) -> None:
    o1 = _insert_opp(store, market_id="m1", market_question="Q1")
    o2 = _insert_opp(store, market_id="m2", market_question="Q2")
    _insert_trade(store, opp_id=o1, net=5.0, market_id="m1")
    _insert_trade(store, opp_id=o1, net=2.0, market_id="m1")
    _insert_trade(store, opp_id=o2, net=1.0, market_id="m2")

    top = store.top_markets_by_pnl(days=30, limit=5)
    assert top[0]["market_id"] == "m1"
    assert top[0]["net_pnl"] == pytest.approx(7.0)

    daily = store.daily_pnl_series(days=30)
    assert len(daily) >= 1
    total = sum(d["net_pnl"] for d in daily)
    assert total == pytest.approx(8.0)


def test_notion_configured_property() -> None:
    from bot.config.settings import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.notion_configured is False

    s2 = Settings(  # type: ignore[call-arg]
        _env_file=None,
        notion_token="ntn_x",
        notion_dashboard_page_id="dash",
        notion_trades_db_id="t",
        notion_opportunities_db_id="o",
    )
    assert s2.notion_configured is True
