"""Replay de snapshots historicos contra o detector.

Util pra:
- Calibrar threshold de lucro liquido sem rodar 24h ao vivo.
- Testar mudanca no CostModel (ex.: e se taker_fee subir pra 200bps?).
- Estimar duracao tipica de oportunidades (quanto tempo o bot tem pra
  agir antes da arb sumir).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from bot.backtest.snapshot import Snapshot
from bot.strategies.intra_market import ArbOpportunity, IntraMarketDetector


@dataclass
class MarketStats:
    market_id: str
    question: str
    detections: int = 0
    gross_pnl_usdc: float = 0.0
    fees_usdc: float = 0.0
    gas_usdc: float = 0.0
    net_pnl_usdc: float = 0.0


@dataclass
class ReplayMetrics:
    snapshots_processed: int = 0
    snapshots_with_arb: int = 0
    detections: int = 0
    gross_pnl_usdc: float = 0.0
    fees_usdc: float = 0.0
    gas_usdc: float = 0.0
    net_pnl_usdc: float = 0.0

    first_ts: str | None = None
    last_ts: str | None = None

    per_market: dict[str, MarketStats] = field(default_factory=dict)
    durations_seconds: list[float] = field(default_factory=list)

    @property
    def time_span_seconds(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        try:
            t0 = datetime.fromisoformat(self.first_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(self.last_ts.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return 0.0
        return (t1 - t0).total_seconds()

    @property
    def time_span_days(self) -> float:
        return self.time_span_seconds / 86_400.0

    def duration_quantile(self, q: float) -> float | None:
        """Quantil das duracoes detectadas (0 <= q <= 1)."""
        if not self.durations_seconds:
            return None
        sorted_d = sorted(self.durations_seconds)
        idx = max(0, min(len(sorted_d) - 1, int(round(q * (len(sorted_d) - 1)))))
        return sorted_d[idx]

    @property
    def avg_duration(self) -> float | None:
        if not self.durations_seconds:
            return None
        return sum(self.durations_seconds) / len(self.durations_seconds)

    @property
    def top_markets(self) -> list[MarketStats]:
        return sorted(self.per_market.values(), key=lambda s: s.net_pnl_usdc, reverse=True)


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class ReplayRunner:
    """Executa o detector contra um stream de snapshots.

    Para tracking de duracao: para cada mercado, mantem o timestamp da
    primeira deteccao consecutiva. Quando uma deteccao falha (sem arb),
    fecha o intervalo e registra a duracao.
    """

    def __init__(self, detector: IntraMarketDetector) -> None:
        self.detector = detector

    def run(self, snapshots: Iterable[Snapshot]) -> ReplayMetrics:
        """Mede duracao de cada janela de arb por mercado.

        Convencao:
        - Quando uma arb termina mid-stream (snap sem arb apos snaps com arb),
          fecha a janela em (start_ts, no_arb_ts). Upper bound: a arb durou
          *no maximo* ate este snap.
        - Quando o stream acaba e a arb ainda esta ativa, fecha em
          (start_ts, last_seen_arb_ts). Lower bound observado.
        """
        metrics = ReplayMetrics()
        active: dict[str, tuple[str, str]] = {}  # market_id -> (start_ts, last_seen_arb_ts)

        for snap in snapshots:
            metrics.snapshots_processed += 1
            if metrics.first_ts is None:
                metrics.first_ts = snap.ts
            metrics.last_ts = snap.ts

            opp = self.detector.detect(snap.market, snap.yes_book, snap.no_book)
            mid = snap.market.id

            if opp is not None:
                metrics.snapshots_with_arb += 1
                self._accumulate(metrics, opp, snap.market.question)
                if mid in active:
                    start_ts, _ = active[mid]
                    active[mid] = (start_ts, snap.ts)
                else:
                    active[mid] = (snap.ts, snap.ts)
            else:
                if mid in active:
                    start_ts, _ = active.pop(mid)
                    self._close_arb_window(metrics, (start_ts, snap.ts))

        for window in active.values():
            self._close_arb_window(metrics, window)

        return metrics

    @staticmethod
    def _accumulate(metrics: ReplayMetrics, opp: ArbOpportunity, question: str) -> None:
        metrics.detections += 1
        metrics.gross_pnl_usdc += opp.gross_pnl_usdc
        metrics.fees_usdc += opp.variable_cost.fees_usdc
        metrics.gas_usdc += opp.variable_cost.gas_usdc
        metrics.net_pnl_usdc += opp.net_pnl_usdc

        mid = opp.market.id
        ms = metrics.per_market.get(mid)
        if ms is None:
            ms = MarketStats(market_id=mid, question=question or "")
            metrics.per_market[mid] = ms
        ms.detections += 1
        ms.gross_pnl_usdc += opp.gross_pnl_usdc
        ms.fees_usdc += opp.variable_cost.fees_usdc
        ms.gas_usdc += opp.variable_cost.gas_usdc
        ms.net_pnl_usdc += opp.net_pnl_usdc

    @staticmethod
    def _close_arb_window(metrics: ReplayMetrics, window: tuple[str, str]) -> None:
        start_ts, end_ts = window
        t0 = _parse_iso(start_ts)
        t1 = _parse_iso(end_ts)
        if t0 is None or t1 is None:
            return
        dur = (t1 - t0).total_seconds()
        if dur >= 0:
            metrics.durations_seconds.append(dur)
