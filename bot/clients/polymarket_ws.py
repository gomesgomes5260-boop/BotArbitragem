"""Cliente WebSocket para o canal `market` do CLOB da Polymarket.

Recebe eventos de book / price_change / tick_size_change em tempo real.
Auto-reconecta com backoff exponencial.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import websockets
from loguru import logger

DEFAULT_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_PING_INTERVAL = 20.0
DEFAULT_PING_TIMEOUT = 10.0
DEFAULT_RECONNECT_BASE = 1.0
DEFAULT_RECONNECT_MAX = 30.0


WsHandler = Callable[[dict[str, Any]], Awaitable[None]]


class ClobWsClient:
    """Conecta ao WS publico de market data do CLOB.

    Uso simples (callback):
        client = ClobWsClient()
        await client.subscribe(token_ids, on_event=handler)

    Ou consumindo via iterador:
        async for event in client.events(token_ids):
            ...
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        ping_timeout: float = DEFAULT_PING_TIMEOUT,
    ) -> None:
        self._url = url
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout

    @staticmethod
    def _build_subscription(token_ids: list[str]) -> dict[str, Any]:
        return {"type": "market", "assets_ids": list(token_ids)}

    async def events(
        self,
        token_ids: list[str],
        *,
        max_messages: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Itera eventos do book. Reconecta sozinho em caso de queda.

        max_messages: util pra testes ou para o comando watch-book com timeout.
        """
        if not token_ids:
            raise ValueError("token_ids vazio")

        sub_msg = json.dumps(self._build_subscription(token_ids))
        backoff = DEFAULT_RECONNECT_BASE
        msg_count = 0

        while True:
            try:
                logger.debug("Conectando WS em {}", self._url)
                async with websockets.connect(
                    self._url,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                ) as ws:
                    await ws.send(sub_msg)
                    logger.debug("Subscrito em {} token(s)", len(token_ids))
                    backoff = DEFAULT_RECONNECT_BASE  # reset apos sucesso

                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("Mensagem WS nao-JSON ignorada: {}", raw[:80])
                            continue

                        # Polymarket as vezes envia listas de eventos
                        if isinstance(data, list):
                            for item in data:
                                if isinstance(item, dict):
                                    yield item
                                    msg_count += 1
                                    if max_messages is not None and msg_count >= max_messages:
                                        return
                        elif isinstance(data, dict):
                            yield data
                            msg_count += 1
                            if max_messages is not None and msg_count >= max_messages:
                                return
            except (websockets.WebSocketException, OSError, asyncio.TimeoutError) as exc:
                logger.warning("WS desconectado ({}). Reconectando em {:.1f}s", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, DEFAULT_RECONNECT_MAX)

    async def subscribe(
        self,
        token_ids: list[str],
        *,
        on_event: WsHandler,
        max_messages: int | None = None,
    ) -> None:
        async for event in self.events(token_ids, max_messages=max_messages):
            await on_event(event)
