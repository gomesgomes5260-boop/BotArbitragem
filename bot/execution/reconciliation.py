"""Reconciliacao: compara estado local vs estado on-chain.

Versao Fase 5 minima: confere que o saldo USDC reportado pelo client
nao esta muito abaixo do que o bot acha que deveria ter (notional gasto
em trades de hoje).

Casos detectados:
- Saldo on-chain MUITO menor que o esperado: alguma coisa drenou a
  carteira fora do bot. Acionar kill-switch.
- Saldo MAIOR que esperado: positivo, mas alerta (alguem depositou ou
  trade fechou e a gente nao registrou).

Limitacoes (resolver em fases posteriores):
- Nao confere tokens de outcome (YES/NO) on-chain - precisa de
  subgraph ou contract calls.
- Tolerancia de drift configuravel em USDC absoluto, nao bps.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from bot.data.opportunity_store import OpportunityStore
from bot.execution.live_client import LiveClient
from bot.execution.live_trader import _today_utc_start_iso
from bot.risk.limits import RiskManager


@dataclass
class ReconciliationResult:
    on_chain_balance: float
    expected_min_balance: float
    drift_usdc: float          # on_chain - expected
    ok: bool
    reason: str | None = None


class Reconciliator:
    def __init__(
        self,
        client: LiveClient,
        store: OpportunityStore,
        risk_manager: RiskManager,
        *,
        starting_balance_usdc: float,
        tolerance_usdc: float = 1.0,
    ) -> None:
        self.client = client
        self.store = store
        self.risk_manager = risk_manager
        self.starting_balance_usdc = starting_balance_usdc
        self.tolerance_usdc = tolerance_usdc

    async def check(self) -> ReconciliationResult:
        try:
            balance = await self.client.get_usdc_balance()
        except Exception as exc:  # noqa: BLE001
            logger.error("Reconciliacao falhou ao ler saldo: {}", exc)
            return ReconciliationResult(
                on_chain_balance=0.0,
                expected_min_balance=0.0,
                drift_usdc=0.0,
                ok=False,
                reason=f"balance_fetch_error: {exc}",
            )

        agg_today = self.store.aggregate_trades(
            mode="live", since_iso=_today_utc_start_iso()
        )
        expected_min = (
            self.starting_balance_usdc
            - float(agg_today["notional_usdc"])
            + float(agg_today["net_pnl_usdc"])
        )

        drift = balance - expected_min
        if drift < -self.tolerance_usdc:
            reason = (
                f"unexpected_drain: on_chain={balance:.2f} < expected_min={expected_min:.2f}"
                f" drift={drift:.2f} (tolerance={self.tolerance_usdc:.2f})"
            )
            logger.error("Reconciliacao detectou problema: {}", reason)
            self.risk_manager.kill_switch.trigger(reason)
            return ReconciliationResult(
                on_chain_balance=balance,
                expected_min_balance=expected_min,
                drift_usdc=drift,
                ok=False,
                reason=reason,
            )

        return ReconciliationResult(
            on_chain_balance=balance,
            expected_min_balance=expected_min,
            drift_usdc=drift,
            ok=True,
        )
