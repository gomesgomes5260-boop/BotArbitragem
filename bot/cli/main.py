"""Entrypoint do CLI do bot."""
from __future__ import annotations

import asyncio

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


@app.command(name="list-markets")
def list_markets_cmd(
    min_volume: float = typer.Option(
        10000.0, "--min-volume", help="Volume minimo (USDC) para listar."
    ),
    max_pages: int = typer.Option(5, "--max-pages", help="Quantas paginas da Gamma API consultar."),
    limit_display: int = typer.Option(20, "--limit", help="Quantos exibir no terminal."),
) -> None:
    """Lista mercados binarios ativos com volume minimo (Gamma API)."""
    from bot.clients.polymarket_gamma import GammaClient

    async def _run() -> None:
        async with GammaClient() as gc:
            markets = await gc.list_active_binary_markets(
                min_volume=min_volume, max_pages=max_pages
            )
        markets.sort(key=lambda m: m.best_volume, reverse=True)
        typer.echo(f"== {len(markets)} mercados binarios ativos (volume >= {min_volume:.0f}) ==")
        for m in markets[:limit_display]:
            cid_short = (m.condition_id or "")[:10] + "..." if m.condition_id else "(no cid)"
            typer.echo(
                f"  vol={m.best_volume:>12.0f}  liq={m.liquidity:>10.0f}  {cid_short}  {m.question[:80]}"
            )
        if len(markets) > limit_display:
            typer.echo(f"  ... e mais {len(markets) - limit_display}")

    asyncio.run(_run())


@app.command(name="show-book")
def show_book_cmd(
    condition_id: str = typer.Argument(..., help="conditionId do mercado (Gamma)."),
    depth: int = typer.Option(5, "--depth", help="Niveis do book a exibir por lado."),
) -> None:
    """Exibe o order book (YES e NO) de um mercado binario."""
    from bot.clients.polymarket_clob import ClobClient
    from bot.clients.polymarket_gamma import GammaClient

    async def _run() -> None:
        async with GammaClient() as gc:
            market = await gc.get_market(condition_id)
        if market is None:
            typer.echo(f"Mercado {condition_id} nao encontrado.")
            raise typer.Exit(code=1)
        if not market.is_binary:
            typer.echo(f"Mercado {condition_id} nao e binario (outcomes={market.outcomes}).")
            raise typer.Exit(code=1)

        typer.echo(f"== {market.question} ==")
        typer.echo(f"   slug={market.slug}  vol={market.best_volume:.0f}")

        async with ClobClient() as cc:
            books = await cc.get_books(market.clob_token_ids)

        labels = market.outcomes if len(market.outcomes) == 2 else ["YES", "NO"]
        for label, book in zip(labels, books, strict=False):
            typer.echo(f"\n-- {label}  (asset_id={book.asset_id[:14]}...) --")
            typer.echo("  ASKS (vendas):")
            for lvl in sorted(book.asks, key=lambda x: x.price)[:depth]:
                typer.echo(f"    {lvl.price:>7.4f}  size={lvl.size:.2f}")
            typer.echo("  BIDS (compras):")
            for lvl in sorted(book.bids, key=lambda x: x.price, reverse=True)[:depth]:
                typer.echo(f"    {lvl.price:>7.4f}  size={lvl.size:.2f}")
            if book.midpoint is not None:
                typer.echo(f"  midpoint={book.midpoint:.4f}")

        if len(books) == 2:
            ba_yes = books[0].best_ask
            ba_no = books[1].best_ask
            if ba_yes and ba_no:
                soma = ba_yes.price + ba_no.price
                typer.echo(
                    f"\n  best_ask(YES)+best_ask(NO) = {ba_yes.price:.4f} + {ba_no.price:.4f} = {soma:.4f}"
                )
                if soma < 1.0:
                    typer.echo(
                        f"  >> Possivel arbitragem bruta: {(1.0 - soma) * 100:.2f}% (sem custos)"
                    )

    asyncio.run(_run())


