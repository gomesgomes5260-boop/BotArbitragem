"""Cliente HTTP minimo para a Notion API v1.

Apenas os endpoints usados pelo dashboard:
- POST /pages          (criar entrada em database)
- PATCH /blocks/{id}/children   (anexar conteudo a pagina)
- GET   /blocks/{id}/children   (listar conteudo de pagina)
- DELETE /blocks/{id}           (remover bloco)

Caracteristicas:
- Auth via Bearer token + header Notion-Version.
- Throttle simples: respeita teto de 3 req/s (rate limit publico).
- Retry em 429 e 5xx com Retry-After / backoff exponencial.

Sem dependencias alem de httpx (ja no projeto).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from loguru import logger


_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 4
_RETRY_BASE_DELAY = 1.0  # segundos


class NotionAPIError(Exception):
    """Erro retornado pela API do Notion (4xx/5xx nao recuperavel)."""

    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(f"Notion API {status}: {message}")
        self.status = status
        self.message = message
        self.payload = payload


class _RateLimiter:
    """Token bucket trivial (3 req/s)."""

    def __init__(self, max_per_second: float = 3.0) -> None:
        self._min_interval = 1.0 / max_per_second
        self._last_call = 0.0

    async def acquire(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        api_version: str = "2022-06-28",
        api_base: str = "https://api.notion.com/v1",
        client: httpx.AsyncClient | None = None,
        max_per_second: float = 3.0,
    ) -> None:
        self._token = token
        self._api_version = api_version
        self._api_base = api_base.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        self._owns_client = client is None
        self._limiter = _RateLimiter(max_per_second=max_per_second)

    async def __aenter__(self) -> "NotionClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._api_version,
            "Content-Type": "application/json",
        }

    async def _request(
        self, method: str, path: str, *, json: dict | None = None
    ) -> dict:
        url = f"{self._api_base}{path}"
        for attempt in range(_MAX_RETRIES + 1):
            await self._limiter.acquire()
            try:
                resp = await self._client.request(
                    method, url, headers=self._headers, json=json
                )
            except httpx.HTTPError as exc:
                if attempt >= _MAX_RETRIES:
                    raise NotionAPIError(0, f"transport error: {exc}") from exc
                delay = _RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "notion transport error attempt={} delay={:.1f}s err={}",
                    attempt + 1, delay, exc,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= _MAX_RETRIES:
                    raise NotionAPIError(
                        resp.status_code, resp.text or "rate-limited/server-error"
                    )
                retry_after_hdr = resp.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_hdr) if retry_after_hdr else 0.0
                except ValueError:
                    retry_after = 0.0
                delay = max(retry_after, _RETRY_BASE_DELAY * (2**attempt))
                logger.warning(
                    "notion {} attempt={} delay={:.1f}s",
                    resp.status_code, attempt + 1, delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                    msg = payload.get("message", resp.text)
                except Exception:
                    payload = None
                    msg = resp.text
                raise NotionAPIError(resp.status_code, msg, payload)

            try:
                return resp.json()
            except ValueError:
                return {}

        raise NotionAPIError(0, "exhausted retries")

    async def create_database_page(
        self,
        database_id: str,
        properties: dict[str, Any],
        *,
        children: list[dict] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        if children:
            body["children"] = children
        return await self._request("POST", "/pages", json=body)

    async def create_database(
        self, parent_page_id: str, title: str, properties: dict[str, Any]
    ) -> dict:
        return await self._request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": properties,
            },
        )

    async def create_subpage(
        self,
        parent_page_id: str,
        title: str,
        *,
        emoji: str | None = None,
        children: list[dict] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": [{"type": "text", "text": {"content": title}}]
            },
        }
        if emoji:
            body["icon"] = {"type": "emoji", "emoji": emoji}
        if children:
            body["children"] = children
        return await self._request("POST", "/pages", json=body)

    async def list_block_children(
        self, block_id: str, *, page_size: int = 100
    ) -> dict:
        return await self._request(
            "GET",
            f"/blocks/{block_id}/children?page_size={page_size}",
        )

    async def append_block_children(
        self, block_id: str, children: list[dict]
    ) -> dict:
        return await self._request(
            "PATCH",
            f"/blocks/{block_id}/children",
            json={"children": children},
        )

    async def delete_block(self, block_id: str) -> dict:
        return await self._request("DELETE", f"/blocks/{block_id}")

    async def replace_page_content(
        self, page_id: str, children: list[dict]
    ) -> None:
        """Substitui o conteudo da pagina: deleta blocos existentes (top-level)
        e anexa os novos. Idempotente."""
        existing = await self.list_block_children(page_id)
        for block in existing.get("results", []):
            block_id = block.get("id")
            if block_id:
                try:
                    await self.delete_block(block_id)
                except NotionAPIError as exc:
                    logger.warning("nao consegui deletar bloco {}: {}", block_id, exc)
        # Notion limita 100 children por request
        for i in range(0, len(children), 100):
            chunk = children[i : i + 100]
            await self.append_block_children(page_id, chunk)
