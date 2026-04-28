"""Configuracoes do bot, carregadas via pydantic-settings."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    poly_private_key: str | None = None
    poly_funder_address: str | None = None

    poly_api_key: str | None = None
    poly_api_secret: str | None = None
    poly_api_passphrase: str | None = None

    polygon_rpc_url: str = "https://polygon-rpc.com"

    taker_fee_bps: int = Field(default=0, ge=0, le=1000)
    maker_fee_bps: int = Field(default=0, ge=0, le=1000)
    monthly_rpc_usd: float = Field(default=0.0, ge=0.0)
    monthly_vps_usd: float = Field(default=10.0, ge=0.0)
    monthly_other_usd: float = Field(default=0.0, ge=0.0)

    avg_gas_polygon_usd: float = Field(default=0.05, ge=0.0)
    txs_per_arb: int = Field(default=2, ge=1)

    min_net_profit_pct: float = Field(default=0.5, ge=0.0)

    paper_capital_per_trade_usdc: float = Field(default=20.0, ge=1.0)

    log_level: str = "INFO"
    database_path: Path = Path("data/bot.db")

    @property
    def wallet_configured(self) -> bool:
        return bool(self.poly_private_key and self.poly_funder_address)

    @property
    def api_configured(self) -> bool:
        return bool(
            self.poly_api_key and self.poly_api_secret and self.poly_api_passphrase
        )

    @property
    def total_monthly_fixed_usd(self) -> float:
        return self.monthly_rpc_usd + self.monthly_vps_usd + self.monthly_other_usd

    @property
    def daily_fixed_cost_usd(self) -> float:
        return self.total_monthly_fixed_usd / 30.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
