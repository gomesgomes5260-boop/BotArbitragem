"""Sync periodico do estado do bot para o Notion.

Tres alvos:
1. Dashboard page: pagina com KPIs consolidados (re-renderizada a cada sync).
2. Trades database: 1 row por trade (paper/live) - append-only via cursor.
3. Opportunities database: 1 row por oportunidade detectada - append-only.

Idempotencia: tabela `notion_sync_state` no SQLite guarda o ultimo `id`
sincronizado por entidade. Cada run pega so o que e novo (id > cursor).
Cap configuravel (`max_rows_per_run`) evita estourar rate limit ao
recuperar de longa janela offline.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger

from bot.data.opportunity_store import OpportunityStore
from bot.economics.cost_model import CostModel
from bot.monitoring.notion_client import NotionAPIError, NotionClient
from bot.monitoring.pnl_report import build_report


@dataclass
class SyncResult:
    trades_synced: int
    opportunities_synced: int
    dashboard_updated: bool
    errors: list[str]


# ---------- Mapeamento SQLite row -> Notion properties ----------


def _trade_title(row: sqlite3.Row, question: str | None) -> str:
    base = (question or row["market_id"])[:80]
    return f"{base} | trade #{row['id']}"


def _opp_title(row: sqlite3.Row) -> str:
    base = (row["market_question"] or row["market_id"])[:80]
    return f"{base} | opp #{row['id']}"


def _title_prop(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text}}]}


def _rich_text_prop(text: str | None) -> dict:
    if not text:
        return {"rich_text": []}
    return {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}


def _number_prop(value: float | int | None) -> dict:
    if value is None:
        return {"number": None}
    return {"number": float(value)}


def _select_prop(value: str | None) -> dict:
    if value is None:
        return {"select": None}
    return {"select": {"name": value}}


def _date_prop(iso: str | None) -> dict:
    if not iso:
        return {"date": None}
    return {"date": {"start": iso}}


def _checkbox_prop(value: bool) -> dict:
    return {"checkbox": bool(value)}


def trade_row_to_properties(
    row: sqlite3.Row, *, question: str | None
) -> dict[str, Any]:
    """Mapeia linha SQLite da tabela `trades` para properties da Notion DB."""
    return {
        "Name": _title_prop(_trade_title(row, question)),
        "Trade ID": _number_prop(row["id"]),
        "Date": _date_prop(row["timestamp_utc"]),
        "Mode": _select_prop(row["mode"]),
        "Market ID": _rich_text_prop(row["market_id"]),
        "Question": _rich_text_prop(question),
        "Size USDC": _number_prop(row["size_usdc"]),
        "Gross PnL": _number_prop(row["realized_gross_pnl_usdc"]),
        "Net PnL": _number_prop(row["net_pnl_usdc"]),
        "Fees": _number_prop(
            (row["taker_fee_paid_usdc"] or 0.0) + (row["maker_fee_paid_usdc"] or 0.0)
        ),
        "Gas": _number_prop(row["gas_paid_usdc"]),
        "Fill YES": _number_prop(row["fill_price_yes"]),
        "Fill NO": _number_prop(row["fill_price_no"]),
        "Status": _select_prop(row["leg_status"]),
    }


def opp_row_to_properties(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "Name": _title_prop(_opp_title(row)),
        "Opp ID": _number_prop(row["id"]),
        "Date": _date_prop(row["timestamp_utc"]),
        "Market ID": _rich_text_prop(row["market_id"]),
        "Question": _rich_text_prop(row["market_question"]),
        "Ask YES": _number_prop(row["ask_yes"]),
        "Ask NO": _number_prop(row["ask_no"]),
        "Sum Asks": _number_prop(row["sum_asks"]),
        "Size Max USDC": _number_prop(row["size_max_usdc"]),
        "Gross PnL": _number_prop(row["gross_pnl_usdc"]),
        "Est Fees": _number_prop(row["estimated_fees_usdc"]),
        "Est Gas": _number_prop(row["estimated_gas_usdc"]),
        "Net PnL": _number_prop(row["net_pnl_usdc"]),
        "Net PnL %": _number_prop(row["net_pnl_pct"]),
        "Was Traded": _checkbox_prop(bool(row["was_traded"])),
    }


# ---------- Dashboard rendering ----------


def _heading(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {
        "object": "block",
        "type": key,
        key: {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _callout(text: str, *, emoji: str = "📊", color: str = "default") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _table_block(headers: list[str], rows: list[list[str]]) -> dict:
    """Bloco table com cabecalho. Notion suporta ate 100 linhas por request."""
    width = len(headers)

    def row(cells: list[str]) -> dict:
        return {
            "object": "block",
            "type": "table_row",
            "table_row": {
                "cells": [
                    [{"type": "text", "text": {"content": c}}] for c in cells
                ]
            },
        }

    children = [row(headers)] + [row(r) for r in rows]
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        },
    }


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _pct(v: float) -> str:
    return f"{v:.2f}%"


def build_dashboard_blocks(
    store: OpportunityStore, cost_model: CostModel
) -> list[dict]:
    """Constroi os blocos do dashboard a partir do estado atual do banco."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    blocks: list[dict] = []
    blocks.append(_heading(1, "BotArbitragem · Dashboard"))
    blocks.append(_paragraph(f"Ultima atualizacao: {now}"))
    blocks.append(_divider())

    # KPIs por periodo
    blocks.append(_heading(2, "Profit por periodo"))
    period_rows: list[list[str]] = []
    period_rows.append(
        ["Periodo", "Modo", "Trades", "Notional", "Gross", "Fees", "Gas", "Net", "Net pos infra", "ROI", "Win"]
    )
    body_rows: list[list[str]] = []
    for period in ("1d", "7d", "30d", "all"):
        for mode in ("paper", "live"):
            rep = build_report(store, cost_model, period=period, mode=mode)
            if rep.num_trades == 0:
                continue
            body_rows.append(
                [
                    period,
                    mode,
                    str(rep.num_trades),
                    _money(rep.notional_usdc),
                    _money(rep.gross_pnl_usdc),
                    _money(rep.fees_usdc),
                    _money(rep.gas_usdc),
                    _money(rep.net_pnl_usdc),
                    _money(rep.final_net_after_fixed),
                    _pct(rep.roi_pct),
                    _pct(rep.win_rate),
                ]
            )
    if body_rows:
        blocks.append(_table_block(period_rows[0], body_rows))
    else:
        blocks.append(
            _callout(
                "Nenhum trade registrado ainda. Mostrando so oportunidades detectadas abaixo.",
                emoji="ℹ️",
                color="gray_background",
            )
        )

    # Headline net 7d
    rep_7d = build_report(store, cost_model, period="7d", mode="paper")
    rep_7d_live = build_report(store, cost_model, period="7d", mode="live")
    headline_color = (
        "green_background"
        if (rep_7d.net_pnl_usdc + rep_7d_live.net_pnl_usdc) > 0
        else "red_background"
    )
    blocks.append(
        _callout(
            f"Net 7d (paper+live): {_money(rep_7d.net_pnl_usdc + rep_7d_live.net_pnl_usdc)} | "
            f"Custo fixo 7d: {_money(cost_model.daily_fixed_cost_usd * 7)}",
            emoji="💰",
            color=headline_color,
        )
    )
    blocks.append(_divider())

    # Top markets by net pnl
    blocks.append(_heading(2, "Top mercados por net P&L (30d)"))
    top = store.top_markets_by_pnl(days=30, limit=10)
    if top:
        rows = [
            [
                m["question"][:60],
                str(m["num_trades"]),
                _money(m["notional"]),
                _money(m["gross_pnl"]),
                _money(m["net_pnl"]),
            ]
            for m in top
        ]
        blocks.append(
            _table_block(
                ["Mercado", "Trades", "Notional", "Gross", "Net"], rows
            )
        )
    else:
        blocks.append(_paragraph("Sem dados de trades nos ultimos 30 dias."))
    blocks.append(_divider())

    # Daily P&L
    blocks.append(_heading(2, "P&L diario (30d)"))
    daily = store.daily_pnl_series(days=30)
    if daily:
        rows = [
            [
                d["day"],
                str(d["num_trades"]),
                _money(d["notional"]),
                _money(d["gross_pnl"]),
                _money(d["fees"] + d["gas"]),
                _money(d["net_pnl"]),
                str(d["wins"]),
            ]
            for d in daily[-30:]
        ]
        blocks.append(
            _table_block(
                ["Dia", "Trades", "Notional", "Gross", "Custos", "Net", "Wins"], rows
            )
        )
    else:
        blocks.append(_paragraph("Sem trades nos ultimos 30 dias."))
    blocks.append(_divider())

    # Oportunidades detectadas (potenciais)
    opp_pnl = store.aggregate_pnl()
    blocks.append(_heading(2, "Oportunidades detectadas (vida toda)"))
    blocks.append(
        _paragraph(
            f"Total: {opp_pnl['num_opportunities']} | "
            f"Net potencial agregado: {_money(float(opp_pnl['net_pnl_usdc']))} | "
            f"Gross: {_money(float(opp_pnl['gross_pnl_usdc']))}"
        )
    )

    # Custos fixos
    blocks.append(_heading(2, "Modelo de custos"))
    blocks.append(
        _paragraph(
            f"Mensal: RPC {_money(cost_model.monthly_rpc_usd)} + "
            f"VPS {_money(cost_model.monthly_vps_usd)} + "
            f"Outros {_money(cost_model.monthly_other_usd)} = "
            f"{_money(cost_model.total_monthly_fixed_usd)}"
        )
    )
    blocks.append(
        _paragraph(
            f"Diario amortizado: {_money(cost_model.daily_fixed_cost_usd)} | "
            f"Taker {cost_model.taker_fee_bps} bps | "
            f"Maker {cost_model.maker_fee_bps} bps | "
            f"Gas/tx {_money(cost_model.avg_gas_polygon_usd)}"
        )
    )

    return blocks


