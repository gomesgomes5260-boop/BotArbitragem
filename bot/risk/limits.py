"""Limites de risco e gates pre/pos-trade.

Todo `LiveTrader.execute()` deve passar por `check_pre_trade(...)` antes
de submeter ordens, e por `record_outcome(...)` depois - este pode
ativar o kill-switch automaticamente se a perda diaria estourar.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot.config.settings import Settings
from bot.risk.kill_switch import KillSwitch


@dataclass(frozen=True)
class RiskLimits:
    max_capital_per_trade_usdc: float
    max_open_exposure_usdc: float
    max_daily_loss_usdc: float
    min_usdc_balance: float

    @classmethod
    def from_settings(cls, settings: Settings) -> "RiskLimits":
        return cls(
            max_capital_per_trade_usdc=settings.live_max_capital_per_trade_usdc,
            max_open_exposure_usdc=settings.live_max_open_exposure_usdc,
            max_daily_loss_usdc=settings.live_max_daily_loss_usdc,
            min_usdc_balance=settings.live_min_usdc_balance,
        )


@dataclass(frozen=True)
class RiskCheckResult:
    allowed: bool
    reason: str | None = None


@dataclass(frozen=True)
class RiskSnapshot:
    """Estado agregado lido do banco/onchain pra decisao de risco."""

    intended_size_usdc: float
    current_open_exposure_usdc: float
    today_realized_pnl_usdc: float
    usdc_balance: float


class RiskManager:
    """Gates de risco. Sempre persiste o kill-switch via KillSwitch."""

    def __init__(
        self,
        limits: RiskLimits,
        kill_switch: KillSwitch,
        *,
        hard_cap_per_trade_usdc: float | None = None,
    ) -> None:
        self.limits = limits
        self.kill_switch = kill_switch
        self.hard_cap_per_trade_usdc = hard_cap_per_trade_usdc

    @classmethod
    def from_settings(
        cls, settings: Settings, kill_switch: KillSwitch | None = None
    ) -> "RiskManager":
        ks = kill_switch or KillSwitch(settings.kill_switch_path)
        return cls(
            RiskLimits.from_settings(settings),
            ks,
            hard_cap_per_trade_usdc=settings.live_capital_hard_cap_usdc,
        )

    def check_pre_trade(self, snap: RiskSnapshot) -> RiskCheckResult:
        if self.kill_switch.is_active():
            return RiskCheckResult(False, "kill_switch_active")

        if snap.intended_size_usdc <= 0:
            return RiskCheckResult(False, "intended_size_invalid")

        if (
            self.hard_cap_per_trade_usdc is not None
            and snap.intended_size_usdc > self.hard_cap_per_trade_usdc
        ):
            return RiskCheckResult(
                False,
                f"hard_cap_exceeded ({snap.intended_size_usdc:.2f} > {self.hard_cap_per_trade_usdc:.2f})",
            )

        if snap.intended_size_usdc > self.limits.max_capital_per_trade_usdc:
            return RiskCheckResult(
                False,
                f"per_trade_cap ({snap.intended_size_usdc:.2f} > {self.limits.max_capital_per_trade_usdc:.2f})",
            )

        projected = snap.intended_size_usdc + snap.current_open_exposure_usdc
        if projected > self.limits.max_open_exposure_usdc:
            return RiskCheckResult(
                False,
                f"exposure_cap ({projected:.2f} > {self.limits.max_open_exposure_usdc:.2f})",
            )

        if snap.today_realized_pnl_usdc <= -self.limits.max_daily_loss_usdc:
            return RiskCheckResult(
                False,
                f"daily_loss_limit (today_pnl={snap.today_realized_pnl_usdc:.2f})",
            )

        if snap.usdc_balance < self.limits.min_usdc_balance:
            return RiskCheckResult(
                False,
                f"balance_too_low ({snap.usdc_balance:.2f} < {self.limits.min_usdc_balance:.2f})",
            )

        return RiskCheckResult(True, None)

    def record_outcome(self, today_realized_pnl_usdc: float) -> None:
        """Apos cada trade real. Aciona kill-switch se passou do limite."""
        if today_realized_pnl_usdc <= -self.limits.max_daily_loss_usdc:
            if not self.kill_switch.is_active():
                self.kill_switch.trigger(
                    f"daily_loss_limit_breached today_pnl={today_realized_pnl_usdc:.2f}"
                )

    def status_summary(self) -> dict[str, object]:
        return {
            "kill_switch_active": self.kill_switch.is_active(),
            "kill_switch_reason": self.kill_switch.reason(),
            "kill_switch_path": str(self.kill_switch.path),
            "limits": {
                "max_capital_per_trade_usdc": self.limits.max_capital_per_trade_usdc,
                "max_open_exposure_usdc": self.limits.max_open_exposure_usdc,
                "max_daily_loss_usdc": self.limits.max_daily_loss_usdc,
                "min_usdc_balance": self.limits.min_usdc_balance,
            },
            "hard_cap_per_trade_usdc": self.hard_cap_per_trade_usdc,
        }
