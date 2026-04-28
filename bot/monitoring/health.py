"""Health checks - usado pelo live mode pra pausar exec se algo quebra.

Cada check eh independente e retorna ok/reason. O agregador `run_all`
roda todos e devolve um snapshot consumivel pelo orquestrador (live).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger


@dataclass(frozen=True)
class HealthCheck:
    name: str
    ok: bool
    detail: str | None = None


@dataclass(frozen=True)
class HealthReport:
    checks: list[HealthCheck]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok and c.detail]


CheckFn = Callable[[], Awaitable[HealthCheck]]


async def run_all(checks: list[CheckFn]) -> HealthReport:
    results: list[HealthCheck] = []
    for fn in checks:
        try:
            results.append(await fn())
        except Exception as exc:  # noqa: BLE001
            logger.error("Health check exploded: {}", exc)
            results.append(HealthCheck(name=fn.__name__, ok=False, detail=str(exc)))
    return HealthReport(checks=results)


def make_balance_check(client, *, min_balance: float) -> CheckFn:
    async def _check() -> HealthCheck:
        try:
            bal = await client.get_usdc_balance()
        except Exception as exc:  # noqa: BLE001
            return HealthCheck(name="usdc_balance", ok=False, detail=f"error: {exc}")
        if bal < min_balance:
            return HealthCheck(
                name="usdc_balance", ok=False, detail=f"{bal:.2f} < min {min_balance:.2f}"
            )
        return HealthCheck(name="usdc_balance", ok=True, detail=f"{bal:.2f}")

    return _check


def make_kill_switch_check(kill_switch) -> CheckFn:
    async def _check() -> HealthCheck:
        if kill_switch.is_active():
            return HealthCheck(
                name="kill_switch", ok=False, detail=kill_switch.reason() or "active"
            )
        return HealthCheck(name="kill_switch", ok=True)

    return _check