# ---------- Reporter principal ----------


class NotionReporter:
    """Orquestra o sync entre SQLite e Notion."""

    def __init__(
        self,
        store: OpportunityStore,
        cost_model: CostModel,
        client: NotionClient,
        *,
        dashboard_page_id: str,
        trades_db_id: str,
        opportunities_db_id: str,
        max_rows_per_run: int = 50,
    ) -> None:
        self.store = store
        self.cost_model = cost_model
        self.client = client
        self.dashboard_page_id = dashboard_page_id
        self.trades_db_id = trades_db_id
        self.opportunities_db_id = opportunities_db_id
        self.max_rows_per_run = max_rows_per_run

    async def sync_trades(self) -> tuple[int, list[str]]:
        cursor = self.store.get_notion_sync_cursor("trades")
        rows = self.store.trades_after_id(cursor, limit=self.max_rows_per_run)
        if not rows:
            return 0, []

        question_by_opp: dict[int, str | None] = {}

        errors: list[str] = []
        last_id = cursor
        synced = 0
        for row in rows:
            opp_id = row["opportunity_id"]
            question: str | None = None
            if opp_id is not None:
                if opp_id not in question_by_opp:
                    with self.store._connect() as conn:  # type: ignore[attr-defined]
                        cur = conn.execute(
                            "SELECT market_question FROM opportunities WHERE id = ?",
                            (opp_id,),
                        )
                        r = cur.fetchone()
                        question_by_opp[opp_id] = r["market_question"] if r else None
                question = question_by_opp[opp_id]
            try:
                props = trade_row_to_properties(row, question=question)
                await self.client.create_database_page(self.trades_db_id, props)
                synced += 1
                last_id = max(last_id, int(row["id"]))
            except NotionAPIError as exc:
                errors.append(f"trade #{row['id']}: {exc}")
                logger.error("falha sync trade #{}: {}", row["id"], exc)
                # Para na primeira falha pra nao avancar cursor passando rows pendentes
                break

        if last_id > cursor:
            self.store.set_notion_sync_cursor("trades", last_id)
        return synced, errors

    async def sync_opportunities(self) -> tuple[int, list[str]]:
        cursor = self.store.get_notion_sync_cursor("opportunities")
        rows = self.store.opportunities_after_id(cursor, limit=self.max_rows_per_run)
        if not rows:
            return 0, []

        errors: list[str] = []
        last_id = cursor
        synced = 0
        for row in rows:
            try:
                props = opp_row_to_properties(row)
                await self.client.create_database_page(
                    self.opportunities_db_id, props
                )
                synced += 1
                last_id = max(last_id, int(row["id"]))
            except NotionAPIError as exc:
                errors.append(f"opp #{row['id']}: {exc}")
                logger.error("falha sync opp #{}: {}", row["id"], exc)
                break

        if last_id > cursor:
            self.store.set_notion_sync_cursor("opportunities", last_id)
        return synced, errors

    async def update_dashboard(self) -> tuple[bool, list[str]]:
        try:
            blocks = build_dashboard_blocks(self.store, self.cost_model)
            await self.client.replace_page_content(self.dashboard_page_id, blocks)
            return True, []
        except NotionAPIError as exc:
            logger.error("falha atualizar dashboard: {}", exc)
            return False, [f"dashboard: {exc}"]

    async def sync_once(self) -> SyncResult:
        trades_n, te = await self.sync_trades()
        opps_n, oe = await self.sync_opportunities()
        ok, de = await self.update_dashboard()
        result = SyncResult(
            trades_synced=trades_n,
            opportunities_synced=opps_n,
            dashboard_updated=ok,
            errors=te + oe + de,
        )
        logger.info(
            "notion sync done: trades={} opps={} dashboard={} errors={}",
            result.trades_synced,
            result.opportunities_synced,
            result.dashboard_updated,
            len(result.errors),
        )
        return result
