"""Tests do IntraMarketDetector e funcoes auxiliares."""
from __future__ import annotations

from bot.clients.models import BookLevel, Market, OrderBook
from bot.economics.cost_model import CostModel
from bot.strategies.intra_market import (
    IntraMarketDetector,
    _cost_for_size,
    _cumulative_curve,
)


def _market(mid: str = "m1") -> Market:
    return Market.model_validate(
        {
            "id": mid,
            "question": "Test market?",
            "slug": "t",
            "conditionId": "0xtest",
            "active": True,
            "clobTokenIds": '["yes", "no"]',
            "outcomes": '["Yes", "No"]',
            "volumeNum": 100000,
        }
    )


def _book(asset_id: str, asks: list[tuple[float, float]]) -> OrderBook:
    return OrderBook.model_validate(
        {
            "asset_id": asset_id,
            "asks": [{"price": p, "size": s} for p, s in asks],
            "bids": [],
        }
    )


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


def _real_cost_model() -> CostModel:
    return CostModel(
        taker_fee_bps=100,           # 1%
        maker_fee_bps=0,
        avg_gas_polygon_usd=0.05,
        txs_per_arb=2,
        monthly_rpc_usd=0.0,
        monthly_vps_usd=10.0,
        monthly_other_usd=0.0,
    )


def test_cumulative_curve_orders_by_price() -> None:
    asks = [BookLevel(price=0.50, size=10), BookLevel(price=0.45, size=20)]
    curve = _cumulative_curve(asks)
    assert len(curve) == 2
    assert curve[0].cum_size == 20
    assert curve[0].cum_cost == 20 * 0.45
    assert curve[1].cum_size == 30
    assert abs(curve[1].cum_cost - (20 * 0.45 + 10 * 0.50)) < 1e-9


def test_cost_for_size_within_first_level() -> None:
    asks = [BookLevel(price=0.40, size=100)]
    curve = _cumulative_curve(asks)
    assert _cost_for_size(curve, 50) == 50 * 0.40


def test_cost_for_size_crosses_levels() -> None:
    asks = [BookLevel(price=0.40, size=10), BookLevel(price=0.42, size=10)]
    curve = _cumulative_curve(asks)
    # comprar 15 = 10 @ 0.40 + 5 @ 0.42 = 4.0 + 2.1 = 6.10
    assert abs(_cost_for_size(curve, 15) - 6.10) < 1e-9


def test_cost_for_size_too_deep_returns_none() -> None:
    asks = [BookLevel(price=0.40, size=10)]
    curve = _cumulative_curve(asks)
    assert _cost_for_size(curve, 100) is None


def test_detector_obvious_arb_zero_cost() -> None:
    """YES@0.40 + NO@0.50 = 0.90 → arb obvia, sem custos."""
    market = _market()
    yb = _book("yes", [(0.40, 100)])
    nb = _book("no", [(0.50, 100)])
    det = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    opp = det.detect(market, yb, nb)

    assert opp is not None
    assert opp.size_shares == 100
    assert abs(opp.gross_pnl_usdc - 10.0) < 1e-9   # 100 * (1 - 0.90)
    assert abs(opp.net_pnl_usdc - 10.0) < 1e-9
    assert abs(opp.notional_usdc - 90.0) < 1e-9
    assert abs(opp.net_pnl_pct - (10.0 / 90.0 * 100)) < 1e-9


def test_detector_no_arb() -> None:
    """YES@0.55 + NO@0.50 = 1.05 → sem arb."""
    market = _market()
    yb = _book("yes", [(0.55, 100)])
    nb = _book("no", [(0.50, 100)])
    det = IntraMarketDetector(_real_cost_model(), min_net_profit_pct=0.0)
    assert det.detect(market, yb, nb) is None


def test_detector_arb_killed_by_costs() -> None:
    """YES + NO = 0.998 (arb bruta de 0.2%) → custos comem tudo."""
    market = _market()
    yb = _book("yes", [(0.499, 1000)])
    nb = _book("no", [(0.499, 1000)])
    # taker 1% + gas: pra notional ~998 isso eh ~10 USD de fees - mata a arb
    det = IntraMarketDetector(_real_cost_model(), min_net_profit_pct=0.5)
    assert det.detect(market, yb, nb) is None


def test_detector_threshold_filters_marginal_arb() -> None:
    market = _market()
    yb = _book("yes", [(0.49, 100)])
    nb = _book("no", [(0.50, 100)])
    # gross = 100 * 0.01 = 1.0 ; notional = 99 ; gross_pct = ~1.01%
    cm = _zero_cost_model()
    det_low = IntraMarketDetector(cm, min_net_profit_pct=0.5)
    det_high = IntraMarketDetector(cm, min_net_profit_pct=2.0)

    assert det_low.detect(market, yb, nb) is not None
    assert det_high.detect(market, yb, nb) is None


def test_detector_chooses_size_that_maximizes_net() -> None:
    """Book com varios niveis: detector deve achar tamanho otimo.

    Tres tamanhos candidatos: 50, 100, 150.
      size=50:  cost = 50*0.40 + 50*0.50 = 45      gross = 5.0
      size=100: cost = 42.5 + 51.0 = 93.5          gross = 6.5
      size=150: cost = (50*0.40 + 100*0.45) + (50*0.50 + 100*0.52)
                     = 65 + 77 = 142                gross = 8.0  <- otimo
    """
    market = _market()
    yb = _book("yes", [(0.40, 50), (0.45, 100)])
    nb = _book("no", [(0.50, 50), (0.52, 100)])
    det = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    opp = det.detect(market, yb, nb)
    assert opp is not None
    assert opp.size_shares == 150
    assert abs(opp.gross_pnl_usdc - 8.0) < 1e-9


def test_detector_stops_extending_when_marginal_negative() -> None:
    """Se o segundo nivel ja torna a arb negativa, detector deve ficar no topo."""
    market = _market()
    # Topo: 0.40 + 0.50 = 0.90 (arb)
    # Segundo nivel YES: 0.55 -> 0.55 + 0.55 = 1.10 (sem arb)
    yb = _book("yes", [(0.40, 50), (0.55, 100)])
    nb = _book("no", [(0.50, 50), (0.55, 100)])
    det = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    opp = det.detect(market, yb, nb)
    assert opp is not None
    assert opp.size_shares == 50
    assert abs(opp.gross_pnl_usdc - 5.0) < 1e-9


def test_detector_empty_books() -> None:
    market = _market()
    yb = _book("yes", [])
    nb = _book("no", [])
    det = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)
    assert det.detect(market, yb, nb) is None


def test_detector_only_one_side_empty() -> None:
    market = _market()
    yb = _book("yes", [(0.40, 100)])
    nb = _book("no", [])
    det = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)
    assert det.detect(market, yb, nb) is None
