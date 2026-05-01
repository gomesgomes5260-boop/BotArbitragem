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
    paper: bool = typer.Option(
        False, "--paper", help="Simula execucao paper de cada arb detectada."
    ),
    paper_capital: float | None = typer.Option(
        None, "--paper-capital", help="Override do capital por trade (USDC)."
    ),
) -> None:
    """Faz uma passada completa: detecta arbs YES+NO em mercados ativos e grava em SQLite."""
    from bot.clients.polymarket_clob import ClobClient
    from bot.clients.polymarket_gamma import GammaClient
    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.economics.cost_model import CostModel
    from bot.execution.paper_trader import PaperTrader
    from bot.strategies.intra_market import IntraMarketDetector
    from bot.strategies.scanner import IntraMarketScanner, format_opportunity_line

    settings = get_settings()
    cost_model = CostModel.from_settings(settings)
    detector = IntraMarketDetector(cost_model, min_net_profit_pct=settings.min_net_profit_pct)

    store = OpportunityStore(settings.database_path)
    store.init_schema()

    paper_trader: PaperTrader | None = None
    if paper:
        capital = paper_capital if paper_capital is not None else settings.paper_capital_per_trade_usdc
        paper_trader = PaperTrader(cost_model, store, capital_per_trade_usdc=capital)

    typer.echo(f"== Scan inicio ==")
    typer.echo(
        f"   threshold lucro liquido: {settings.min_net_profit_pct}%   "
        f"taker_fee={settings.taker_fee_bps}bps   "
        f"gas_per_tx=${settings.avg_gas_polygon_usd}"
    )
    if paper_trader is not None:
        typer.echo(f"   paper trading ON, capital ${paper_trader.capital_per_trade_usdc:.2f}/trade")

    async def _run() -> None:
        async with GammaClient() as gamma, ClobClient() as clob:
            scanner = IntraMarketScanner(
                gamma, clob, detector, store, paper_trader=paper_trader
            )
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

            if result.paper_trades:
                typer.echo(f"\n  Paper trades simulados: {len(result.paper_trades)}")
                for t in result.paper_trades:
                    typer.echo(
                        f"    [{t.leg_status:13s}] net=${t.net_pnl_usdc:>6.2f}  "
                        f"size=${t.total_usdc_spent:>6.2f}  "
                        f"{t.opportunity.market.question[:50]}"
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
    paper: bool = typer.Option(
        False, "--paper", help="Simula execucao paper de cada arb detectada."
    ),
    paper_capital: float | None = typer.Option(
        None, "--paper-capital", help="Override do capital por trade (USDC)."
    ),
) -> None:
    """Loop continuo de scan. Ctrl+C pra parar."""
    from bot.backtest.snapshot import SnapshotWriter
    from bot.clients.polymarket_clob import ClobClient
    from bot.clients.polymarket_gamma import GammaClient
    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.economics.cost_model import CostModel
    from bot.execution.paper_trader import PaperTrader
    from bot.strategies.intra_market import IntraMarketDetector
    from bot.strategies.scanner import IntraMarketScanner, format_opportunity_line

    settings = get_settings()
    cost_model = CostModel.from_settings(settings)
    detector = IntraMarketDetector(cost_model, min_net_profit_pct=settings.min_net_profit_pct)

    store = OpportunityStore(settings.database_path)
    store.init_schema()

    paper_trader: PaperTrader | None = None
    if paper:
        capital = paper_capital if paper_capital is not None else settings.paper_capital_per_trade_usdc
        paper_trader = PaperTrader(cost_model, store, capital_per_trade_usdc=capital)

    typer.echo(f"== Monitor iniciando (interval={interval}s, Ctrl+C pra parar) ==")
    if snapshot_dir is not None:
        typer.echo(f"   gravando snapshots em {snapshot_dir}/")
    if paper_trader is not None:
        typer.echo(f"   paper trading ON, capital ${paper_trader.capital_per_trade_usdc:.2f}/trade")

    async def _run() -> None:
        writer_ctx = SnapshotWriter(snapshot_dir) if snapshot_dir else None
        async with GammaClient() as gamma, ClobClient() as clob:
            try:
                if writer_ctx is not None:
                    writer_ctx.__enter__()
                scanner = IntraMarketScanner(
                    gamma,
                    clob,
                    detector,
                    store,
                    snapshot_writer=writer_ctx,
                    paper_trader=paper_trader,
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


@app.command(name="pnl")
def pnl_cmd(
    period: str = typer.Option("all", "--period", help="'1d' | '7d' | '30d' | 'all'"),
    mode: str | None = typer.Option(
        None, "--mode", help="'paper' | 'live'. Padrao: ambos."
    ),
    last_trades: int = typer.Option(
        10, "--last", help="Quantos trades recentes mostrar."
    ),
) -> None:
    """Relatorio de P&L consolidado (trades reais + custos fixos)."""
    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.economics.cost_model import CostModel
    from bot.monitoring.pnl_report import build_report, format_report

    settings = get_settings()
    store = OpportunityStore(settings.database_path)
    store.init_schema()
    cost_model = CostModel.from_settings(settings)

    report = build_report(store, cost_model, period=period, mode=mode)
    for line in format_report(report):
        typer.echo(line)

    rows = store.recent_trades(mode=mode, limit=last_trades)
    if rows:
        typer.echo("")
        typer.echo(f"== Ultimos {len(rows)} trades ==")
        for r in rows:
            ts = r["timestamp_utc"][:19]
            typer.echo(
                f"  {ts}  {r['mode']:5s}  {r['leg_status']:13s}  "
                f"net=${r['net_pnl_usdc']:>6.2f}  size=${r['size_usdc']:>7.2f}  "
                f"market={r['market_id']}"
            )


CONFIRM_PHRASE = "EU CONFIRMO DINHEIRO REAL"


@app.command(name="risk-status")
def risk_status_cmd() -> None:
    """Mostra estado de risco: limites, exposure, kill-switch."""
    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.execution.live_trader import get_today_live_exposure, get_today_live_pnl
    from bot.risk.limits import RiskManager

    settings = get_settings()
    store = OpportunityStore(settings.database_path)
    store.init_schema()
    rm = RiskManager.from_settings(settings)

    summary = rm.status_summary()
    today_exposure = get_today_live_exposure(store)
    today_pnl = get_today_live_pnl(store)

    typer.echo("== Risk status ==")
    typer.echo(f"  kill_switch_active : {summary['kill_switch_active']}")
    if summary["kill_switch_active"]:
        typer.echo(f"  kill_switch_reason : {summary['kill_switch_reason']}")
    typer.echo(f"  kill_switch_path   : {summary['kill_switch_path']}")
    typer.echo("")
    typer.echo("  Limits (live):")
    for k, v in summary["limits"].items():
        typer.echo(f"    {k} = {v}")
    typer.echo(f"    hard_cap_per_trade_usdc = {summary['hard_cap_per_trade_usdc']}")
    typer.echo("")
    typer.echo("  Hoje (UTC):")
    typer.echo(f"    exposure (notional): ${today_exposure:.2f}")
    typer.echo(f"    realized P&L:        ${today_pnl:.2f}")


@app.command(name="kill-switch")
def kill_switch_cmd(
    reset: bool = typer.Option(False, "--reset", help="Desarma o kill-switch."),
    trigger: bool = typer.Option(False, "--trigger", help="Arma manualmente (parar trading)."),
    reason: str = typer.Option("manual", "--reason", help="Motivo (registrado no arquivo)."),
) -> None:
    """Gerencia kill-switch (arma/desarma).

    Sem flags: so mostra status. Com --reset: desarma. Com --trigger:
    arma. So um por chamada.
    """
    from bot.config.settings import get_settings
    from bot.risk.kill_switch import KillSwitch

    if reset and trigger:
        typer.echo("Use apenas --reset ou --trigger, nao ambos.")
        raise typer.Exit(code=1)

    settings = get_settings()
    ks = KillSwitch(settings.kill_switch_path)

    if reset:
        if not ks.is_active():
            typer.echo("Kill-switch ja esta desarmado.")
            return
        ks.reset()
        typer.echo("Kill-switch DESARMADO.")
        return

    if trigger:
        ks.trigger(reason)
        typer.echo(f"Kill-switch ARMADO. razao: {reason}")
        return

    if ks.is_active():
        typer.echo(f"Kill-switch ATIVO. Razao: {ks.reason()}")
    else:
        typer.echo("Kill-switch desarmado.")


@app.command(name="live")
def live_cmd(
    min_volume: float = typer.Option(10000.0, "--min-volume"),
    max_markets: int = typer.Option(20, "--max-markets"),
    max_pages: int = typer.Option(5, "--max-pages"),
    interval: float = typer.Option(5.0, "--interval"),
    confirm: bool = typer.Option(
        False, "--i-confirm-real-money", help="Pula prompt interativo (CI)."
    ),
    one_shot: bool = typer.Option(
        False, "--one-shot", help="Roda uma passada e sai (em vez de loop)."
    ),
) -> None:
    """EXECUCAO REAL: detecta + executa arbs com capital de verdade.

    Antes de rodar:
    1. Configure .env com POLY_PRIVATE_KEY, POLY_FUNDER_ADDRESS, POLY_API_*.
    2. Confira `bot risk-status` - limites batem com seu apetite?
    3. Comece com LIVE_MAX_CAPITAL_PER_TRADE_USDC pequeno (5-10).
    """
    import asyncio as _asyncio

    from bot.clients.polymarket_clob import ClobClient
    from bot.clients.polymarket_gamma import GammaClient
    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.economics.cost_model import CostModel
    from bot.execution.live_client import PolymarketLiveClient
    from bot.execution.live_trader import LiveTrader
    from bot.risk.kill_switch import KillSwitch
    from bot.risk.limits import RiskManager
    from bot.strategies.intra_market import IntraMarketDetector
    from bot.strategies.scanner import IntraMarketScanner, format_opportunity_line

    settings = get_settings()

    if not settings.wallet_configured:
        typer.echo("ERRO: POLY_PRIVATE_KEY ou POLY_FUNDER_ADDRESS nao configurado em .env")
        raise typer.Exit(code=1)

    typer.echo("======================================================")
    typer.echo("  ATENCAO: MODO LIVE - dinheiro real sera utilizado.")
    typer.echo("======================================================")
    typer.echo(f"  Carteira (funder)  : {settings.poly_funder_address}")
    typer.echo(f"  Capital/trade max  : ${settings.live_max_capital_per_trade_usdc}")
    typer.echo(f"  Hard cap/trade     : ${settings.live_capital_hard_cap_usdc}")
    typer.echo(f"  Exposure max       : ${settings.live_max_open_exposure_usdc}")
    typer.echo(f"  Daily loss limit   : ${settings.live_max_daily_loss_usdc}")
    typer.echo(f"  Min saldo USDC     : ${settings.live_min_usdc_balance}")
    typer.echo(f"  Threshold lucro    : {settings.min_net_profit_pct}%")

    if not confirm:
        typer.echo("")
        typer.echo(f"Pra continuar, digite EXATAMENTE: {CONFIRM_PHRASE}")
        entered = input("> ").strip()
        if entered != CONFIRM_PHRASE:
            typer.echo("Frase incorreta. Abortando.")
            raise typer.Exit(code=1)

    cost_model = CostModel.from_settings(settings)
    detector = IntraMarketDetector(cost_model, min_net_profit_pct=settings.min_net_profit_pct)
    store = OpportunityStore(settings.database_path)
    store.init_schema()
    kill_switch = KillSwitch(settings.kill_switch_path)
    risk_manager = RiskManager.from_settings(settings, kill_switch)

    if kill_switch.is_active():
        typer.echo(f"Kill-switch ATIVO ({kill_switch.reason()}). Use `bot kill-switch --reset` antes.")
        raise typer.Exit(code=2)

    try:
        live_client = PolymarketLiveClient(
            host=settings.poly_clob_host,
            chain_id=settings.poly_chain_id,
            private_key=settings.poly_private_key,
            funder=settings.poly_funder_address,
            signature_type=settings.poly_signature_type,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"ERRO ao inicializar PolymarketLiveClient: {exc}")
        raise typer.Exit(code=3) from exc

    trader = LiveTrader(
        live_client,
        cost_model,
        store,
        risk_manager,
        capital_per_trade_usdc=settings.live_max_capital_per_trade_usdc,
    )

    async def _run() -> None:
        async with GammaClient() as gamma, ClobClient() as clob:
            scanner = IntraMarketScanner(gamma, clob, detector, store)
            await scanner.refresh_cache(min_volume=min_volume, max_pages=max_pages)
            typer.echo(f"  mercados ativos: {len(scanner.cache.markets)}")

            pass_num = 0
            try:
                while True:
                    pass_num += 1
                    if kill_switch.is_active():
                        typer.echo(
                            f"[#{pass_num:04d}] kill-switch acionado: {kill_switch.reason()}"
                        )
                        break
                    result = await scanner.scan_once(max_markets=max_markets)
                    if result.opportunities:
                        result.opportunities.sort(
                            key=lambda o: o.net_pnl_pct, reverse=True
                        )
                        best = result.opportunities[0]
                        typer.echo(
                            f"[#{pass_num:04d}] {result.opportunities_found} arbs - executando best:"
                        )
                        typer.echo(format_opportunity_line(best))
                        outcome = await trader.execute(best)
                        if outcome.submitted and outcome.trade is not None:
                            t = outcome.trade
                            typer.echo(
                                f"  -> {t.leg_status} net=${t.net_pnl_usdc:.2f} "
                                f"matched={t.matched_shares:.2f}"
                            )
                        else:
                            typer.echo(f"  -> rejeitado: {outcome.risk_reason}")
                    else:
                        typer.echo(
                            f"[#{pass_num:04d}] nenhuma arb (varridos {result.markets_scanned})"
                        )
                    if one_shot:
                        break
                    await _asyncio.sleep(interval)
            except (KeyboardInterrupt, _asyncio.CancelledError):
                typer.echo("\nLive encerrado.")

    try:
        _asyncio.run(_run())
    except KeyboardInterrupt:
        pass


@app.command(name="notion-sync")
def notion_sync_cmd(
    once: bool = typer.Option(
        False, "--once", help="Executa um sync e sai (sem loop)."
    ),
    interval: int = typer.Option(
        0,
        "--interval",
        help="Segundos entre syncs em modo loop. 0 = usar NOTION_SYNC_INTERVAL_SECONDS.",
    ),
) -> None:
    """Sincroniza estado do bot (SQLite) para o Notion: trades, opps e dashboard.

    Modo padrao: loop infinito ate Ctrl+C. Use --once para um unico sync
    (util pra cron ou debug).
    """
    import asyncio as _asyncio

    from bot.config.settings import get_settings
    from bot.data.opportunity_store import OpportunityStore
    from bot.economics.cost_model import CostModel
    from bot.monitoring.notion_client import NotionClient
    from bot.monitoring.notion_reporter import NotionReporter

    s = get_settings()
    if not s.notion_configured:
        typer.echo("ERRO: Notion nao configurado. Defina no .env:", err=True)
        typer.echo("  NOTION_TOKEN, NOTION_DASHBOARD_PAGE_ID,", err=True)
        typer.echo("  NOTION_TRADES_DB_ID, NOTION_OPPORTUNITIES_DB_ID", err=True)
        typer.echo("Use `bot notion-bootstrap` para criar a estrutura.", err=True)
        raise typer.Exit(code=2)

    store = OpportunityStore(s.database_path)
    store.init_schema()
    cost_model = CostModel.from_settings(s)
    interval_s = interval if interval > 0 else s.notion_sync_interval_seconds

    async def _run() -> None:
        async with NotionClient(
            s.notion_token,  # type: ignore[arg-type]
            api_version=s.notion_api_version,
            api_base=s.notion_api_base,
        ) as client:
            reporter = NotionReporter(
                store,
                cost_model,
                client,
                dashboard_page_id=s.notion_dashboard_page_id,  # type: ignore[arg-type]
                trades_db_id=s.notion_trades_db_id,  # type: ignore[arg-type]
                opportunities_db_id=s.notion_opportunities_db_id,  # type: ignore[arg-type]
                max_rows_per_run=s.notion_sync_max_rows_per_run,
            )
            try:
                while True:
                    res = await reporter.sync_once()
                    typer.echo(
                        f"sync ok: trades+{res.trades_synced} "
                        f"opps+{res.opportunities_synced} "
                        f"dashboard={'sim' if res.dashboard_updated else 'NAO'} "
                        f"erros={len(res.errors)}"
                    )
                    for err in res.errors:
                        typer.echo(f"  ! {err}", err=True)
                    if once:
                        break
                    await _asyncio.sleep(interval_s)
            except (KeyboardInterrupt, _asyncio.CancelledError):
                typer.echo("\nNotion sync encerrado.")

    try:
        _asyncio.run(_run())
    except KeyboardInterrupt:
        pass


@app.command(name="notion-bootstrap")
def notion_bootstrap_cmd(
    parent_page_id: str = typer.Option(
        ...,
        "--parent-page-id",
        help="ID da pagina parent no Notion onde criar Dashboard + databases.",
    ),
) -> None:
    """Cria Dashboard, Trades DB e Opportunities DB sob a pagina informada.

    Imprime os IDs no final pra voce colar no .env. NAO grava o .env automaticamente.
    """
    import asyncio as _asyncio

    from bot.config.settings import get_settings
    from bot.monitoring.notion_client import NotionClient

    s = get_settings()
    if not s.notion_token:
        typer.echo("ERRO: NOTION_TOKEN nao definido no .env.", err=True)
        raise typer.Exit(code=2)

    parent_clean = parent_page_id.replace("-", "")

    trades_props = {
        "Name": {"title": {}},
        "Trade ID": {"number": {}},
        "Date": {"date": {}},
        "Mode": {
            "select": {
                "options": [
                    {"name": "paper", "color": "blue"},
                    {"name": "live", "color": "red"},
                ]
            }
        },
        "Market ID": {"rich_text": {}},
        "Question": {"rich_text": {}},
        "Size USDC": {"number": {"format": "dollar"}},
        "Gross PnL": {"number": {"format": "dollar"}},
        "Net PnL": {"number": {"format": "dollar"}},
        "Fees": {"number": {"format": "dollar"}},
        "Gas": {"number": {"format": "dollar"}},
        "Fill YES": {"number": {"format": "number"}},
        "Fill NO": {"number": {"format": "number"}},
        "Status": {
            "select": {
                "options": [
                    {"name": "both_filled", "color": "green"},
                    {"name": "partial_yes", "color": "yellow"},
                    {"name": "partial_no", "color": "yellow"},
                    {"name": "failed", "color": "red"},
                ]
            }
        },
    }
    opps_props = {
        "Name": {"title": {}},
        "Opp ID": {"number": {}},
        "Date": {"date": {}},
        "Market ID": {"rich_text": {}},
        "Question": {"rich_text": {}},
        "Ask YES": {"number": {"format": "number"}},
        "Ask NO": {"number": {"format": "number"}},
        "Sum Asks": {"number": {"format": "number"}},
        "Size Max USDC": {"number": {"format": "dollar"}},
        "Gross PnL": {"number": {"format": "dollar"}},
        "Est Fees": {"number": {"format": "dollar"}},
        "Est Gas": {"number": {"format": "dollar"}},
        "Net PnL": {"number": {"format": "dollar"}},
        "Net PnL %": {"number": {"format": "percent"}},
        "Was Traded": {"checkbox": {}},
    }
    dash_placeholder = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": "Aguardando primeiro sync... rode `bot notion-sync --once`."
                        },
                    }
                ]
            },
        }
    ]

    async def _run() -> None:
        async with NotionClient(
            s.notion_token,  # type: ignore[arg-type]
            api_version=s.notion_api_version,
            api_base=s.notion_api_base,
        ) as client:
            trades_db = await client.create_database(
                parent_clean, "Trades", trades_props
            )
            opps_db = await client.create_database(
                parent_clean, "Opportunities", opps_props
            )
            dash = await client.create_subpage(
                parent_clean, "Dashboard", emoji="📊", children=dash_placeholder
            )

            typer.echo("\n== IDs criados (cole no .env) ==\n")
            typer.echo(f"NOTION_TRADES_DB_ID={trades_db['id']}")
            typer.echo(f"NOTION_OPPORTUNITIES_DB_ID={opps_db['id']}")
            typer.echo(f"NOTION_DASHBOARD_PAGE_ID={dash['id']}")

    _asyncio.run(_run())


if __name__ == "__main__":
    app()
