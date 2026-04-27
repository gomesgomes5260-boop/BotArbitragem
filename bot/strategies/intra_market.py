"""Detector de arbitragem intra-mercado YES + NO < $1.

Para cada mercado binario, calcula o tamanho maximo profitable da arb
considerando profundidade de book em ambos os lados, descontando custos
variaveis via CostModel.

Algoritmo:
1. Constroi 'curva cumulativa' de cada lado (asks ordenadas por preco).
2. Enumera tamanhos candidatos = uniao dos breakpoints das duas curvas.
3. Para cada tamanho N, calcula custo_yes(N) + custo_no(N) - N (cada
   par YES+NO redime US$ 1) -> lucro bruto.
4. Aplica custos variaveis (fees + gas) -> lucro liquido.
5. Retorna o tamanho que maximiza o lucro liquido.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.clients.models import BookLevel, Market, OrderBook
from bot.economics.cost_model import CostModel, VariableCost


@dataclass(frozen=True)
class ArbOpportunity:
    """Resultado da deteccao quando arb existe (lucro liquido > 0)."""

    market: Market
    yes_book: OrderBook
    no_book: OrderBook

    size_shares: float
    avg_ask_yes: float
    avg_ask_no: float
    gross_pnl_usdc: float
    variable_cost: VariableCost

    @property
    def notional_usdc(self) -> float:
        return self.size_shares * (self.avg_ask_yes + self.avg_ask_no)

    @property
    def net_pnl_usdc(self) -> float:
        return self.gross_pnl_usdc - self.variable_cost.total

    @property
    def net_pnl_pct(self) -> float:
        if self.notional_usdc <= 0:
            return 0.0
        return (self.net_pnl_usdc / self.notional_usdc) * 100.0

    @property
    def best_ask_yes(self) -> float:
        ba = self.yes_book.best_ask
        return ba.price if ba else 0.0

    @property
    def best_ask_no(self) -> float:
        ba = self.no_book.best_ask
        return ba.price if ba else 0.0


@dataclass(frozen=True)
class _CumPoint:
    cum_size: float
    cum_cost: float
    next_marginal_price: float | None  # preco do nivel apos este (None se ultimo)


def _cumulative_curve(asks: list[BookLevel]) -> list[_CumPoint]:
    """Constroi curva cumulativa (size, cost) ordenada por preco ascendente."""
    sorted_asks = sorted(asks, key=lambda lvl: lvl.price)
    out: list[_CumPoint] = []
    cum_s = 0.0
    cum_c = 0.0
    for i, lvl in enumerate(sorted_asks):
        if lvl.size <= 0 or lvl.price <= 0:
            continue
        cum_s += lvl.size
        cum_c += lvl.size * lvl.price
        next_price = (
            sorted_asks[i + 1].price if i + 1 < len(sorted_asks) else None
        )
        out.append(_CumPoint(cum_size=cum_s, cum_cost=cum_c, next_marginal_price=next_price))
    return out


def _cost_for_size(curve: list[_CumPoint], target_size: float) -> float | None:
    """Custo USDC pra comprar `target_size` shares acumuladas. None se book raso demais."""
    if not curve or target_size <= 0:
        return None
    if target_size > curve[-1].cum_size:
        return None
    prev_size = 0.0
    prev_cost = 0.0
    for pt in curve:
        if pt.cum_size >= target_size:
            level_price = (pt.cum_cost - prev_cost) / (pt.cum_size - prev_size)
            extra = target_size - prev_size
            return prev_cost + extra * level_price
        prev_size = pt.cum_size
        prev_cost = pt.cum_cost
    return None


class IntraMarketDetector:
    """Detector stateless. Recebe books e devolve a melhor oportunidade
    (ou None se nao houver lucro liquido positivo acima do threshold)."""

    def __init__(self, cost_model: CostModel, *, min_net_profit_pct: float = 0.5) -> None:
        self.cost_model = cost_model
        self.min_net_profit_pct = min_net_profit_pct

    def detect(
        self, market: Market, yes_book: OrderBook, no_book: OrderBook
    ) -> ArbOpportunity | None:
        if not yes_book.asks or not no_book.asks:
            return None

        curve_yes = _cumulative_curve(yes_book.asks)
        curve_no = _cumulative_curve(no_book.asks)
        if not curve_yes or not curve_no:
            return None

        # quick reject: se o melhor ask YES + melhor ask NO ja >= 1, nao ha arb
        first_y = curve_yes[0].cum_cost / curve_yes[0].cum_size
        first_n = curve_no[0].cum_cost / curve_no[0].cum_size
        if first_y + first_n >= 1.0:
            return None

        max_size = min(curve_yes[-1].cum_size, curve_no[-1].cum_size)
        if max_size <= 0:
            return None

        candidate_sizes: set[float] = set()
        for pt in curve_yes:
            if pt.cum_size <= max_size:
                candidate_sizes.add(pt.cum_size)
        for pt in curve_no:
            if pt.cum_size <= max_size:
                candidate_sizes.add(pt.cum_size)
        candidate_sizes.add(max_size)

        best: ArbOpportunity | None = None

        for size in sorted(candidate_sizes):
            if size <= 0:
                continue
            cost_y = _cost_for_size(curve_yes, size)
            cost_n = _cost_for_size(curve_no, size)
            if cost_y is None or cost_n is None:
                continue

            gross = size - cost_y - cost_n  # cada par redime US$ 1
            if gross <= 0:
                continue

            notional = cost_y + cost_n
            var = self.cost_model.variable_cost(notional)
            net = gross - var.total
            if net <= 0:
                continue

            net_pct = (net / notional) * 100.0 if notional > 0 else 0.0
            if net_pct < self.min_net_profit_pct:
                continue

            opp = ArbOpportunity(
                market=market,
                yes_book=yes_book,
                no_book=no_book,
                size_shares=size,
                avg_ask_yes=cost_y / size,
                avg_ask_no=cost_n / size,
                gross_pnl_usdc=gross,
                variable_cost=var,
            )
            if best is None or opp.net_pnl_usdc > best.net_pnl_usdc:
                best = opp

        return best
