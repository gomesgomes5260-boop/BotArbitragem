"""Snapshot writer/reader em JSONL para a Fase 3 (backtesting).

Cada linha do arquivo eh um JSON contendo um mercado + seus dois books
em um instante. Append-only, rotacionado por dia automaticamente.

Formato (uma linha):
    {
      "ts": "2026-04-28T10:00:00.000Z",
      "market": { ...campos da Gamma... },
      "yes_book": {"asset_id": "...", "asks": [[p, s], ...], "bids": [...]},
      "no_book":  { ... }
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Iterator, TextIO

from bot.clients.models import BookLevel, Market, OrderBook


def _serialize_market(m: Market) -> dict[str, Any]:
    return {
        "id": m.id,
        "question": m.question,
        "slug": m.slug,
        "conditionId": m.condition_id,
        "active": m.active,
        "closed": m.closed,
        "archived": m.archived,
        "volume": m.volume,
        "volumeNum": m.volume_num,
        "liquidity": m.liquidity,
        "liquidityNum": m.liquidity_num,
        "clobTokenIds": m.clob_token_ids,
        "outcomes": m.outcomes,
        "outcomePrices": m.outcome_prices,
    }


def _serialize_book(b: OrderBook) -> dict[str, Any]:
    return {
        "asset_id": b.asset_id,
        "market": b.market,
        "timestamp": b.timestamp,
        "asks": [[lvl.price, lvl.size] for lvl in b.asks],
        "bids": [[lvl.price, lvl.size] for lvl in b.bids],
    }


def _parse_book(d: dict[str, Any]) -> OrderBook:
    return OrderBook(
        asset_id=d["asset_id"],
        market=d.get("market"),
        timestamp=d.get("timestamp"),
        asks=[BookLevel(price=p, size=s) for p, s in d.get("asks", [])],
        bids=[BookLevel(price=p, size=s) for p, s in d.get("bids", [])],
    )


@dataclass
class Snapshot:
    """Um frame: mercado + books no instante `ts`."""

    ts: str
    market: Market
    yes_book: OrderBook
    no_book: OrderBook

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Snapshot":
        return cls(
            ts=d["ts"],
            market=Market.model_validate(d["market"]),
            yes_book=_parse_book(d["yes_book"]),
            no_book=_parse_book(d["no_book"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "market": _serialize_market(self.market),
            "yes_book": _serialize_book(self.yes_book),
            "no_book": _serialize_book(self.no_book),
        }


def _today_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"


class SnapshotWriter:
    """Escreve snapshots em JSONL append-only no diretorio dado.

    Roda automatico um arquivo por dia UTC. Use como context manager pra
    garantir flush:
        with SnapshotWriter(Path("data/snapshots")) as w:
            w.write(market, yes_book, no_book)
    """

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO | None = None
        self._current_filename: str | None = None

    def __enter__(self) -> "SnapshotWriter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _ensure_file(self) -> TextIO:
        target = _today_filename()
        if self._fh is None or self._current_filename != target:
            self.close()
            self._current_filename = target
            self._fh = (self._dir / target).open("a", encoding="utf-8")
        return self._fh

    def write(
        self,
        market: Market,
        yes_book: OrderBook,
        no_book: OrderBook,
        *,
        ts: str | None = None,
    ) -> None:
        snap = Snapshot(
            ts=ts or datetime.now(timezone.utc).isoformat(),
            market=market,
            yes_book=yes_book,
            no_book=no_book,
        )
        fh = self._ensure_file()
        fh.write(json.dumps(snap.to_dict(), ensure_ascii=False) + "\n")

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None
            self._current_filename = None


class SnapshotReader:
    """Le snapshots de um arquivo JSONL ou de todos os arquivos de um diretorio.

    Iter ordena (a) lexicograficamente por nome de arquivo e (b) na ordem
    do arquivo - assumindo writer append-only, isso preserva ordem temporal.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def __iter__(self) -> Iterator[Snapshot]:
        if self._path.is_file():
            yield from self._iter_file(self._path)
            return
        if not self._path.is_dir():
            raise FileNotFoundError(self._path)
        for f in sorted(self._path.glob("*.jsonl")):
            yield from self._iter_file(f)

    @staticmethod
    def _iter_file(path: Path) -> Iterator[Snapshot]:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield Snapshot.from_dict(json.loads(line))
