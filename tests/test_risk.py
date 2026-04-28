"""Tests de KillSwitch e RiskManager."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.risk.kill_switch import KillSwitch
from bot.risk.limits import RiskLimits, RiskManager, RiskSnapshot


def _limits(**overrides) -> RiskLimits:
    base = dict(
        max_capital_per_trade_usdc=10.0,
        max_open_exposure_usdc=30.0,
        max_daily_loss_usdc=5.0,
        min_usdc_balance=10.0,
    )
    base.update(overrides)
    return RiskLimits(**base)


def _ok_snap(**overrides) -> RiskSnapshot:
    base = dict(
        intended_size_usdc=5.0,
        current_open_exposure_usdc=0.0,
        today_realized_pnl_usdc=0.0,
        usdc_balance=100.0,
    )
    base.update(overrides)
    return RiskSnapshot(**base)


def _manager(tmp_path: Path, **overrides) -> RiskManager:
    ks = KillSwitch(tmp_path / "KILL")
    return RiskManager(_limits(**overrides), ks, hard_cap_per_trade_usdc=20.0)


def test_kill_switch_initially_inactive(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    assert ks.is_active() is False
    assert ks.reason() is None


def test_kill_switch_trigger_creates_file(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    ks.trigger("test reason")
    assert ks.is_active()
    assert "test reason" in (ks.reason() or "")


def test_kill_switch_reset_removes_file(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    ks.trigger("x")
    ks.reset()
    assert ks.is_active() is False
    ks.reset()  # idempotente


def test_kill_switch_persists_across_instances(tmp_path: Path) -> None:
    p = tmp_path / "KILL"
    KillSwitch(p).trigger("first")
    assert KillSwitch(p).is_active()


def test_pre_trade_allows_normal_trade(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    res = rm.check_pre_trade(_ok_snap())
    assert res.allowed
    assert res.reason is None


def test_pre_trade_blocks_when_kill_switch_active(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    rm.kill_switch.trigger("manual")
    res = rm.check_pre_trade(_ok_snap())
    assert not res.allowed
    assert res.reason == "kill_switch_active"


def test_pre_trade_blocks_zero_or_negative_size(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    res = rm.check_pre_trade(_ok_snap(intended_size_usdc=0.0))
    assert not res.allowed
    assert "intended_size_invalid" in (res.reason or "")


def test_pre_trade_blocks_above_per_trade_cap(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    res = rm.check_pre_trade(_ok_snap(intended_size_usdc=15.0))   # cap = 10
    assert not res.allowed
    assert "per_trade_cap" in (res.reason or "")


def test_pre_trade_blocks_above_hard_cap(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    rm.hard_cap_per_trade_usdc = 5.0
    res = rm.check_pre_trade(_ok_snap(intended_size_usdc=8.0))   # > hard cap
    assert not res.allowed
    assert "hard_cap_exceeded" in (res.reason or "")


def test_pre_trade_blocks_above_open_exposure_cap(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    res = rm.check_pre_trade(_ok_snap(intended_size_usdc=10.0, current_open_exposure_usdc=25.0))
    # 10 + 25 = 35 > 30
    assert not res.allowed
    assert "exposure_cap" in (res.reason or "")


def test_pre_trade_blocks_when_daily_loss_breached(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    res = rm.check_pre_trade(_ok_snap(today_realized_pnl_usdc=-5.5))   # > limit 5
    assert not res.allowed
    assert "daily_loss_limit" in (res.reason or "")


def test_pre_trade_blocks_when_balance_too_low(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    res = rm.check_pre_trade(_ok_snap(usdc_balance=5.0))   # < min 10
    assert not res.allowed
    assert "balance_too_low" in (res.reason or "")


def test_record_outcome_triggers_kill_switch_on_limit_breach(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    assert not rm.kill_switch.is_active()
    rm.record_outcome(today_realized_pnl_usdc=-6.0)
    assert rm.kill_switch.is_active()
    assert "daily_loss_limit_breached" in (rm.kill_switch.reason() or "")


def test_record_outcome_does_nothing_under_limit(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    rm.record_outcome(today_realized_pnl_usdc=-2.0)
    assert not rm.kill_switch.is_active()


def test_record_outcome_idempotent_when_already_killed(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    rm.kill_switch.trigger("manual reason")
    rm.record_outcome(today_realized_pnl_usdc=-50.0)
    # Nao sobrescreve a razao original
    assert "manual reason" in (rm.kill_switch.reason() or "")


def test_status_summary_serializable(tmp_path: Path) -> None:
    rm = _manager(tmp_path)
    s = rm.status_summary()
    assert s["kill_switch_active"] is False
    assert "limits" in s
    assert s["hard_cap_per_trade_usdc"] == 20.0
