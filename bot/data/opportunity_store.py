"""Persistencia em SQLite das oportunidades de arbitragem detectadas.

Schema completo (Fase 2.5):
- opportunities: snapshot de cada oportunidade detectada (so log).
- trades: execucoes paper/live (preenchido a partir da Fase 4).
- daily_costs: custos fixos amortizados por dia.
- matic_price_history: snapshot diario da cotacao MATIC/USD.

Uso typico (Fase 2):
    store = OpportunityStore(Path("data/bot.db"))
    store.init_schema()
    store.insert_opportunity(opp)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    market_id TEXT NOT NULL,
    condition_id TEXT,
    market_question TEXT,
    ask_yes REAL NOT NULL,
    ask_no REAL NOT NULL,
    sum_asks REAL NOT NULL,
    size_max_usdc REAL NOT NULL,
    size_max_shares REAL NOT NULL,
    gross_pnl_usdc REAL NOT NULL,
    estimated_fees_usdc REAL NOT NULL,
    estimated_gas_usdc REAL NOT NULL,
    net_pnl_usdc REAL NOT NULL,
    net_pnl_pct REAL NOT NULL,
    duration_seconds REAL,
    was_traded INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_opportunities_market_ts
    ON opportunities (market_id, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_opportunities_ts
    ON opportunities (timestamp_utc);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER REFERENCES opportunities(id),
    timestamp_utc TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    market_id TEXT NOT NULL,
    size_usdc REAL NOT NULL,
    fill_price_yes REAL,
    fill_price_no REAL,
    realized_gross_pnl_usdc REAL,
    taker_fee_paid_usdc REAL,
    maker_fee_paid_usdc REAL,
    gas_paid_usdc REAL,
    matic_price_at_trade REAL,
    net_pnl_usdc REAL,
    leg_status TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades (timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades (mode);

CREATE TABLE IF NOT EXISTS daily_costs (
    date TEXT PRIMARY KEY,
    rpc_cost_usd REAL NOT NULL,
    vps_cost_usd REAL NOT NULL,
    other_fixed_usd REAL NOT NULL,
    total_fixed_usd REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS matic_price_history (
    date TEXT PRIMARY KEY,
    price_usd REAL NOT NULL
);
"""


@dataclass
class OpportunityRow:
    """Snapshot de uma oportunidade detectada (entrada do banco)."""

    market_id: str
    condition_id: str | None
    market_question: str | None
    ask_yes: float
    ask_no: float
    size_max_usdc: float
    size_max_shares: float
    gross_pnl_usdc: float
    estimated_fees_usdc: float
    estimated_gas_usdc: float
    timestamp_utc: str | None = None  # default: agora

    @property
    def sum_asks(self) -> float:
        return self.ask_yes + self.ask_no

    @property
    def net_pnl_usdc(self) -> float:
        return self.gross_pnl_usdc - self.estimated_fees_usdc - self.estimated_gas_usdc

    @property
    def net_pnl_pct(self) -> float:
        if self.size_max_usdc <= 0:
            return 0.0
        return (self.net_pnl_usdc / self.size_max_usdc) * 100.0


class OpportunityStore:
    """Wrapper SQLite. Sem ORM - schema simples justifica."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def insert_opportunity(self, row: OpportunityRow) -> int:
        ts = row.timestamp_utc or datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO opportunities (
                    timestamp_utc, market_id, condition_id, market_question,
                    ask_yes, ask_no, sum_asks,
                    size_max_usdc, size_max_shares,
                    gross_pnl_usdc, estimated_fees_usdc, estimated_gas_usdc,
                    net_pnl_usdc, net_pnl_pct,
                    was_traded
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    ts, row.market_id, row.condition_id, row.market_question,
                    row.ask_yes, row.ask_no, row.sum_asks,
                    row.size_max_usdc, row.size_max_shares,
                    row.gross_pnl_usdc, row.estimated_fees_usdc, row.estimated_gas_usdc,
                    row.net_pnl_usdc, row.net_pnl_pct,
                ),
            )
            return cur.lastrowid or 0

    def upsert_daily_cost(
        self,
        date: str,
        *,
        rpc_cost_usd: float,
        vps_cost_usd: float,
        other_fixed_usd: float,
    ) -> None:
        total = rpc_cost_usd + vps_cost_usd + other_fixed_usd
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_costs (date, rpc_cost_usd, vps_cost_usd, other_fixed_usd, total_fixed_usd)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    rpc_cost_usd = excluded.rpc_cost_usd,
                    vps_cost_usd = excluded.vps_cost_usd,
                    other_fixed_usd = excluded.other_fixed_usd,
                    total_fixed_usd = excluded.total_fixed_usd
                """,
                (date, rpc_cost_usd, vps_cost_usd, other_fixed_usd, total),
            )

    def upsert_matic_price(self, date: str, price_usd: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO matic_price_history (date, price_usd) VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET price_usd = excluded.price_usd
                """,
                (date, price_usd),
            )

    def count_opportunities(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) AS n FROM opportunities")
            return int(cur.fetchone()["n"])

    def recent_opportunities(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM opportunities ORDER BY id DESC LIMIT ?", (limit,)
            )
            return list(cur.fetchall())

    def aggregate_pnl(self, *, since_iso: str | None = None) -> dict[str, float | int]:
        """Soma simples de lucro detectado (so observacao). Para P&L de
        trades reais ver Fase 4+."""
        with self._connect() as conn:
            if since_iso is None:
                cur = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS n,
                        COALESCE(SUM(gross_pnl_usdc), 0) AS gross,
                        COALESCE(SUM(estimated_fees_usdc), 0) AS fees,
                        COALESCE(SUM(estimated_gas_usdc), 0) AS gas,
                        COALESCE(SUM(net_pnl_usdc), 0) AS net
                    FROM opportunities
                    """
                )
            else:
                cur = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS n,
                        COALESCE(SUM(gross_pnl_usdc), 0) AS gross,
                        COALESCE(SUM(estimated_fees_usdc), 0) AS fees,
                        COALESCE(SUM(estimated_gas_usdc), 0) AS gas,
                        COALESCE(SUM(net_pnl_usdc), 0) AS net
                    FROM opportunities WHERE timestamp_utc >= ?
                    """,
                    (since_iso,),
                )
            row = cur.fetchone()
            return {
                "num_opportunities": int(row["n"]),
                "gross_pnl_usdc": float(row["gross"]),
                "estimated_fees_usdc": float(row["fees"]),
                "estimated_gas_usdc": float(row["gas"]),
                "net_pnl_usdc": float(row["net"]),
            }
