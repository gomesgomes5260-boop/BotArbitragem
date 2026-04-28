"""Tests do ReplayRunner com snapshots sinteticos."""
from __future__ import annotations

from bot.backtest.replay import ReplayRunner
from bot.backtest.snapshot import Snapshot
from bot.clients.models import BookLevel, Market, OrderBook
from bot.economics.cost_model import CostModel
from bot.strategies.intra_market import IntraMarketDetector


def _market(mid: str = "m1", question: str = "Test?") -> Market:
    return Market.model_validate(
        {
            "id": mid,
            "question": question,
            "slug": "t",
            "conditionId": f"0x{mid}",
            "active": True,
            "clobTokenIds": '["y", "n"]',
            "outcomes": '["Yes", "No"]',
            "volumeNum": 50000,
        }
    )


def _book(asset: str, asks: list[tuple[float, float]]) -> OrderBook:
    return OrderBook(
        asset_id=asset,
        asks=[BookLevel(price=p, size=s) for p, s in asks],
        bids=[],
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


def _arb_snap(mid: str, ts: str) -> Snapshot:
    return Snapshot(
        ts=ts,
        market=_market(mid),
        yes_book=_book("y", [(0.40, 100)]),
        no_book=_book("n", [(0.50, 100)]),
    )


def _no_arb_snap(mid: str, ts: str) -> Snapshot:
    return Snapshot(
        ts=ts,
        market=_market(mid),
        yes_book=_book("y", [(0.55, 100)]),
        no_book=_book("n", [(0.50, 100)]),
    )


def test_replay_accumulates_metrics() -> None:
    snaps = [
        _arb_snap("m1", "2026-04-28T10:00:00+00:00"),
        _arb_snap("m1", "2026-04-28T10:00:05+00:00"),
        _arb_snap("m2", "2026-04-28T10:00:05+00:00"),
    ]
    detector = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    metrics = ReplayRunner(detector).run(snaps)

    assert metrics.snapshots_processed == 3
    assert metrics.snapshots_with_arb == 3
    assert metrics.detections == 3
    assert abs(metrics.gross_pnl_usdc - 30.0) < 1e-9   # 3 x gross 10.0
    assert metrics.net_pnl_usdc == metrics.gross_pnl_usdc  # zero costs
    assert metrics.first_ts == "2026-04-28T10:00:00+00:00"
    assert metrics.last_ts == "2026-04-28T10:00:05+00:00"


def test_replay_per_market_aggregation() -> None:
    snaps = [
        _arb_snap("m1", "2026-04-28T10:00:00+00:00"),
        _arb_snap("m1", "2026-04-28T10:00:05+00:00"),
        _arb_snap("m2", "2026-04-28T10:00:05+00:00"),
    ]
    detector = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    metrics = ReplayRunner(detector).run(snaps)

    assert "m1" in metrics.per_market
    assert metrics.per_market["m1"].detections == 2
    assert abs(metrics.per_market["m1"].net_pnl_usdc - 20.0) < 1e-9
    assert metrics.per_market["m2"].detections == 1


def test_replay_tracks_duration_when_arb_ends() -> None:
    """Arb dura 3 snapshots e some. Closure no no_arb_ts -> upper bound 35s."""
    snaps = [
        _arb_snap("m1", "2026-04-28T10:00:00+00:00"),
        _arb_snap("m1", "2026-04-28T10:00:15+00:00"),
        _arb_snap("m1", "2026-04-28T10:00:30+00:00"),
        _no_arb_snap("m1", "2026-04-28T10:00:35+00:00"),
    ]
    detector = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    metrics = ReplayRunner(detector).run(snaps)

    assert len(metrics.durations_seconds) == 1
    # 10:00:35 - 10:00:00 = 35s
    assert abs(metrics.durations_seconds[0] - 35.0) < 1e-9


def test_replay_closes_open_arbs_at_end() -> None:
    """Se a serie acaba durante uma arb, registra (start, last_seen)."""
    snaps = [
        _arb_snap("m1", "2026-04-28T10:00:00+00:00"),
        _arb_snap("m1", "2026-04-28T10:01:00+00:00"),
    ]
    detector = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    metrics = ReplayRunner(detector).run(snaps)

    assert len(metrics.durations_seconds) == 1
    assert abs(metrics.durations_seconds[0] - 60.0) < 1e-9


def test_replay_two_separate_arb_windows() -> None:
    """Janela 1: ends mid-stream em no_arb -> fecha em no_arb_ts.
    Janela 2: stream acaba -> fecha em last_seen."""
    snaps = [
        _arb_snap("m1", "2026-04-28T10:00:00+00:00"),
        _arb_snap("m1", "2026-04-28T10:00:10+00:00"),
        _no_arb_snap("m1", "2026-04-28T10:00:15+00:00"),
        _arb_snap("m1", "2026-04-28T10:01:00+00:00"),
        _arb_snap("m1", "2026-04-28T10:01:25+00:00"),
    ]
    detector = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    metrics = ReplayRunner(detector).run(snaps)

    assert len(metrics.durations_seconds) == 2
    durations = sorted(metrics.durations_seconds)
    # janela 1: 10:00:15 - 10:00:00 = 15 (closure no no_arb_ts)
    # janela 2: 10:01:25 - 10:01:00 = 25 (closure no last_seen)
    assert abs(durations[0] - 15.0) < 1e-9
    assert abs(durations[1] - 25.0) < 1e-9


def test_replay_quantiles() -> None:
    snaps = [
        _arb_snap("m1", "2026-04-28T10:00:00+00:00"),
        _no_arb_snap("m1", "2026-04-28T10:00:10+00:00"),
        _arb_snap("m1", "2026-04-28T10:01:00+00:00"),
        _no_arb_snap("m1", "2026-04-28T10:01:30+00:00"),
        _arb_snap("m1", "2026-04-28T10:02:00+00:00"),
        _no_arb_snap("m1", "2026-04-28T10:03:00+00:00"),
    ]
    detector = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)

    metrics = ReplayRunner(detector).run(snaps)

    # duracoes: 10, 30, 60
    assert sorted(metrics.durations_seconds) == [10.0, 30.0, 60.0]
    assert metrics.avg_duration is not None
    assert abs(metrics.avg_duration - (10 + 30 + 60) / 3) < 1e-9
    # p50 sobre 3 elementos = elemento do meio
    assert metrics.duration_quantile(0.50) == 30.0


def test_replay_empty_iter() -> None:
    detector = IntraMarketDetector(_zero_cost_model(), min_net_profit_pct=0.0)
    metrics = ReplayRunner(detector).run([])
    assert metrics.snapshots_processed == 0
    assert metrics.detections == 0
    assert metrics.first_ts is None
    assert metrics.duration_quantile(0.5) is None
