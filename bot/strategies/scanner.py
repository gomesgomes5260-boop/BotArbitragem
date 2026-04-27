"""Orquestra fetch de books + detector + persistencia.

Mantem a CLI fina e a logica testavel sem invocar typer.
"""
from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from bot.clients.models import Market, OrderBook
from bot.clients.polymarket_clob import ClobClient
from bot.clients.polymarket_gamma import GammaClient
from bot.data.market_cache import MarketCache
from bot.data.opportunity_store import OpportunityRow, OpportunityStore
from bot.strategies.intra_market import ArbOpportunity, IntraMarketDetector


@dataclass
class ScanResult:
    markets_scanned: int
    opportunities_found: int
    opportunities: list[ArbOpportunity]


def _opportunity_to_row(opp: ArbOpportunity) -> OpportunityRow:
    return OpportunityRow(
        market_id=opp.market.id,
        condition_id=opp.market.condition_id,
        market_question=opp.market.question,
        ask_yes=opp.best_ask_yes,
        ask_no=opp.best_ask_no,
        size_max_usdc=opp.notional_usdc,
        size_max_shares=opp.size_shares,
        gross_pnl_usdc=opp.gross_pnl_usdc,
        estimated_fees_usdc=opp.variable_cost.fees_usdc,
        estimated_gas_usdc=opp.variable_cost.gas_usdc,
    )


class IntraMarketScanner:
    """Faz uma 'passada' completa: lista mercados, busca books, detecta, salva."""

    def __init__(
        self,
        gamma: GammaClient,
        clob: ClobClient,
        detector: IntraMarketDetector,
        store: OpportunityStore | None = None,
        *,
        cache: MarketCache | None = None,
    ) -> None:
        self.gamma = gamma
        self.clob = clob
        self.detector = detector
        self.store = store
        self.cache = cache or MarketCache()

    async def refresh_cache(self, *, min_volume: float, max_pages: int = 5) -> int:
        self.cache.min_volume = min_volume
        self.cache.max_pages = max_pages
        return await self.cache.refresh(self.gamma)

    async def scan_once(self, *, max_markets: int | None = None) -> ScanResult:
        markets = self.cache.markets
        markets.sort(key=lambda m: m.best_volume, reverse=True)
        if max_markets is not None:
            markets = markets[:max_markets]

        if not markets:
            return ScanResult(markets_scanned=0, opportunities_found=0, opportunities=[])

        token_ids: list[str] = []
        for m in markets:
            token_ids.extend(m.clob_token_ids)

        try:
            books = await self.clob.get_books(token_ids)
        except Exception as exc:  # noqa: BLE001
            logger.error("Falha ao buscar books em batch: {}", exc)
            return ScanResult(markets_scanned=0, opportunities_found=0, opportunities=[])

        books_by_id: dict[str, OrderBook] = {b.asset_id: b for b in books}

        found: list[ArbOpportunity] = []
        for market in markets:
            yes_id = market.yes_token_id
            no_id = market.no_token_id
            if yes_id is None or no_id is None:
                continue
            yb = books_by_id.get(yes_id)
            nb = books_by_id.get(no_id)
            if yb is None or nb is None:
                continue
            opp = self.detector.detect(market, yb, nb)
            if opp is None:
                continue
            found.append(opp)
            if self.store is not None:
                try:
                    self.store.insert_opportunity(_opportunity_to_row(opp))
                except Exception as exc:  # noqa: BLE001
                    logger.error("Falha ao salvar oportunidade {}: {}", market.id, exc)

        return ScanResult(
            markets_scanned=len(markets),
            opportunities_found=len(found),
            opportunities=found,
        )


def format_opportunity_line(opp: ArbOpportunity) -> str:
    return (
        f"  [{opp.net_pnl_pct:>5.2f}%]  "
        f"net=${opp.net_pnl_usdc:>7.2f}  "
        f"size=${opp.notional_usdc:>9.2f}  "
        f"yes={opp.avg_ask_yes:.4f} no={opp.avg_ask_no:.4f}  "
        f"fees=${opp.variable_cost.fees_usdc:.3f} gas=${opp.variable_cost.gas_usdc:.3f}  "
        f"{opp.market.question[:60]}"
    )
