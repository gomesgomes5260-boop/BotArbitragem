"""Smoke tests da Fase 0: garante que imports e settings funcionam."""
from __future__ import annotations

from typer.testing import CliRunner


def test_imports() -> None:
    import bot
    import bot.config.settings
    import bot.cli.main

    assert bot.__version__ == "0.1.0"


def test_settings_defaults(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    from bot.config.settings import Settings

    s = Settings()

    assert s.taker_fee_bps == 0
    assert s.maker_fee_bps == 0
    assert s.monthly_vps_usd == 10.0
    assert s.min_net_profit_pct == 0.5
    assert s.polygon_rpc_url.startswith("http")
    assert s.wallet_configured is False
    assert s.api_configured is False
    assert s.total_monthly_fixed_usd == 10.0
    assert abs(s.daily_fixed_cost_usd - (10.0 / 30.0)) < 1e-9


def test_settings_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TAKER_FEE_BPS", "200")
    monkeypatch.setenv("MONTHLY_VPS_USD", "20")

    from bot.config.settings import Settings

    s = Settings()

    assert s.taker_fee_bps == 200
    assert s.monthly_vps_usd == 20.0


def test_cli_version() -> None:
    from bot.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "bot-arbitragem v0.1.0" in result.stdout


def test_cli_settings_check(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)

    from bot.cli.main import app
    from bot.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()

    runner = CliRunner()
    result = runner.invoke(app, ["settings-check"])

    assert result.exit_code == 0
    assert "polygon_rpc_url" in result.stdout
    assert "wallet_configured" in result.stdout
