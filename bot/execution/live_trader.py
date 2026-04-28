"""LiveTrader - executa arbs reais via LiveClient, com gates de risco.

Fluxo:
1. Calcula target_shares respeitando capital cap.
2. Le snapshot de risco (saldo, exposure de hoje, P&L do dia).
3. RiskManager.check_pre_trade(...) -> se nao permitido, recusa.
4. Submete YES + NO IOC concorrentemente.
5. Calcula match e residual; se houver residual, submete close (IOC sell).
6. Persiste em `trades` com mode='live'.
7. RiskManager.record_outcome(...) - aciona kill-switch se preciso.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from bot.data.opportunity_store import OpportunityStore
from bot.economics.cost_model import CostModel
from bot.execution.live_client import LiveClient, OrderResponse
from bot.execution.paper_trader import (
    LEG_STATUS_BOTH_FILLED,
    LEG_STATUS_FAILED,
    LEG_STATUS_PARTIAL_BOTH,
    LEG_STATUS_PARTIAL_NO,
    LEG_STATUS_PARTIAL_YES,
    LegFill,
    PaperTradeResult,
)
from bot.risk.limits import RiskCheckResult, RiskManager, RiskSnapshot
from bot.strategies.intra_market import ArbOpportunity


@dataclass
class LiveTradeOutcome:
    """Resultado de uma tentativa de execucao real."""

    submitted: bool
    risk_reason: str | None
    trade: PaperTradeResult | None         # None se foi rejeitada por risco
    yes_response: OrderResponse | None = None
    no_response: OrderResponse | None = None
    residual_response: OrderResponse | None = None


def _today_utc_start_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )


def get_today_live_pnl(store: OpportunityStore) -> float:
    agg = store.aggregate_trades(mode="live", since_iso=_today_utc_start_iso())
    return float(agg["net_pnl_usdc"])


def get_today_live_exposure(store: OpportunityStore) -> float:
    agg = store.aggregate_trades(mode="live", since_iso=_today_utc_start_iso())
    return float(agg["notional_usdc"])


class LiveTrader:
    def __init__(
        self,
        client: LiveClient,
        cost_model: CostModel,
        store: OpportunityStore,
        risk_manager: RiskManager,
        *,
        capital_per_trade_usdc: float,
    ) -> None:
        self.client = client
        self.cost_model = cost_model
        self.store = store
        self.risk_manager = risk_manager
        self.capital_per_trade_usdc = capital_per_trade_usdc

    async def execute(
        self, opp: ArbOpportunity, *, opportunity_id: int | None = None
    ) -> LiveTradeOutcome:
        target_shares = self._size_for_capital(opp)
        intended_size_usdc = target_shares * (opp.avg_ask_yes + opp.avg_ask_no)

        balance = await self._get_balance_safely()
        snap = RiskSnapshot(
            intended_size_usdc=intended_size_usdc,
            current_open_exposure_usdc=get_today_live_exposure(self.store),
            today_realized_pnl_usdc=get_today_live_pnl(self.store),
            usdc_balance=balance,
        )

        risk = self.risk_manager.check_pre_trade(snap)
        if not risk.allowed:
            logger.warning("Trade rejeitado por risco: {}", risk.reason)
            return LiveTradeOutcome(
                submitted=False, risk_reason=risk.reason, trade=None
            )

        yes_id = opp.market.yes_token_id
        no_id = opp.market.no_token_id
        if not yes_id or not no_id:
            return LiveTradeOutcome(
                submitted=False, risk_reason="market_token_ids_missing", trade=None
            )

        # Limite de preco = melhor ask disponivel (worst-case que a gente
        # detectou). Se o book moveu, IOC com esse preco fica unmatched.
        yes_price = max(opp.best_ask_yes, opp.avg_ask_yes)
        no_price = max(opp.best_ask_no, opp.avg_ask_no)

        import asyncio as _asyncio  # noqa: PLC0415

        yes_resp, no_resp = await _asyncio.gather(
            self.client.submit_buy_ioc(yes_id, yes_price, target_shares),
            self.client.submit_buy_ioc(no_id, no_price, target_shares),
        )

        yes_filled = yes_resp.filled_shares if yes_resp.success else 0.0
        no_filled = no_resp.filled_shares if no_resp.success else 0.0
        yes_avg = yes_resp.avg_fill_price or 0.0
        no_avg = no_resp.avg_fill_price or 0.0
        yes_cost = yes_filled * yes_avg
        no_cost = no_filled * no_avg

        leg_status = self._classify_status(yes_filled, no_filled, target_shares)
        matched = min(yes_filled, no_filled)
        residual_yes = yes_filled - matched
        residual_no = no_filled - matched

        residual_response: OrderResponse | None = None
        residual_value = 0.0
        residual_fees = 0.0

        if residual_yes > 1e-9 and yes_id is not None:
            best_bid = opp.yes_book.best_bid
            if best_bid is not None and best_bid.price > 0:
                residual_response = await self.client.submit_sell_ioc(
                    yes_id, best_bid.price, residual_yes
                )
                if residual_response.success:
                    residual_value = (
                        residual_response.filled_shares
                        * (residual_response.avg_fill_price or best_bid.price)
                    )
                    residual_fees = residual_response.fees_paid_usdc
        elif residual_no > 1e-9 and no_id is not None:
            best_bid = opp.no_book.best_bid
            if best_bid is not None and best_bid.price > 0:
                residual_response = await self.client.submit_sell_ioc(
                    no_id, best_bid.price, residual_no
                )
                if residual_response.success:
                    residual_value = (
                        residual_response.filled_shares
                        * (residual_response.avg_fill_price or best_bid.price)
                    )
                    residual_fees = residual_response.fees_paid_usdc

        usdc_spent = yes_cost + no_cost
        usdc_received = matched * 1.0 + residual_value
        gross = usdc_received - usdc_spent

        # Fees reais retornados pelos responses + estimativa de gas
        real_fees = yes_resp.fees_paid_usdc + no_resp.fees_paid_usdc + residual_fees
        n_txs = self.cost_model.txs_per_arb + (1 if residual_response else 0)
        gas_estimate = self.cost_model.avg_gas_polygon_usd * n_txs
        net = gross - real_fees - gas_estimate

        result = PaperTradeResult(
            opportunity=opp,
            yes_fill=LegFill(
                side="yes",
                requested_shares=target_shares,
                filled_shares=yes_filled,
                avg_fill_price=yes_avg,
                usdc_spent=yes_cost,
            ),
            no_fill=LegFill(
                side="no",
                requested_shares=target_shares,
                filled_shares=no_filled,
                avg_fill_price=no_avg,
                usdc_spent=no_cost,
            ),
            leg_status=leg_status,
            matched_shares=matched,
            residual_yes_shares=residual_yes,
            residual_no_shares=residual_no,
            residual_close_value=residual_value,
            fees_paid_usdc=real_fees,
            gas_paid_usdc=gas_estimate,
            realized_gross_pnl_usdc=gross,
            net_pnl_usdc=net,
        )

        try:
            self.store.insert_trade(result, opportunity_id=opportunity_id, mode="live")
        except Exception as exc:  # noqa: BLE001
            logger.error("Falha ao persistir live trade: {}", exc)

        # Pos-trade: atualiza P&L diario e dispara kill-switch se necessario
        self.risk_manager.record_outcome(get_today_live_pnl(self.store))

        return LiveTradeOutcome(
            submitted=True,
            risk_reason=None,
            trade=result,
            yes_response=yes_resp,
            no_response=no_resp,
            residual_response=residual_response,
        )

    def _size_for_capital(self, opp: ArbOpportunity) -> float:
        cost_per_share = opp.avg_ask_yes + opp.avg_ask_no
        if cost_per_share <= 0:
            return 0.0
        max_shares_capital = self.capital_per_trade_usdc / cost_per_share
        return min(opp.size_shares, max_shares_capital)

    @staticmethod
    def _classify_status(yes_filled: float, no_filled: float, target: float) -> str:
        yes_ok = yes_filled >= target - 1e-9
        no_ok = no_filled >= target - 1e-9
        if yes_filled <= 1e-9 and no_filled <= 1e-9:
            return LEG_STATUS_FAILED
        if yes_ok and no_ok:
            return LEG_STATUS_BOTH_FILLED
        if yes_ok and not no_ok:
            return LEG_STATUS_PARTIAL_NO
        if not yes_ok and no_ok:
            return LEG_STATUS_PARTIAL_YES
        return LEG_STATUS_PARTIAL_BOTH

    async def _get_balance_safely(self) -> float:
        try:
            return await self.client.get_usdc_balance()
        except Exception as exc:  # noqa: BLE001
            logger.error("Falha ao ler saldo USDC: {}", exc)
            return 0.0
