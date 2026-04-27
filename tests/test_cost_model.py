"""Tests do CostModel."""
from __future__ import annotations

from bot.config.settings import Settings
from bot.economics.cost_model import CostModel


def _model(**kwargs) -> CostModel:
    defaults = dict(
        taker_fee_bps=100,           # 1%
        maker_fee_bps=0,
        avg_gas_polygon_usd=0.05,
        txs_per_arb=2,
        monthly_rpc_usd=0.0,
        monthly_vps_usd=10.0,
        monthly_other_usd=0.0,
    )
    defaults.update(kwargs)
    return CostModel(**defaults)


def test_variable_cost_taker_fees() -> None:
    cm = _model(taker_fee_bps=200)
    vc = cm.variable_cost(notional_usdc=100.0)
    assert vc.fees_usdc == 2.0   # 100 * 200/10000
    assert vc.gas_usdc == 0.10   # 0.05 * 2 txs
    assert vc.total == 2.10


def test_variable_cost_zero_fees() -> None:
    cm = _model(taker_fee_bps=0)
    vc = cm.variable_cost(notional_usdc=1000.0)
    assert vc.fees_usdc == 0.0
    assert vc.gas_usdc == 0.10


def test_variable_cost_overrides_gas() -> None:
    cm = _model()
    vc = cm.variable_cost(notional_usdc=100.0, gas_usdc=0.30)
    assert vc.gas_usdc == 0.30


def test_fixed_costs_amortization() -> None:
    cm = _model(monthly_rpc_usd=49.0, monthly_vps_usd=10.0, monthly_other_usd=1.0)
    assert cm.total_monthly_fixed_usd == 60.0
    assert abs(cm.daily_fixed_cost_usd - 2.0) < 1e-9
    assert cm.break_even_daily_pnl() == cm.daily_fixed_cost_usd


def test_from_settings_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TAKER_FEE_BPS", "150")
    monkeypatch.setenv("MONTHLY_VPS_USD", "20")
    monkeypatch.setenv("AVG_GAS_POLYGON_USD", "0.10")

    s = Settings()
    cm = CostModel.from_settings(s)
    assert cm.taker_fee_bps == 150
    assert cm.monthly_vps_usd == 20.0
    assert cm.avg_gas_polygon_usd == 0.10
    assert cm.txs_per_arb == 2
