"""Testes do ClobWsClient (apenas a montagem da subscription, sem rede)."""
from __future__ import annotations

import pytest

from bot.clients.polymarket_ws import ClobWsClient


def test_subscription_payload_shape() -> None:
    sub = ClobWsClient._build_subscription(["a", "b", "c"])
    assert sub["type"] == "market"
    assert sub["assets_ids"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_events_raises_on_empty() -> None:
    ws = ClobWsClient()
    with pytest.raises(ValueError):
        async for _ in ws.events([]):
            break
