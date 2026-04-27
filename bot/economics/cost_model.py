"""Modelo de custos do bot.

Custos divididos em duas categorias:
- Variaveis (por trade): fees da Polymarket + gas Polygon.
- Fixos (mensais): RPC + VPS + outros, amortizados por dia.

Lucro liquido = lucro_bruto - custo_variavel - rateio_de_custos_fixos.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.config.settings import Settings


@dataclass(frozen=True)
class VariableCost:
    """Custos atribuiveis a um trade especifico."""

    fees_usdc: float
    gas_usdc: float

    @property
    def total(self) -> float:
        return self.fees_usdc + self.gas_usdc


class CostModel:
    """Calcula custos de trade. Imutavel apos criacao.

    Fees aplicadas como taker em ambas as pernas (YES + NO via IOC) -
    consistente com a estrategia da Fase 4+.
    """

    def __init__(
        self,
        *,
        taker_fee_bps: int,
        maker_fee_bps: int,
        avg_gas_polygon_usd: float,
        txs_per_arb: int,
        monthly_rpc_usd: float,
        monthly_vps_usd: float,
        monthly_other_usd: float,
    ) -> None:
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps
        self.avg_gas_polygon_usd = avg_gas_polygon_usd
        self.txs_per_arb = txs_per_arb
        self.monthly_rpc_usd = monthly_rpc_usd
        self.monthly_vps_usd = monthly_vps_usd
        self.monthly_other_usd = monthly_other_usd

    @classmethod
    def from_settings(cls, settings: Settings) -> "CostModel":
        return cls(
            taker_fee_bps=settings.taker_fee_bps,
            maker_fee_bps=settings.maker_fee_bps,
            avg_gas_polygon_usd=settings.avg_gas_polygon_usd,
            txs_per_arb=settings.txs_per_arb,
            monthly_rpc_usd=settings.monthly_rpc_usd,
            monthly_vps_usd=settings.monthly_vps_usd,
            monthly_other_usd=settings.monthly_other_usd,
        )

    def variable_cost(
        self,
        notional_usdc: float,
        *,
        gas_usdc: float | None = None,
        all_taker: bool = True,
    ) -> VariableCost:
        """Custos de um trade com `notional_usdc` total (YES + NO somados).

        Args:
            notional_usdc: USDC total gasto no trade (custo YES + custo NO).
            gas_usdc: gas real, se ja conhecido. Se None, usa estimativa.
            all_taker: True = ambas pernas pagam taker fee (estrategia padrao).
                       False = ambas como maker (raro pra arb com IOC).
        """
        bps = self.taker_fee_bps if all_taker else self.maker_fee_bps
        fees = notional_usdc * (bps / 10_000.0)
        gas = gas_usdc if gas_usdc is not None else (self.avg_gas_polygon_usd * self.txs_per_arb)
        return VariableCost(fees_usdc=fees, gas_usdc=gas)

    @property
    def total_monthly_fixed_usd(self) -> float:
        return self.monthly_rpc_usd + self.monthly_vps_usd + self.monthly_other_usd

    @property
    def daily_fixed_cost_usd(self) -> float:
        return self.total_monthly_fixed_usd / 30.0

    def break_even_daily_pnl(self) -> float:
        """Lucro liquido diario minimo (apos custos variaveis) para pagar a infra."""
        return self.daily_fixed_cost_usd
