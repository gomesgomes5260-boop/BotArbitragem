"""Modelos pydantic para respostas das APIs da Polymarket."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _parse_json_string_list(v: Any) -> list[str]:
    """Gamma API retorna alguns campos como string JSON-encoded (ex: '["a","b"]')."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        if not v.strip():
            return []
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _parse_json_string_floats(v: Any) -> list[float]:
    raw = _parse_json_string_list(v)
    out: list[float] = []
    for item in raw:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


class Market(BaseModel):
    """Mercado da Gamma API. Aceita campos extras sem quebrar."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    question: str
    slug: str
    condition_id: str | None = Field(default=None, alias="conditionId")
    active: bool = True
    closed: bool = False
    archived: bool = False
    volume: float = 0.0
    volume_num: float | None = Field(default=None, alias="volumeNum")
    liquidity: float = 0.0
    liquidity_num: float | None = Field(default=None, alias="liquidityNum")
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[float] = Field(default_factory=list, alias="outcomePrices")
    end_date: datetime | None = Field(default=None, alias="endDate")
    start_date: datetime | None = Field(default=None, alias="startDate")

    @field_validator("clob_token_ids", mode="before")
    @classmethod
    def _v_clob_token_ids(cls, v: Any) -> list[str]:
        return _parse_json_string_list(v)

    @field_validator("outcomes", mode="before")
    @classmethod
    def _v_outcomes(cls, v: Any) -> list[str]:
        return _parse_json_string_list(v)

    @field_validator("outcome_prices", mode="before")
    @classmethod
    def _v_outcome_prices(cls, v: Any) -> list[float]:
        return _parse_json_string_floats(v)

    @field_validator("volume", "liquidity", mode="before")
    @classmethod
    def _v_floats(cls, v: Any) -> float:
        if v is None or v == "":
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2 and len(self.clob_token_ids) == 2

    @property
    def yes_token_id(self) -> str | None:
        return self.clob_token_ids[0] if self.is_binary else None

    @property
    def no_token_id(self) -> str | None:
        return self.clob_token_ids[1] if self.is_binary else None

    @property
    def best_volume(self) -> float:
        """Usa volumeNum se disponivel, fallback para volume."""
        return self.volume_num if self.volume_num is not None else self.volume


class BookLevel(BaseModel):
    """Um nivel do order book (price + size)."""

    model_config = ConfigDict(extra="ignore")

    price: float
    size: float

    @field_validator("price", "size", mode="before")
    @classmethod
    def _v_to_float(cls, v: Any) -> float:
        if v is None or v == "":
            return 0.0
        return float(v)


class OrderBook(BaseModel):
    """Order book retornado pelo CLOB GET /book."""

    model_config = ConfigDict(extra="ignore")

    market: str | None = None
    asset_id: str
    timestamp: str | None = None
    hash: str | None = None
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)

    @property
    def best_bid(self) -> BookLevel | None:
        if not self.bids:
            return None
        return max(self.bids, key=lambda lvl: lvl.price)

    @property
    def best_ask(self) -> BookLevel | None:
        if not self.asks:
            return None
        return min(self.asks, key=lambda lvl: lvl.price)

    @property
    def midpoint(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb.price + ba.price) / 2.0
