"""Testes do NotionClient. Usa MockTransport do httpx pra evitar rede."""
from __future__ import annotations

import json

import httpx
import pytest

from bot.monitoring.notion_client import NotionAPIError, NotionClient


def _make_client(handler) -> NotionClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return NotionClient("token-xyz", client=http, max_per_second=1000.0)


@pytest.mark.asyncio
async def test_create_database_page_sends_correct_body() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200, json={"object": "page", "id": "page-123"}
        )

    client = _make_client(handler)
    try:
        result = await client.create_database_page(
            "db-abc",
            {"Name": {"title": [{"type": "text", "text": {"content": "X"}}]}},
        )
    finally:
        await client.close()

    assert result["id"] == "page-123"
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/pages"
    body = json.loads(req.content)
    assert body["parent"] == {"database_id": "db-abc"}
    assert "Name" in body["properties"]
    assert req.headers["Authorization"] == "Bearer token-xyz"
    assert req.headers["Notion-Version"] == "2022-06-28"


@pytest.mark.asyncio
async def test_retries_on_429_and_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={"id": "ok"})

    client = _make_client(handler)
    try:
        out = await client.create_database_page("db", {})
    finally:
        await client.close()

    assert calls["n"] == 2
    assert out["id"] == "ok"


@pytest.mark.asyncio
async def test_raises_on_4xx_non_429() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"object": "error", "status": 400, "message": "bad property"},
        )

    client = _make_client(handler)
    try:
        with pytest.raises(NotionAPIError) as exc:
            await client.create_database_page("db", {})
    finally:
        await client.close()
    assert exc.value.status == 400
    assert "bad property" in str(exc.value)


@pytest.mark.asyncio
async def test_replace_page_content_deletes_then_appends() -> None:
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "GET" and "/blocks/" in req.url.path:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "blk-1", "type": "paragraph"},
                        {"id": "blk-2", "type": "paragraph"},
                    ],
                    "next_cursor": None,
                },
            )
        if req.method == "DELETE":
            return httpx.Response(200, json={"id": "deleted"})
        if req.method == "PATCH":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, text="unexpected")

    client = _make_client(handler)
    try:
        await client.replace_page_content(
            "page-abc",
            [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}],
        )
    finally:
        await client.close()

    methods = [m for m, _ in calls]
    assert methods.count("GET") == 1
    assert methods.count("DELETE") == 2  # 2 blocos antigos
    assert methods.count("PATCH") == 1


@pytest.mark.asyncio
async def test_create_database_helper() -> None:
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "db-new"})

    client = _make_client(handler)
    try:
        out = await client.create_database(
            "parent-page", "Trades", {"Name": {"title": {}}}
        )
    finally:
        await client.close()

    assert out["id"] == "db-new"
    body = captured[0]
    assert body["parent"]["page_id"] == "parent-page"
    assert body["title"][0]["text"]["content"] == "Trades"
    assert "Name" in body["properties"]
