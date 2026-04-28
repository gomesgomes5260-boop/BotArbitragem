"""Paper trader - simula execucao IOC simultanea de YES + NO contra os
books reais, sem submeter ordens.

Mecanica:
1. Determina target_shares = min(opportunity.size_shares, capital / cost_per_share).
2. Simula fill em cada lado: caminha asks ordenadas por preco; preenche
   ate target_shares ou esgotar book.
3. Calcula leg_status: both_filled / partial_yes / partial_no /
   partial_both / failed.
4. Se houver residuo (qtd YES != qtd NO), simula fechamento ao melhor
   bid do lado em excesso. Pessimista mas honesto.
5. Custos: fees taker em ambas pernas + (se residuo) fees na venda; gas
   = N txs base + 1 extra se houver venda de residuo.
6. P&L liquido = receita_total - custo_total - fees - gas.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from bot.clients.models import BookLevel
from bot.data.opportunity_store import OpportunityStore
from bot.economics.cost_model import CostModel
from bot.strategies.intra_market import ArbOpportunity


LEG_STATUS_BOTH_FILLED = "both_filled"
LEG_STATUS_PARTIAL_YES = "partial_yes"   # YES parcial, NO completo
LEG_STATUS_PARTIAL_NO = "partial_no"     # NO parcial, YES completo
LEG_STATUS_PARTIAL_BOTH = "partial_both"
LEG_STATUS_FAILED = "failed"


@dataclass
class LegFill:
    """Resultado da simulacao de uma perna."""

    side: str  # 'yes' | 'no'
    requested_shares: float
    filled_shares: float
    avg_fill_price: float
    usdc_spent: float

    @property
    def fully_filled(self) -> bool:
        return self.filled_shares >= self.requested_shares - 1e-9


@dataclass
class PaperTradeResult:
    opportunity: ArbOpportunity
    yes_fill: LegFill
    no_fill: LegFill
    leg_status: str
    matched_shares: float
    residual_yes_shares: float
    residual_no_shares: float
    residual_close_value: float       # USDC recuperado fechando residuo
    fees_paid_usdc: float
    gas_paid_usdc: float
    realized_gross_pnl_usdc: float    # = receita - custo (antes de fees/gas)
    net_pnl_usdc: float

    @property
    def total_usdc_spent(self) -> float:
        return self.yes_fill.usdc_spent + self.no_fill.usdc_spent

    @property
    def has_residual(self) -> bool:
        return self.residual_yes_shares > 1e-9 or self.residual_no_shares > 1e-9


def _walk_asks(asks: list[BookLevel], target_shares: float) -> tuple[float, float]:
    """Caminha asks ordenadas por preco asc, preenche ate target_shares.

    Retorna (filled_shares, total_cost_usdc).
    """
    if target_shares <= 0:
        return 0.0, 0.0
    sorted_asks = sorted(asks, key=lambda lvl: lvl.price)
    filled = 0.0
    cost = 0.0
    remaining = target_shares
    for lvl in sorted_asks:
        if remaining <= 1e-9:
            break
        if lvl.size <= 0 or lvl.price <= 0:
            continue
        take = min(lvl.size, remaining)
        filled += take
        cost += take * lvl.price
        remaining -= take
    return filled, cost


def _classify_status(
    yes_filled: float, no_filled: float, target_shares: float
) -> str:
    yes_ok = yes_filled >= target_shares - 1e-9
    no_ok = no_filled >= target_shares - 1e-9
    if yes_filled <= 1e-9 and no_filled <= 1e-9:
        return LEG_STATUS_FAILED
    if yes_ok and no_ok:
        return LEG_STATUS_BOTH_FILLED
    if yes_ok and not no_ok:
        return LEG_STATUS_PARTIAL_NO
    if not yes_ok and no_ok:
        return LEG_STATUS_PARTIAL_YES
    return LEG_STATUS_PARTIAL_BOTH


class PaperTrader:
    """Simula execucao em paper mode contra um snapshot do book."""

    def __init__(
        self,
        cost_model: CostModel,
        store: OpportunityStore | None = None,
        *,
        capital_per_trade_usdc: float = 20.0,
    ) -> None:
        self.cost_model = cost_model
        self.store = store
        self.capital_per_trade_usdc = capital_per_trade_usdc

    def execute(
        self, opp: ArbOpportunity, *, opportunity_id: int | None = None
    ) -> PaperTradeResult:
        target_shares = self._size_for_capital(opp)

        yes_filled, yes_cost = _walk_asks(opp.yes_book.asks, target_shares)
        no_filled, no_cost = _walk_asks(opp.no_book.asks, target_shares)

        yes_fill = LegFill(
            side="yes",
            requested_shares=target_shares,
            filled_shares=yes_filled,
            avg_fill_price=(yes_cost / yes_filled) if yes_filled > 0 else 0.0,
            usdc_spent=yes_cost,
        )
        no_fill = LegFill(
            side="no",
            requested_shares=target_shares,
            filled_shares=no_filled,
            avg_fill_price=(no_cost / no_filled) if no_filled > 0 else 0.0,
            usdc_spent=no_cost,
        )

        status = _classify_status(yes_filled, no_filled, target_shares)
        matched = min(yes_filled, no_filled)
        residual_yes = yes_filled - matched
        residual_no = no_filled - matched

        residual_value = self._close_residuals(opp, residual_yes, residual_no)

        usdc_spent = yes_cost + no_cost
        # Pares matched redimem em $1 cada na liquidacao do mercado.
        usdc_received = matched * 1.0 + residual_value
        gross = usdc_received - usdc_spent

        has_residual = residual_yes > 1e-9 or residual_no > 1e-9
        n_txs = self.cost_model.txs_per_arb + (1 if has_residual else 0)
        gas_estimate = self.cost_model.avg_gas_polygon_usd * n_txs
        # Notional inclui buys + sells de residuo (taker em ambas).
        var_cost = self.cost_model.variable_cost(
            usdc_spent + residual_value, gas_usdc=gas_estimate
        )
        fees = var_cost.fees_usdc
        gas = var_cost.gas_usdc
        net = gross - fees - gas

        result = PaperTradeResult(
            opportunity=opp,
            yes_fill=yes_fill,
            no_fill=no_fill,
            leg_status=status,
            matched_shares=matched,
            residual_yes_shares=residual_yes,
            residual_no_shares=residual_no,
            residual_close_value=residual_value,
            fees_paid_usdc=fees,
            gas_paid_usdc=gas,
            realized_gross_pnl_usdc=gross,
            net_pnl_usdc=net,
        )

        if self.store is not None:
            try:
                self.store.insert_trade(result, opportunity_id=opportunity_id, mode="paper")
            except Exception as exc:  # noqa: BLE001
                logger.error("Falha ao persistir paper trade: {}", exc)

        return result

    def _size_for_capital(self, opp: ArbOpportunity) -> float:
        """Tamanho alvo em shares respeitando capital_per_trade_usdc."""
        cost_per_share = opp.avg_ask_yes + opp.avg_ask_no
        if cost_per_share <= 0:
            return 0.0
        max_shares_capital = self.capital_per_trade_usdc / cost_per_share
        return min(opp.size_shares, max_shares_capital)

    @staticmethod
    def _close_residuals(opp: ArbOpportunity, residual_yes: float, residual_no: float) -> float:
        """Simula venda de residuo no melhor bid (pessimista)."""
        value = 0.0
        if residual_yes > 1e-9:
            best_bid = opp.yes_book.best_bid
            value += residual_yes * (best_bid.price if best_bid else 0.0)
        if residual_no > 1e-9:
            best_bid = opp.no_book.best_bid
            value += residual_no * (best_bid.price if best_bid else 0.0)
        return value