@app.command(name="watch-book")
def watch_book_cmd(
    condition_id: str = typer.Argument(..., help="conditionId do mercado para escutar."),
    duration: int = typer.Option(30, "--duration", help="Segundos antes de desconectar."),
    max_messages: int = typer.Option(
        50, "--max-messages", help="Para apos N mensagens (-1 = ilimitado)."
    ),
) -> None:
    """Conecta no WebSocket do CLOB e mostra updates do book em tempo real."""
    from bot.clients.polymarket_gamma import GammaClient
    from bot.clients.polymarket_ws import ClobWsClient

    async def _run() -> None:
        async with GammaClient() as gc:
            market = await gc.get_market(condition_id)
        if market is None or not market.is_binary:
            typer.echo(f"Mercado {condition_id} invalido ou nao binario.")
            raise typer.Exit(code=1)

        typer.echo(f"Escutando WS de '{market.question[:80]}'...")
        typer.echo(f"  token_ids: {market.clob_token_ids}")

        ws = ClobWsClient()
        cap = None if max_messages < 0 else max_messages

        async def consume() -> None:
            count = 0
            async for ev in ws.events(market.clob_token_ids, max_messages=cap):
                etype = ev.get("event_type", "?")
                aid = (ev.get("asset_id") or "?")[:14]
                ts = ev.get("timestamp", "?")
                count += 1
                typer.echo(f"  #{count:03d}  type={etype:14s} asset={aid}... ts={ts}")

        try:
            await asyncio.wait_for(consume(), timeout=duration)
        except asyncio.TimeoutError:
            typer.echo(f"\nTimeout de {duration}s atingido.")

    asyncio.run(_run())


@app.command(name="scan")
def scan_cmd(
    min_volume: float = typer.Option(
        10000.0, "--min-volume", help="Volume minimo (USDC) dos mercados a varrer."
    ),
    max_markets: int = typer.Option(
        50, "--max-markets", help="Quantos mercados (top por volume) varrer."
    ),
    max_pages: int = typer.Option(5, "--max-pages", help="Paginas Gamma a consultar."),
    show_all: bool = typer.Option(
        False, "--show-all", help="Imprime todas as oportunidades, nao so o resumo."
    ),
) -> None:
    """Faz uma passada completa: detecta arbs YES+NO em mercados ativos e grava em SQLite."""
    from bot.clients.polymarket_clob import ClobClient
    from bot.clients.polymarket_gamma import GammaClient
    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.economics.cost_model import CostModel
    from bot.strategies.intra_market import IntraMarketDetector
    from bot.strategies.scanner import IntraMarketScanner, format_opportunity_line

    settings = get_settings()
    cost_model = CostModel.from_settings(settings)
    detector = IntraMarketDetector(cost_model, min_net_profit_pct=settings.min_net_profit_pct)

    store = OpportunityStore(settings.database_path)
    store.init_schema()

    typer.echo(f"== Scan inicio ==")
    typer.echo(
        f"   threshold lucro liquido: {settings.min_net_profit_pct}%   "
        f"taker_fee={settings.taker_fee_bps}bps   "
        f"gas_per_tx=${settings.avg_gas_polygon_usd}"
    )

    async def _run() -> None:
        async with GammaClient() as gamma, ClobClient() as clob:
            scanner = IntraMarketScanner(gamma, clob, detector, store)
            n_cached = await scanner.refresh_cache(min_volume=min_volume, max_pages=max_pages)
            typer.echo(f"   mercados ativos no cache: {n_cached}")

            result = await scanner.scan_once(max_markets=max_markets)
            typer.echo(
                f"   varridos {result.markets_scanned}  ->  "
                f"{result.opportunities_found} oportunidades"
            )

            if result.opportunities:
                result.opportunities.sort(key=lambda o: o.net_pnl_pct, reverse=True)
                typer.echo("\n  Oportunidades (ordenadas por net_pct):")
                top = result.opportunities if show_all else result.opportunities[:10]
                for opp in top:
                    typer.echo(format_opportunity_line(opp))
                if not show_all and len(result.opportunities) > 10:
                    typer.echo(
                        f"  ... e mais {len(result.opportunities) - 10}. Use --show-all pra ver tudo."
                    )

    asyncio.run(_run())


