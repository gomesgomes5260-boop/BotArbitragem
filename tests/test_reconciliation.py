"""Tests de Reconciliator e health checks."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from bot.data.opportunity_store import OpportunityStore
from bot.execution.live_client import OrderResponse
from bot.execution.reconciliation import Reconciliator
from bot.monitoring.health import (
    HealthReport,
    make_balance_check,
    make_kill_switch_check,
    run_all,
)
from bot.risk.kill_switch import KillSwitch
from bot.risk.limits import RiskLimits, RiskManager


@dataclass
class FakeBalanceClient:
    balance: float = 100.0

    async def submit_buy_ioc(self, *a, **k) -> OrderResponse:  # pragma: no cover
        raise NotImplementedError

    async def submit_sell_ioc(self, *a, **k) -> OrderResponse:  # pragma: no cover
        raise NotImplementedError

    async def get_usdc_balance(self) -> float:
        return self.balance


def _store(tmp_path: Path) -> OpportunityStore:
    s = OpportunityStore(tmp_path / "rec.db")
    s.init_schema()
    return s


def _rm(tmp_path: Path) -> RiskManager:
    limits = RiskLimits(
        max_capital_per_trade_usdc=100.0,
        max_open_exposure_usdc=1000.0,
        max_daily_loss_usdc=50.0,
        min_usdc_balance=10.0,
    )
    return RiskManager(limits, KillSwitch(tmp_path / "KILL"))


@pytest.mark.asyncio
async def test_reconciliation_ok_when_balance_matches(tmp_path: Path) -> None:
    client = FakeBalanceClient(balance=100.0)
    rec = Reconciliator(
        client, _store(tmp_path), _rm(tmp_path), starting_balance_usdc=100.0
    )

    result = await rec.check()

    assert result.ok
    assert result.drift_usdc >= 0


@pytest.mark.asyncio
async def test_reconciliation_detects_drain(tmp_path: Path) -> None:
    client = FakeBalanceClient(balance=50.0)
    rm = _rm(tmp_path)
    rec = Reconciliator(
        client, _store(tmp_path), rm, starting_balance_usdc=100.0, tolerance_usdc=1.0
    )

    result = await rec.check()

    assert not result.ok
    assert "unexpected_drain" in (result.reason or "")
    # Kill-switch deve ter sido acionado
    assert rm.kill_switch.is_active()


@pytest.mark.asyncio
async def test_reconciliation_handles_balance_error(tmp_path: Path) -> None:
    class BadClient(FakeBalanceClient):
        async def get_usdc_balance(self) -> float:
            raise RuntimeError("rpc down")

    rec = Reconciliator(
        BadClient(), _store(tmp_path), _rm(tmp_path), starting_balance_usdc=100.0
    )
    result = await rec.check()
    assert not result.ok
    assert "balance_fetch_error" in (result.reason or "")


@pytest.mark.asyncio
async def test_health_balance_check_ok() -> None:
    client = FakeBalanceClient(balance=50.0)
    check = make_balance_check(client, min_balance=10.0)
    result = await check()
    assert result.ok
    assert "50" in (result.detail or "")


@pytest.mark.asyncio
async def test_health_balance_check_under_min() -> None:
    client = FakeBalanceClient(balance=5.0)
    check = make_balance_check(client, min_balance=10.0)
    result = await check()
    assert not result.ok


@pytest.mark.asyncio
async def test_health_kill_switch_check(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    check = make_kill_switch_check(ks)
    assert (await check()).ok is True
    ks.trigger("manual")
    assert (await check()).ok is False


@pytest.mark.asyncio
async def test_health_run_all_aggregates(tmp_path: Path) -> None:
    client = FakeBalanceClient(balance=50.0)
    ks = KillSwitch(tmp_path / "KILL")
    report = await run_all(
        [
            make_balance_check(client, min_balance=10.0),
            make_kill_switch_check(ks),
        ]
    )
    assert isinstance(report, HealthReport)
    assert report.ok is True

    ks.trigger("test")
    report2 = await run_all(
        [
            make_balance_check(client, min_balance=10.0),
            make_kill_switch_check(ks),
        ]
    )
    assert report2.ok is False
    assert any("kill_switch" in r for r in report2.reasons())
