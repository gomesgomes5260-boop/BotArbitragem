# BotArbitragem

Bot de arbitragem para a [Polymarket](https://polymarket.com) — plataforma de mercados de previsao em Polygon (USDC) com Central Limit Order Book (CLOB).

**Status atual:** Fase 6 — operacao 24/7 em VPS. Paper trading + dashboard no Notion.

## Estrategia v1

Arbitragem **intra-mercado YES + NO**: em todo mercado binario da Polymarket, `preco(YES) + preco(NO)` deveria somar US$ 1. Quando soma < $1 (descontados taxas + gas + slippage), comprar ambos ao mesmo tempo trava lucro garantido na liquidacao do mercado.

## Roadmap

| Fase | Entregavel |
|---|---|
| 0 | Fundacao: estrutura de modulos, settings, CLI esqueleto, testes smoke |
| 1 | Acesso read-only a Polymarket (Gamma API + CLOB REST + WS) |
| 2 | Detector de oportunidades em tempo real (so log) |
| 2.5 | Modelo de custos + schema SQLite com P&L liquido |
| 3 | Backtesting via replay de snapshots |
| 4 | Paper trading |
| 5 | Execucao real com capital pequeno |
| 6 | Operacao 24/7 em VPS com observabilidade |
| 7 | Multi-outcome e iteracoes |

Plano completo: `/root/.claude/plans/claude-vamos-do-organizar-curried-bubble.md`.

## Setup

```bash
# 1. Clonar e entrar
git clone <repo>
cd BotArbitragem

# 2. Criar venv e instalar
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 3. Configurar
cp .env.example .env
# Edite .env com seus valores (NAO comite o .env)

# 4. Validar
pytest -q
python -m bot.cli --help
python -m bot.cli settings-check
```

## Aviso de seguranca

- **Carteira dedicada:** crie uma nova carteira Polygon exclusiva para o bot. Nunca use a sua carteira pessoal.
- **`.env` nunca vai pro Git:** ja esta no `.gitignore`. Conferir antes de qualquer commit.
- **Capital pequeno primeiro:** comecar com US$ 5–20 por trade na Fase 5. Escalar so apos consistencia em paper trading.
- **Kill-switch:** sempre testar em dry-run antes de confiar em producao.

## Estrutura

```
bot/
  config/        # settings (pydantic-settings)
  clients/       # wrappers Gamma / CLOB / WS (Fase 1)
  data/          # cache de mercados, store SQLite (Fase 1+)
  economics/     # modelo de custos, gas oracle (Fase 2.5)
  strategies/    # detector intra-market YES+NO (Fase 2)
  execution/     # paper / live trader (Fase 4+)
  risk/          # limites, kill-switch (Fase 5)
  monitoring/    # logging, relatorios de P&L (Fase 2.5+)
  backtest/      # replay de snapshots (Fase 3)
  cli/           # entrypoints typer
tests/
```

## Dashboard no Notion

Sync periodico do estado do bot (trades, oportunidades, P&L diario, top mercados) pra uma pagina Notion. Foco em analise de profit. Renderiza em-place a cada `NOTION_SYNC_INTERVAL_SECONDS` (default 15min).

### Setup (rodar na VPS)

```bash
# 1) No Notion: cria pagina "BotArbitragem" e conecta a integracao Claude/Notion
#    (ou cria uma integracao em https://www.notion.so/profile/integrations
#     e compartilha a pagina com ela). Copia o ID da pagina (ultimos 32 chars do URL).

# 2) Adiciona NOTION_TOKEN ao .env (secret_xxx ou ntn_xxx).

# 3) Bootstrap: cria Trades DB, Opportunities DB e Dashboard sob a pagina parent.
.venv/bin/bot notion-bootstrap --parent-page-id <PAGE_ID>
# Imprime 3 IDs - cola no .env: NOTION_TRADES_DB_ID, NOTION_OPPORTUNITIES_DB_ID,
# NOTION_DASHBOARD_PAGE_ID.

# 4) Primeiro sync de teste:
.venv/bin/bot notion-sync --once

# 5) Loop continuo via systemd (recomendado):
INSTALL_NOTION_SYNC=1 bash scripts/setup_vps.sh
sudo systemctl enable --now bot-notion-sync
sudo journalctl -u bot-notion-sync -f
```

O sync e idempotente: tabela `notion_sync_state` no SQLite guarda o ultimo `id` enviado por entidade (`trades`, `opportunities`), entao reinicios nao duplicam nada. Cap configuravel (`NOTION_SYNC_MAX_ROWS_PER_RUN`, default 50) protege rate limit do Notion ao recuperar de janelas longas offline.

## Branch

Desenvolvimento ocorre em `claude/polymarket-arbitrage-bot-1odkJ`.