@app.command(name="monitor")
def monitor_cmd(
    min_volume: float = typer.Option(10000.0, "--min-volume"),
    max_markets: int = typer.Option(50, "--max-markets"),
    max_pages: int = typer.Option(5, "--max-pages"),
    interval: float = typer.Option(
        5.0, "--interval", help="Segundos entre passadas."
    ),
    refresh_cache_every: int = typer.Option(
        20, "--refresh-cache-every", help="Refresca lista de mercados a cada N passadas."
    ),
    snapshot_dir: str | None = typer.Option(
        None, "--snapshot-dir", help="Diretorio onde gravar snapshots JSONL para replay."
    ),
) -> None:
    """Loop continuo de scan. Ctrl+C pra parar."""
    from bot.backtest.snapshot import SnapshotWriter
    from bot.clients.polymarket_clob import ClobClient
    from bot.clients.polymarket_gamma import GammaClient
    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.economics.cost_model import CostModel
    from bot.strategies.intra_market import IntraMarketDetector
    from bot.strategies.scanner import IntraMarketScanner, format_opportunity_line

    settings = get_settings()
    cost_model = CostModel.from_settings(settings)
    detector = IntraMarketDetector(cost_model, min_net_profit_pct=settings.min_net_profit_pct)

    store = OpportunityStore(settings.database_path)
    store.init_schema()

    typer.echo(f"== Monitor iniciando (interval={interval}s, Ctrl+C pra parar) ==")
    if snapshot_dir is not None:
        typer.echo(f"   gravando snapshots em {snapshot_dir}/")

    async def _run() -> None:
        writer_ctx = SnapshotWriter(snapshot_dir) if snapshot_dir else None
        async with GammaClient() as gamma, ClobClient() as clob:
            try:
                if writer_ctx is not None:
                    writer_ctx.__enter__()
                scanner = IntraMarketScanner(
                    gamma, clob, detector, store, snapshot_writer=writer_ctx
                )
                await scanner.refresh_cache(min_volume=min_volume, max_pages=max_pages)
                typer.echo(f"   {len(scanner.cache.markets)} mercados no cache")

                pass_num = 0
                try:
                    while True:
                        pass_num += 1
                        if pass_num > 1 and (pass_num % refresh_cache_every == 0):
                            await scanner.refresh_cache(
                                min_volume=min_volume, max_pages=max_pages
                            )
                        result = await scanner.scan_once(max_markets=max_markets)

                        if result.opportunities:
                            result.opportunities.sort(key=lambda o: o.net_pnl_pct, reverse=True)
                            typer.echo(
                                f"[#{pass_num:04d}] {result.opportunities_found} arbs"
                                f" (top {min(3, len(result.opportunities))}):"
                            )
                            for opp in result.opportunities[:3]:
                                typer.echo(format_opportunity_line(opp))
                        else:
                            typer.echo(
                                f"[#{pass_num:04d}] nenhuma arb (varridos {result.markets_scanned})"
                            )

                        await asyncio.sleep(interval)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    typer.echo("\nMonitor encerrado.")
            finally:
                if writer_ctx is not None:
                    writer_ctx.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


