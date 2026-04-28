"""Tests do pnl_report."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot.clients.models import BookLevel, Market, OrderBook
from bot.data.opportunity_store import OpportunityRow, OpportunityStore
from bot.economics.cost_model import CostModel, VariableCost
from bot.execution.paper_trader import PaperTrader
from bot.monitoring.pnl_report import _period_to_since, build_report
from bot.strategies.intra_market import ArbOpportunity


def _cm(**overrides) -> CostModel:
    base = dict(
        taker_fee_bps=100,
        maker_fee_bps=0,
        avg_gas_polygon_usd=0.05,
        txs_per_arb=2,
        monthly_rpc_usd=0.0,
        monthly_vps_usd=30.0,  # = 1 USD/dia
        monthly_other_usd=0.0,
    )
    base.update(overrides)
    return CostModel(**base)


def _market() -> Market:
    return Market.model_validate(
        {
            "id": "m1",
            "question": "Q?",
            "slug": "q",
            "conditionId": "0xa",
            "active": True,
            "clobTokenIds": '["y", "n"]',
            "outcomes": '["Yes", "No"]',
        }
    )


def _book(asset: str, asks: list[tuple[float, float]]) -> OrderBook:
    return OrderBook(
        asset_id=asset,
        asks=[BookLevel(price=p, size=s) for p, s in asks],
        bids=[],
    )


def _seed_trade(store: OpportunityStore, cost_model: CostModel) -> None:
    """Helper: insere 1 trade paper de exemplo com gross=$10, fees=$0.90, gas=$0.10."""
    market = _market()
    yb = _book("y", [(0.40, 100)])
    nb = _book("n", [(0.50, 100)])
    opp = ArbOpportunity(
        market=market,
        yes_book=yb,
        no_book=nb,
        size_shares=100.0,
        avg_ask_yes=0.40,
        avg_ask_no=0.50,
        gross_pnl_usdc=10.0,
        variable_cost=VariableCost(fees_usdc=0.90, gas_usdc=0.10),
    )
    trader = PaperTrader(cost_model, store, capital_per_trade_usdc=10_000)
    trader.execute(opp)


def test_period_to_since_supports_days() -> None:
    s = _period_to_since("7d")
    assert s is not None
    delta = datetime.now(timezone.utc) - s
    assert 6.99 < delta.total_seconds() / 86400 < 7.01


def test_period_to_since_all() -> None:
    assert _period_to_since("all") is None


def test_period_to_since_invalid() -> None:
    with pytest.raises(ValueError):
        _period_to_since("7h")


def test_build_report_empty(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "x.db")
    store.init_schema()
    cm = _cm()
    rep = build_report(store, cm, period="all")
    assert rep.num_trades == 0
    assert rep.gross_pnl_usdc == 0.0
    assert rep.fixed_costs_usd >= 0.0


def test_build_report_with_one_trade(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "x.db")
    store.init_schema()
    cm = _cm()
    _seed_trade(store, cm)

    rep = build_report(store, cm, period="all", mode="paper")

    assert rep.num_trades == 1
    assert abs(rep.gross_pnl_usdc - 10.0) < 1e-9
    assert abs(rep.fees_usdc - 0.90) < 1e-9
    assert abs(rep.gas_usdc - 0.10) < 1e-9
    assert abs(rep.net_pnl_usdc - 9.0) < 1e-9
    assert rep.wins == 1
    assert rep.full_fills == 1
    assert rep.win_rate == 100.0
    assert rep.fill_rate == 100.0


def test_build_report_filter_by_mode(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "x.db")
    store.init_schema()
    cm = _cm()
    _seed_trade(store, cm)
    rep_live = build_report(store, cm, period="all", mode="live")
    rep_paper = build_report(store, cm, period="all", mode="paper")
    assert rep_live.num_trades == 0
    assert rep_paper.num_trades == 1


def test_build_report_filter_by_period(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "x.db")
    store.init_schema()
    cm = _cm()
    # Insere trade com timestamp antigo (10 dias atras)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    market = _market()
    yb = _book("y", [(0.40, 100)])
    nb = _book("n", [(0.50, 100)])
    opp = ArbOpportunity(
        market=market,
        yes_book=yb,
        no_book=nb,
        size_shares=100.0,
        avg_ask_yes=0.40,
        avg_ask_no=0.50,
        gross_pnl_usdc=10.0,
        variable_cost=VariableCost(fees_usdc=0.90, gas_usdc=0.10),
    )
    trader = PaperTrader(cm, store, capital_per_trade_usdc=10_000)
    # Hack: insere via paper trader (timestamp = agora) e depois insere outro com ts antigo manualmente
    trader.execute(opp)
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO trades (
                opportunity_id, timestamp_utc, mode, market_id, size_usdc,
                fill_price_yes, fill_price_no, realized_gross_pnl_usdc,
                taker_fee_paid_usdc, maker_fee_paid_usdc, gas_paid_usdc,
                matic_price_at_trade, net_pnl_usdc, leg_status
            ) VALUES (?, ?, 'paper', 'm1', 90.0, 0.40, 0.50, 10.0,
                      0.90, 0, 0.10, NULL, 9.0, 'both_filled')
            """,
            (None, old_ts),
        )

    # Periodo 7d -> so o trade recente
    rep_recent = build_report(store, cm, period="7d", mode="paper")
    assert rep_recent.num_trades == 1
    rep_all = build_report(store, cm, period="all", mode="paper")
    assert rep_all.num_trades == 2


def test_build_report_pays_infra(tmp_path: Path) -> None:
    store = OpportunityStore(tmp_path / "x.db")
    store.init_schema()
    cm = _cm()  # 1 USD/dia de custo fixo
    _seed_trade(store, cm)

    # 1 trade com net=9, periodo "all" -> dias com base no ts (1 dia min)
    rep = build_report(store, cm, period="all", mode="paper")
    # custo fixo >= 1 USD ; net=9 -> paga
    assert rep.pays_infra is True
