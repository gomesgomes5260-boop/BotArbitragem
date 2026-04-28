"""Tests do LiveTrader com LiveClient mockado."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bot.clients.models import BookLevel, Market, OrderBook
from bot.data.opportunity_store import OpportunityRow, OpportunityStore
from bot.economics.cost_model import CostModel, VariableCost
from bot.execution.live_client import OrderResponse
from bot.execution.live_trader import LiveTrader
from bot.execution.paper_trader import (
    LEG_STATUS_BOTH_FILLED,
    LEG_STATUS_FAILED,
    LEG_STATUS_PARTIAL_NO,
)
from bot.risk.kill_switch import KillSwitch
from bot.risk.limits import RiskLimits, RiskManager
from bot.strategies.intra_market import ArbOpportunity


@dataclass
class FakeLiveClient:
    """Cliente fake configuravel pra testes - record-and-replay."""

    balance: float = 100.0
    buy_responses: dict[str, OrderResponse] = field(default_factory=dict)
    sell_responses: dict[str, OrderResponse] = field(default_factory=dict)
    submitted: list[tuple[str, str, float, float]] = field(default_factory=list)

    async def submit_buy_ioc(
        self, token_id: str, limit_price: float, size_shares: float
    ) -> OrderResponse:
        self.submitted.append(("BUY", token_id, limit_price, size_shares))
        return self.buy_responses.get(
            token_id,
            OrderResponse(
                success=True,
                order_id=f"buy-{token_id}",
                filled_shares=size_shares,
                avg_fill_price=limit_price,
                fees_paid_usdc=0.0,
            ),
        )

    async def submit_sell_ioc(
        self, token_id: str, limit_price: float, size_shares: float
    ) -> OrderResponse:
        self.submitted.append(("SELL", token_id, limit_price, size_shares))
        return self.sell_responses.get(
            token_id,
            OrderResponse(
                success=True,
                order_id=f"sell-{token_id}",
                filled_shares=size_shares,
                avg_fill_price=limit_price,
                fees_paid_usdc=0.0,
            ),
        )

    async def get_usdc_balance(self) -> float:
        return self.balance


def _market() -> Market:
    return Market.model_validate(
        {
            "id": "m1",
            "question": "Q?",
            "slug": "q",
            "conditionId": "0xa",
            "active": True,
            "clobTokenIds": '["yt", "nt"]',
            "outcomes": '["Yes", "No"]',
        }
    )


def _make_opp(yes_asks=None, no_asks=None, yes_bids=None, no_bids=None, size_shares=20.0):
    market = _market()
    yes_book = OrderBook(
        asset_id="yt",
        asks=[BookLevel(price=p, size=s) for p, s in (yes_asks or [(0.40, 100)])],
        bids=[BookLevel(price=p, size=s) for p, s in (yes_bids or [(0.38, 100)])],
    )
    no_book = OrderBook(
        asset_id="nt",
        asks=[BookLevel(price=p, size=s) for p, s in (no_asks or [(0.50, 100)])],
        bids=[BookLevel(price=p, size=s) for p, s in (no_bids or [(0.48, 100)])],
    )
    return ArbOpportunity(
        market=market,
        yes_book=yes_book,
        no_book=no_book,
        size_shares=size_shares,
        avg_ask_yes=0.40,
        avg_ask_no=0.50,
        gross_pnl_usdc=2.0,
        variable_cost=VariableCost(fees_usdc=0.0, gas_usdc=0.0),
    )


def _real_cm() -> CostModel:
    return CostModel(
        taker_fee_bps=100,
        maker_fee_bps=0,
        avg_gas_polygon_usd=0.05,
        txs_per_arb=2,
        monthly_rpc_usd=0.0,
        monthly_vps_usd=0.0,
        monthly_other_usd=0.0,
    )


def _trader(
    tmp_path: Path,
    *,
    client: FakeLiveClient,
    capital: float = 10.0,
    limits: RiskLimits | None = None,
) -> tuple[LiveTrader, OpportunityStore, RiskManager]:
    store = OpportunityStore(tmp_path / "live.db")
    store.init_schema()
    limits = limits or RiskLimits(
        max_capital_per_trade_usdc=20.0,
        max_open_exposure_usdc=100.0,
        max_daily_loss_usdc=50.0,
        min_usdc_balance=10.0,
    )
    rm = RiskManager(limits, KillSwitch(tmp_path / "KILL"), hard_cap_per_trade_usdc=50.0)
    trader = LiveTrader(client, _real_cm(), store, rm, capital_per_trade_usdc=capital)
    return trader, store, rm


@pytest.mark.asyncio
async def test_live_executes_when_risk_ok(tmp_path: Path) -> None:
    client = FakeLiveClient()
    trader, store, _rm = _trader(tmp_path, client=client, capital=10.0)
    opp = _make_opp()

    outcome = await trader.execute(opp)

    assert outcome.submitted is True
    assert outcome.risk_reason is None
    assert outcome.trade is not None
    assert outcome.trade.leg_status == LEG_STATUS_BOTH_FILLED
    # 2 ordens submetidas (YES + NO), nenhuma residual
    sides = [s[0] for s in client.submitted]
    assert sides == ["BUY", "BUY"]
    assert store.count_trades(mode="live") == 1


@pytest.mark.asyncio
async def test_live_rejects_when_kill_switch_active(tmp_path: Path) -> None:
    client = FakeLiveClient()
    trader, _store, rm = _trader(tmp_path, client=client, capital=10.0)
    rm.kill_switch.trigger("manual")

    outcome = await trader.execute(_make_opp())

    assert outcome.submitted is False
    assert outcome.risk_reason == "kill_switch_active"
    assert outcome.trade is None
    assert client.submitted == []


@pytest.mark.asyncio
async def test_live_rejects_when_balance_below_min(tmp_path: Path) -> None:
    client = FakeLiveClient(balance=5.0)
    trader, _store, _rm = _trader(tmp_path, client=client, capital=10.0)

    outcome = await trader.execute(_make_opp())

    assert outcome.submitted is False
    assert "balance_too_low" in (outcome.risk_reason or "")
    assert client.submitted == []


@pytest.mark.asyncio
async def test_live_rejects_when_per_trade_cap_breached(tmp_path: Path) -> None:
    client = FakeLiveClient()
    limits = RiskLimits(
        max_capital_per_trade_usdc=2.0,   # baixissimo
        max_open_exposure_usdc=100.0,
        max_daily_loss_usdc=50.0,
        min_usdc_balance=10.0,
    )
    trader, _store, _rm = _trader(tmp_path, client=client, capital=10.0, limits=limits)

    outcome = await trader.execute(_make_opp())

    assert outcome.submitted is False
    assert "per_trade_cap" in (outcome.risk_reason or "")


@pytest.mark.asyncio
async def test_live_partial_no_triggers_residual_close(tmp_path: Path) -> None:
    client = FakeLiveClient()
    # Forca NO a preencher so 50% (10 shares ao inves de 20)
    client.buy_responses["nt"] = OrderResponse(
        success=True,
        order_id="no-partial",
        filled_shares=10.0,
        avg_fill_price=0.50,
        fees_paid_usdc=0.05,
    )
    trader, store, _rm = _trader(tmp_path, client=client, capital=20.0)
    opp = _make_opp()

    outcome = await trader.execute(opp)

    assert outcome.submitted is True
    assert outcome.trade is not None
    assert outcome.trade.leg_status == LEG_STATUS_PARTIAL_NO
    # YES fill > NO fill -> residuo YES eh vendido
    sides = [s[0] for s in client.submitted]
    assert sides == ["BUY", "BUY", "SELL"]
    sell = [s for s in client.submitted if s[0] == "SELL"][0]
    assert sell[1] == "yt"  # vendeu YES (residuo)
    assert outcome.residual_response is not None
    assert outcome.residual_response.success


@pytest.mark.asyncio
async def test_live_failed_when_both_legs_reject(tmp_path: Path) -> None:
    client = FakeLiveClient()
    fail = OrderResponse(
        success=False, order_id=None, filled_shares=0.0,
        avg_fill_price=None, fees_paid_usdc=0.0, error="rejected",
    )
    client.buy_responses = {"yt": fail, "nt": fail}
    trader, _store, _rm = _trader(tmp_path, client=client, capital=20.0)

    outcome = await trader.execute(_make_opp())

    assert outcome.submitted is True
    assert outcome.trade is not None
    assert outcome.trade.leg_status == LEG_STATUS_FAILED
    # Sem residual quando ambos falham
    sides = [s[0] for s in client.submitted]
    assert sides == ["BUY", "BUY"]


@pytest.mark.asyncio
async def test_live_kill_switch_triggers_when_daily_loss_breaches(tmp_path: Path) -> None:
    """Trade com perda enorme dispara kill-switch."""
    client = FakeLiveClient()
    # YES preenche 20 a 0.40 = 8 USDC, NO preenche 20 a 0.99 = 19.80 USDC
    # gross = 20 - 27.80 = -7.80 -> ultrapassa max_daily_loss=5.0
    client.buy_responses["nt"] = OrderResponse(
        success=True, order_id="x", filled_shares=20.0,
        avg_fill_price=0.99, fees_paid_usdc=0.0,
    )
    limits = RiskLimits(
        max_capital_per_trade_usdc=50.0,
        max_open_exposure_usdc=100.0,
        max_daily_loss_usdc=5.0,
        min_usdc_balance=10.0,
    )
    trader, _store, rm = _trader(tmp_path, client=client, capital=30.0, limits=limits)

    outcome = await trader.execute(_make_opp())
    assert outcome.submitted is True
    assert outcome.trade.net_pnl_usdc < -5.0
    assert rm.kill_switch.is_active()


@pytest.mark.asyncio
async def test_live_persists_real_fees_from_responses(tmp_path: Path) -> None:
    client = FakeLiveClient()
    client.buy_responses["yt"] = OrderResponse(
        success=True, order_id="y", filled_shares=20.0, avg_fill_price=0.40,
        fees_paid_usdc=0.30,   # 30 cents
    )
    client.buy_responses["nt"] = OrderResponse(
        success=True, order_id="n", filled_shares=20.0, avg_fill_price=0.50,
        fees_paid_usdc=0.40,   # 40 cents
    )
    trader, store, _rm = _trader(tmp_path, client=client, capital=20.0)

    outcome = await trader.execute(_make_opp())

    assert outcome.trade is not None
    # fees_paid = 0.30 + 0.40 = 0.70 (real, nao estimado)
    assert abs(outcome.trade.fees_paid_usdc - 0.70) < 1e-9
    rows = store.recent_trades(mode="live", limit=1)
    assert abs(rows[0]["taker_fee_paid_usdc"] - 0.70) < 1e-9


@pytest.mark.asyncio
async def test_live_links_opportunity_id(tmp_path: Path) -> None:
    client = FakeLiveClient()
    trader, store, _rm = _trader(tmp_path, client=client, capital=10.0)

    opp = _make_opp()
    opp_id = store.insert_opportunity(
        OpportunityRow(
            market_id=opp.market.id,
            condition_id=opp.market.condition_id,
            market_question=opp.market.question,
            ask_yes=0.40,
            ask_no=0.50,
            size_max_usdc=18.0,
            size_max_shares=20.0,
            gross_pnl_usdc=2.0,
            estimated_fees_usdc=0.0,
            estimated_gas_usdc=0.0,
        )
    )

    outcome = await trader.execute(opp, opportunity_id=opp_id)

    assert outcome.submitted
    rows = store.recent_trades(mode="live", limit=1)
    assert rows[0]["opportunity_id"] == opp_id
    with store._connect() as conn:
        cur = conn.execute("SELECT was_traded FROM opportunities WHERE id = ?", (opp_id,))
        assert cur.fetchone()["was_traded"] == 1