@app.command(name="replay")
def replay_cmd(
    path: str = typer.Argument(..., help="Arquivo .jsonl ou diretorio com snapshots."),
    threshold_pct: float | None = typer.Option(
        None, "--threshold", help="Override do MIN_NET_PROFIT_PCT pra esta replay."
    ),
    taker_fee_bps: int | None = typer.Option(
        None, "--taker-fee-bps", help="Override do taker fee (bps)."
    ),
    gas_per_tx_usd: float | None = typer.Option(
        None, "--gas-per-tx", help="Override do gas estimado por tx (USD)."
    ),
    top_n_markets: int = typer.Option(10, "--top-markets", help="Top mercados a exibir."),
) -> None:
    """Replay do detector contra snapshots gravados (Fase 3)."""
    from pathlib import Path

    from bot.backtest.replay import ReplayRunner
    from bot.backtest.snapshot import SnapshotReader
    from bot.config.settings import get_settings
    from bot.economics.cost_model import CostModel
    from bot.strategies.intra_market import IntraMarketDetector

    settings = get_settings()
    cm = CostModel(
        taker_fee_bps=taker_fee_bps if taker_fee_bps is not None else settings.taker_fee_bps,
        maker_fee_bps=settings.maker_fee_bps,
        avg_gas_polygon_usd=(
            gas_per_tx_usd if gas_per_tx_usd is not None else settings.avg_gas_polygon_usd
        ),
        txs_per_arb=settings.txs_per_arb,
        monthly_rpc_usd=settings.monthly_rpc_usd,
        monthly_vps_usd=settings.monthly_vps_usd,
        monthly_other_usd=settings.monthly_other_usd,
    )
    threshold = (
        threshold_pct if threshold_pct is not None else settings.min_net_profit_pct
    )
    detector = IntraMarketDetector(cm, min_net_profit_pct=threshold)

    target = Path(path)
    if not target.exists():
        typer.echo(f"Arquivo/diretorio nao encontrado: {path}")
        raise typer.Exit(code=1)

    typer.echo(f"== Replay {path} ==")
    typer.echo(
        f"   threshold={threshold}%   taker_fee={cm.taker_fee_bps}bps   "
        f"gas/tx=${cm.avg_gas_polygon_usd}"
    )

    runner = ReplayRunner(detector)
    metrics = runner.run(SnapshotReader(target))

    typer.echo(f"   snapshots:       {metrics.snapshots_processed}")
    typer.echo(f"   com arb:         {metrics.snapshots_with_arb}")
    typer.echo(f"   detections:      {metrics.detections}")
    typer.echo(f"   periodo:         {metrics.time_span_days:.2f} dias")
    typer.echo("")
    typer.echo(f"   gross:           ${metrics.gross_pnl_usdc:.2f}")
    typer.echo(f"   fees estimadas:  ${metrics.fees_usdc:.2f}")
    typer.echo(f"   gas estimado:    ${metrics.gas_usdc:.2f}")
    typer.echo(f"   net_pnl:         ${metrics.net_pnl_usdc:.2f}")
    if metrics.time_span_days > 0:
        typer.echo(f"   net/dia:         ${metrics.net_pnl_usdc / metrics.time_span_days:.4f}")
    fixed_period = cm.daily_fixed_cost_usd * max(metrics.time_span_days, 1.0)
    typer.echo(f"   custo fixo:      ${fixed_period:.4f} ({cm.daily_fixed_cost_usd:.4f}/dia)")
    payback = "SIM" if metrics.net_pnl_usdc > fixed_period else "NAO"
    typer.echo(f"   paga a infra:    {payback}")

    if metrics.durations_seconds:
        avg = metrics.avg_duration or 0.0
        p50 = metrics.duration_quantile(0.50)
        p95 = metrics.duration_quantile(0.95)
        typer.echo("")
        typer.echo(
            f"   duracao arb:     avg={avg:.1f}s  p50={p50:.1f}s  p95={p95:.1f}s  n={len(metrics.durations_seconds)}"
        )

    if metrics.per_market:
        typer.echo("")
        typer.echo(f"== Top {top_n_markets} mercados (por net_pnl) ==")
        for ms in metrics.top_markets[:top_n_markets]:
            typer.echo(
                f"   net=${ms.net_pnl_usdc:>7.2f}  detections={ms.detections:>4d}  "
                f"{(ms.question or ms.market_id)[:80]}"
            )


@app.command(name="stats")
def stats_cmd(
    last_n: int = typer.Option(
        10, "--last", help="Quantas oportunidades recentes mostrar."
    ),
) -> None:
    """Resumo das oportunidades acumuladas no banco."""
    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.economics.cost_model import CostModel

    settings = get_settings()
    store = OpportunityStore(settings.database_path)
    store.init_schema()
    cost_model = CostModel.from_settings(settings)

    agg = store.aggregate_pnl()
    typer.echo(f"== Total acumulado ==")
    typer.echo(f"  oportunidades:       {agg['num_opportunities']}")
    typer.echo(f"  gross_pnl total:     ${agg['gross_pnl_usdc']:.2f}")
    typer.echo(f"  fees estimadas:      ${agg['estimated_fees_usdc']:.2f}")
    typer.echo(f"  gas estimado:        ${agg['estimated_gas_usdc']:.2f}")
    typer.echo(f"  net_pnl total:       ${agg['net_pnl_usdc']:.2f}")
    typer.echo(f"  custo fixo diario:   ${cost_model.daily_fixed_cost_usd:.4f}")
    typer.echo(f"  break-even/dia:      ${cost_model.break_even_daily_pnl():.4f}")

    rows = store.recent_opportunities(limit=last_n)
    if rows:
        typer.echo(f"\n== Ultimas {len(rows)} oportunidades ==")
        for r in rows:
            ts = r["timestamp_utc"][:19]
            typer.echo(
                f"  {ts}  net={r['net_pnl_pct']:>5.2f}%  "
                f"${r['net_pnl_usdc']:>6.2f}  size=${r['size_max_usdc']:>8.2f}  "
                f"{(r['market_question'] or '')[:60]}"
            )


if __name__ == "__main__":
    app()
