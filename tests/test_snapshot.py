"""Round-trip de SnapshotWriter/SnapshotReader."""
from __future__ import annotations

from pathlib import Path

from bot.backtest.snapshot import Snapshot, SnapshotReader, SnapshotWriter
from bot.clients.models import BookLevel, Market, OrderBook


def _market() -> Market:
    return Market.model_validate(
        {
            "id": "m1",
            "question": "Will X happen?",
            "slug": "x",
            "conditionId": "0xa",
            "active": True,
            "volumeNum": 12345,
            "clobTokenIds": '["yes-tok", "no-tok"]',
            "outcomes": '["Yes", "No"]',
        }
    )


def _book(asset: str, asks: list[tuple[float, float]]) -> OrderBook:
    return OrderBook(
        asset_id=asset,
        asks=[BookLevel(price=p, size=s) for p, s in asks],
        bids=[],
    )


def test_writer_creates_file_and_reader_returns_same(tmp_path: Path) -> None:
    market = _market()
    yb = _book("yes-tok", [(0.40, 100)])
    nb = _book("no-tok", [(0.50, 100)])

    with SnapshotWriter(tmp_path) as w:
        w.write(market, yb, nb, ts="2026-04-28T10:00:00+00:00")
        w.write(market, yb, nb, ts="2026-04-28T10:00:05+00:00")

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1

    snaps = list(SnapshotReader(files[0]))
    assert len(snaps) == 2
    assert snaps[0].ts == "2026-04-28T10:00:00+00:00"
    assert snaps[0].market.id == "m1"
    assert snaps[0].market.condition_id == "0xa"
    assert snaps[0].market.is_binary is True
    assert snaps[0].yes_book.asset_id == "yes-tok"
    assert snaps[0].yes_book.asks[0].price == 0.40
    assert snaps[0].yes_book.asks[0].size == 100
    assert snaps[0].no_book.asks[0].price == 0.50


def test_reader_handles_directory(tmp_path: Path) -> None:
    (tmp_path / "2026-04-26.jsonl").write_text(
        '{"ts":"2026-04-26T00:00:00+00:00","market":{"id":"m1","question":"q","slug":"s",'
        '"conditionId":"0xa","clobTokenIds":["a","b"],"outcomes":["Yes","No"]},'
        '"yes_book":{"asset_id":"a","asks":[],"bids":[]},'
        '"no_book":{"asset_id":"b","asks":[],"bids":[]}}\n',
        encoding="utf-8",
    )
    (tmp_path / "2026-04-27.jsonl").write_text(
        '{"ts":"2026-04-27T00:00:00+00:00","market":{"id":"m2","question":"q","slug":"s",'
        '"conditionId":"0xb","clobTokenIds":["c","d"],"outcomes":["Yes","No"]},'
        '"yes_book":{"asset_id":"c","asks":[],"bids":[]},'
        '"no_book":{"asset_id":"d","asks":[],"bids":[]}}\n',
        encoding="utf-8",
    )
    snaps = list(SnapshotReader(tmp_path))
    assert len(snaps) == 2
    assert [s.market.id for s in snaps] == ["m1", "m2"]


def test_reader_skips_blank_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.jsonl"
    f.write_text(
        "\n"
        '{"ts":"2026-04-28T10:00:00+00:00","market":{"id":"m1","question":"q","slug":"s",'
        '"conditionId":"0xa","clobTokenIds":["a","b"],"outcomes":["Yes","No"]},'
        '"yes_book":{"asset_id":"a","asks":[],"bids":[]},'
        '"no_book":{"asset_id":"b","asks":[],"bids":[]}}\n'
        "\n",
        encoding="utf-8",
    )
    snaps = list(SnapshotReader(f))
    assert len(snaps) == 1


def test_writer_appends_to_existing_file(tmp_path: Path) -> None:
    market = _market()
    yb = _book("yes-tok", [(0.40, 100)])
    nb = _book("no-tok", [(0.50, 100)])

    with SnapshotWriter(tmp_path) as w:
        w.write(market, yb, nb, ts="2026-04-28T10:00:00+00:00")
    with SnapshotWriter(tmp_path) as w:
        w.write(market, yb, nb, ts="2026-04-28T10:00:05+00:00")

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    snaps = list(SnapshotReader(files[0]))
    assert len(snaps) == 2


def test_snapshot_round_trip_preserves_books() -> None:
    market = _market()
    yb = _book("yes-tok", [(0.40, 100), (0.45, 50)])
    yb.bids = [BookLevel(price=0.39, size=200)]
    nb = _book("no-tok", [(0.50, 100)])

    snap = Snapshot(ts="2026-04-28T10:00:00+00:00", market=market, yes_book=yb, no_book=nb)
    rebuilt = Snapshot.from_dict(snap.to_dict())

    assert rebuilt.market.id == market.id
    assert rebuilt.yes_book.asks[0].price == 0.40
    assert rebuilt.yes_book.asks[1].size == 50
    assert rebuilt.yes_book.bids[0].price == 0.39
    assert rebuilt.no_book.asks[0].price == 0.50
