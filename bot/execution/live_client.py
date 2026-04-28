"""Abstracao de cliente para execucao real.

A interface `LiveClient` desacopla a logica do LiveTrader da
implementacao especifica do `py-clob-client`, permitindo:
- Mockar pra tests sem rede.
- Trocar implementacoes (ex.: outro venue) sem mudar callers.

`PolymarketLiveClient` eh a implementacao real - chama
py-clob-client em threads (a lib eh sincrona).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class OrderResponse:
    """Resultado uniforme de submissao de ordem."""

    success: bool
    order_id: str | None
    filled_shares: float
    avg_fill_price: float | None
    fees_paid_usdc: float
    error: str | None = None
    raw: dict[str, Any] | None = None


@runtime_checkable
class LiveClient(Protocol):
    """Interface minima de execucao."""

    async def submit_buy_ioc(
        self, token_id: str, limit_price: float, size_shares: float
    ) -> OrderResponse: ...

    async def submit_sell_ioc(
        self, token_id: str, limit_price: float, size_shares: float
    ) -> OrderResponse: ...

    async def get_usdc_balance(self) -> float: ...


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_polymarket_response(
    raw: dict[str, Any], *, side: str, limit_price: float
) -> OrderResponse:
    """Mapeia o dict retornado por py-clob-client.create_and_post_order."""
    success = bool(raw.get("success", False))

    if not success:
        return OrderResponse(
            success=False,
            order_id=raw.get("orderID") or raw.get("order_id"),
            filled_shares=0.0,
            avg_fill_price=None,
            fees_paid_usdc=0.0,
            error=str(raw.get("errorMsg") or raw.get("error") or "rejected"),
            raw=raw,
        )

    making = _safe_float(raw.get("makingAmount") or raw.get("making_amount"))
    taking = _safe_float(raw.get("takingAmount") or raw.get("taking_amount"))
    fees = _safe_float(raw.get("feeRateBps")) or 0.0

    if side == "BUY":
        # Compramos shares (taking) gastando USDC (making)
        filled_shares = taking or 0.0
        avg_price = (making / taking) if (making and taking and taking > 0) else None
        notional = making or (filled_shares * (avg_price or limit_price))
    else:
        # Vendemos shares (making) recebendo USDC (taking)
        filled_shares = making or 0.0
        avg_price = (taking / making) if (taking and making and making > 0) else None
        notional = taking or (filled_shares * (avg_price or limit_price))

    fees_usdc = (notional or 0.0) * (fees / 10_000.0)

    return OrderResponse(
        success=True,
        order_id=raw.get("orderID") or raw.get("order_id"),
        filled_shares=filled_shares,
        avg_fill_price=avg_price,
        fees_paid_usdc=fees_usdc,
        raw=raw,
    )


@dataclass
class PolymarketLiveClient:
    """Implementacao real usando py-clob-client.

    NOTA: o cliente subjacente eh sincrono. Encapsulamos cada chamada em
    `asyncio.to_thread` pra nao bloquear o loop. Se voce quer alta
    concorrencia, considere reusar conexoes ou usar lib async nativa.

    A inicializacao requer chave privada e (opcionalmente) funder. Em
    producao, leia esses valores de Settings/.env - NAO hardcode.
    """

    host: str
    chain_id: int
    private_key: str
    funder: str | None = None
    signature_type: int = 0
    _client: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Import tardio: deixa o modulo importavel sem py-clob-client em PATH
        from py_clob_client.client import ClobClient

        self._client = ClobClient(
            host=self.host,
            chain_id=self.chain_id,
            key=self.private_key,
            signature_type=self.signature_type,
            funder=self.funder,
        )
        api_creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(api_creds)

    async def submit_buy_ioc(
        self, token_id: str, limit_price: float, size_shares: float
    ) -> OrderResponse:
        return await self._submit(token_id, limit_price, size_shares, "BUY")

    async def submit_sell_ioc(
        self, token_id: str, limit_price: float, size_shares: float
    ) -> OrderResponse:
        return await self._submit(token_id, limit_price, size_shares, "SELL")

    async def _submit(
        self, token_id: str, limit_price: float, size_shares: float, side: str
    ) -> OrderResponse:
        from py_clob_client.clob_types import OrderArgs, OrderType

        args = OrderArgs(
            token_id=token_id,
            price=limit_price,
            size=size_shares,
            side=side,
        )

        try:
            raw = await asyncio.to_thread(
                self._client.create_and_post_order, args, OrderType.FAK
            )
        except Exception as exc:  # noqa: BLE001
            return OrderResponse(
                success=False,
                order_id=None,
                filled_shares=0.0,
                avg_fill_price=None,
                fees_paid_usdc=0.0,
                error=f"client_error: {exc}",
            )

        if not isinstance(raw, dict):
            return OrderResponse(
                success=False,
                order_id=None,
                filled_shares=0.0,
                avg_fill_price=None,
                fees_paid_usdc=0.0,
                error=f"unexpected_response_type: {type(raw).__name__}",
                raw=None,
            )

        return _parse_polymarket_response(raw, side=side, limit_price=limit_price)

    async def get_usdc_balance(self) -> float:
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        raw = await asyncio.to_thread(self._client.get_balance_allowance, params)
        if isinstance(raw, dict):
            balance = _safe_float(raw.get("balance"))
            if balance is not None:
                # USDC tem 6 decimais; alguns retornos vem em wei
                if balance > 1e6:  # heuristica simples
                    return balance / 1_000_000.0
                return balance
        return 0.0
