"""Tests do OpportunityStore (SQLite)."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.data.opportunity_store import OpportunityRow, OpportunityStore


@pytest.fixture
def store(tmp_path: Path) -> OpportunityStore:
    s = OpportunityStore(tmp_path / "test.db")
    s.init_schema()
    return s


def _row(**overrides) -> OpportunityRow:
    base = dict(
        market_id="m1",
        condition_id="0xabc",
        market_question="Q?",
        ask_yes=0.45,
        ask_no=0.50,
        size_max_usdc=100.0,
        size_max_shares=105.0,
        gross_pnl_usdc=5.25,
        estimated_fees_usdc=0.10,
        estimated_gas_usdc=0.10,
    )
    base.update(overrides)
    return OpportunityRow(**base)


def test_init_schema_idempotent(tmp_path: Path) -> None:
    s = OpportunityStore(tmp_path / "x.db")
    s.init_schema()
    s.init_schema()  # nao quebra
    assert s.count_opportunities() == 0


def test_insert_opportunity(store: OpportunityStore) -> None:
    row = _row()
    rid = store.insert_opportunity(row)
    assert rid > 0
    assert store.count_opportunities() == 1


def test_opportunity_row_derived_fields() -> None:
    row = _row(gross_pnl_usdc=2.0, estimated_fees_usdc=0.5, estimated_gas_usdc=0.1)
    assert abs(row.net_pnl_usdc - 1.4) < 1e-9
    assert row.sum_asks == 0.95
    assert abs(row.net_pnl_pct - 1.4) < 1e-9  # 1.4 / 100 * 100


def test_aggregate_pnl(store: OpportunityStore) -> None:
    store.insert_opportunity(_row(gross_pnl_usdc=2.0, estimated_fees_usdc=0.1, estimated_gas_usdc=0.1))
    store.insert_opportunity(_row(gross_pnl_usdc=3.0, estimated_fees_usdc=0.2, estimated_gas_usdc=0.1))

    agg = store.aggregate_pnl()
    assert agg["num_opportunities"] == 2
    assert abs(agg["gross_pnl_usdc"] - 5.0) < 1e-9
    assert abs(agg["estimated_fees_usdc"] - 0.3) < 1e-9
    assert abs(agg["estimated_gas_usdc"] - 0.2) < 1e-9
    assert abs(agg["net_pnl_usdc"] - (5.0 - 0.3 - 0.2)) < 1e-9


def test_recent_opportunities_order(store: OpportunityStore) -> None:
    store.insert_opportunity(_row(market_id="a"))
    store.insert_opportunity(_row(market_id="b"))
    store.insert_opportunity(_row(market_id="c"))

    rows = store.recent_opportunities(limit=2)
    assert len(rows) == 2
    assert rows[0]["market_id"] == "c"
    assert rows[1]["market_id"] == "b"


def test_daily_costs_upsert(store: OpportunityStore) -> None:
    store.upsert_daily_cost("2026-04-27", rpc_cost_usd=1.0, vps_cost_usd=0.33, other_fixed_usd=0.0)
    store.upsert_daily_cost("2026-04-27", rpc_cost_usd=2.0, vps_cost_usd=0.33, other_fixed_usd=0.5)

    with store._connect() as conn:
        cur = conn.execute("SELECT * FROM daily_costs WHERE date = ?", ("2026-04-27",))
        row = cur.fetchone()
        assert row["rpc_cost_usd"] == 2.0
        assert abs(row["total_fixed_usd"] - 2.83) < 1e-9


def test_matic_price_history_upsert(store: OpportunityStore) -> None:
    store.upsert_matic_price("2026-04-27", 0.50)
    store.upsert_matic_price("2026-04-27", 0.55)

    with store._connect() as conn:
        cur = conn.execute("SELECT * FROM matic_price_history")
        rows = list(cur.fetchall())
        assert len(rows) == 1
        assert rows[0]["price_usd"] == 0.55
