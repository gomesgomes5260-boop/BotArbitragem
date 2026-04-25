"""Entrypoint do CLI do bot."""
from __future__ import annotations

import typer

from bot import __version__

app = typer.Typer(
    name="bot",
    help="Bot de arbitragem para a Polymarket.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Exibe a versao do bot."""
    typer.echo(f"bot-arbitragem v{__version__}")


@app.command(name="settings-check")
def settings_check() -> None:
    """Carrega settings e exibe valores nao-secretos."""
    from bot.config.settings import get_settings

    s = get_settings()
    typer.echo("== Settings ==")
    typer.echo(f"polygon_rpc_url       = {s.polygon_rpc_url}")
    typer.echo(f"log_level             = {s.log_level}")
    typer.echo(f"database_path         = {s.database_path}")
    typer.echo("-- Estrategia --")
    typer.echo(f"min_net_profit_pct    = {s.min_net_profit_pct}")
    typer.echo("-- Modelo de custos --")
    typer.echo(f"taker_fee_bps         = {s.taker_fee_bps}")
    typer.echo(f"maker_fee_bps         = {s.maker_fee_bps}")
    typer.echo(f"monthly_rpc_usd       = {s.monthly_rpc_usd}")
    typer.echo(f"monthly_vps_usd       = {s.monthly_vps_usd}")
    typer.echo(f"monthly_other_usd     = {s.monthly_other_usd}")
    typer.echo(f"total_monthly_fixed   = {s.total_monthly_fixed_usd:.2f} USD")
    typer.echo(f"daily_fixed_cost      = {s.daily_fixed_cost_usd:.4f} USD")
    typer.echo("-- Credenciais --")
    typer.echo(f"wallet_configured     = {s.wallet_configured}")
    typer.echo(f"api_configured        = {s.api_configured}")


if __name__ == "__main__":
    app()
