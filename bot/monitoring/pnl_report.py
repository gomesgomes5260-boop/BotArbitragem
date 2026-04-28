"""Relatorio de P&L consolidado: trades reais + custos fixos amortizados.

Diferenca chave em relacao ao `stats` (Fase 2):
- `stats` mostra oportunidades **detectadas** (potenciais).
- `pnl` mostra trades **executados** (paper ou live), descontando ja os
  custos fixos da infra no periodo.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from bot.data.opportunity_store import OpportunityStore
from bot.economics.cost_model import CostModel


PeriodAlias = Literal["1d", "7d", "30d", "all"]


def _period_to_since(period: str) -> datetime | None:
    if period == "all":
        return None
    if period.endswith("d"):
        try:
            n = int(period[:-1])
        except ValueError as e:
            raise ValueError(f"periodo invalido: {period}") from e
        return datetime.now(timezone.utc) - timedelta(days=n)
    raise ValueError(f"periodo invalido: {period}. Use '1d', '7d', '30d' ou 'all'.")


@dataclass
class PnLReport:
    period: str
    mode: str  # 'paper' | 'live' | 'all'
    since_iso: str | None

    num_trades: int
    notional_usdc: float
    gross_pnl_usdc: float
    fees_usdc: float
    gas_usdc: float
    net_pnl_usdc: float

    wins: int
    full_fills: int

    days_in_window: float
    fixed_costs_usd: float
    final_net_after_fixed: float

    @property
    def win_rate(self) -> float:
        if self.num_trades == 0:
            return 0.0
        return (self.wins / self.num_trades) * 100.0

    @property
    def fill_rate(self) -> float:
        if self.num_trades == 0:
            return 0.0
        return (self.full_fills / self.num_trades) * 100.0

    @property
    def roi_pct(self) -> float:
        if self.notional_usdc <= 0:
            return 0.0
        return (self.net_pnl_usdc / self.notional_usdc) * 100.0

    @property
    def pays_infra(self) -> bool:
        return self.final_net_after_fixed > 0


def build_report(
    store: OpportunityStore,
    cost_model: CostModel,
    *,
    period: str = "all",
    mode: str | None = None,
) -> PnLReport:
    """Constroi um PnLReport para o periodo/mode dado.

    Args:
        period: '1d' | '7d' | '30d' | 'all'.
        mode: 'paper' | 'live' | None (todos).
    """
    since = _period_to_since(period)
    since_iso = since.isoformat() if since is not None else None

    agg = store.aggregate_trades(mode=mode, since_iso=since_iso)

    if since is not None:
        days = (datetime.now(timezone.utc) - since).total_seconds() / 86_400.0
    else:
        first_ts, last_ts = store.trades_first_last_ts(mode=mode)
        if first_ts and last_ts:
            try:
                t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                days = max((t1 - t0).total_seconds() / 86_400.0, 1.0)
            except (TypeError, ValueError):
                days = 1.0
        else:
            days = 1.0

    fixed_costs = cost_model.daily_fixed_cost_usd * days
    net_after_fixed = agg["net_pnl_usdc"] - fixed_costs

    return PnLReport(
        period=period,
        mode=mode or "all",
        since_iso=since_iso,
        num_trades=int(agg["num_trades"]),
        notional_usdc=float(agg["notional_usdc"]),
        gross_pnl_usdc=float(agg["gross_pnl_usdc"]),
        fees_usdc=float(agg["taker_fees_usdc"]) + float(agg["maker_fees_usdc"]),
        gas_usdc=float(agg["gas_usdc"]),
        net_pnl_usdc=float(agg["net_pnl_usdc"]),
        wins=int(agg["wins"]),
        full_fills=int(agg["full_fills"]),
        days_in_window=days,
        fixed_costs_usd=fixed_costs,
        final_net_after_fixed=net_after_fixed,
    )


def format_report(report: PnLReport) -> list[str]:
    lines: list[str] = []
    lines.append(f"== PnL {report.mode} - {report.period} ==")
    lines.append(f"   trades:           {report.num_trades}")
    lines.append(f"   periodo (dias):   {report.days_in_window:.2f}")
    lines.append("")
    lines.append(f"   notional total:   ${report.notional_usdc:.2f}")
    lines.append(f"   gross_pnl:        ${report.gross_pnl_usdc:.2f}")
    lines.append(f"   fees:             ${report.fees_usdc:.2f}")
    lines.append(f"   gas:              ${report.gas_usdc:.2f}")
    lines.append(f"   net_pnl (trades): ${report.net_pnl_usdc:.2f}")
    lines.append("")
    lines.append(f"   custo fixo:       ${report.fixed_costs_usd:.4f}")
    lines.append(f"   net pos infra:    ${report.final_net_after_fixed:.2f}")
    lines.append(f"   paga a infra:     {'SIM' if report.pays_infra else 'NAO'}")
    lines.append("")
    lines.append(f"   win rate:         {report.win_rate:.1f}%")
    lines.append(f"   fill rate (both): {report.fill_rate:.1f}%")
    lines.append(f"   ROI:              {report.roi_pct:.2f}%")
    return lines
