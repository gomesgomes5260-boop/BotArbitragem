#!/usr/bin/env bash
# Setup do bot de arbitragem em VPS Ubuntu 24.04+ (DigitalOcean, Hetzner, etc).
#
# Uso (no SSH do VPS, como root):
#   curl -fsSL https://raw.githubusercontent.com/gomesgomes5260-boop/BotArbitragem/claude/polymarket-arbitrage-bot-1odkJ/scripts/setup_vps.sh | bash
#
# O que faz:
# 1. Atualiza apt e instala python3, venv, pip, git, tmux
# 2. Clona o repo na branch de desenvolvimento
# 3. Cria venv e instala dependencias
# 4. Copia .env.example -> .env (mode paper nao precisa de creds reais)
# 5. Roda pytest pra validar
# 6. Imprime proximos passos

set -euo pipefail

REPO_URL="https://github.com/gomesgomes5260-boop/BotArbitragem.git"
BRANCH="claude/polymarket-arbitrage-bot-1odkJ"
INSTALL_DIR="${INSTALL_DIR:-$HOME/BotArbitragem}"

if [ "$EUID" -eq 0 ]; then
    SUDO=""
else
    SUDO="sudo"
fi

echo "==> Atualizando apt e instalando dependencias do sistema..."
$SUDO apt-get update -y
$SUDO apt-get install -y python3 python3-venv python3-pip git tmux ca-certificates

echo "==> Versao do Python:"
python3 --version

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "==> Repositorio ja existe em $INSTALL_DIR, atualizando..."
    cd "$INSTALL_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull --ff-only origin "$BRANCH"
else
    echo "==> Clonando repositorio em $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    git checkout "$BRANCH"
fi

echo "==> Criando virtualenv (.venv)..."
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    python3 -m venv .venv
fi

echo "==> Atualizando pip e instalando dependencias (pode demorar 2-5 min)..."
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -e ".[dev]" --quiet

if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "==> Criando .env a partir do .env.example..."
    cp .env.example .env
    chmod 600 .env
else
    echo "==> .env ja existe, mantendo."
fi

echo "==> Rodando testes pra confirmar setup..."
.venv/bin/pytest -q

# -----------------------------------------------------------------
# Paper monitor service (opcional, ativa com INSTALL_PAPER_MONITOR=1)
# -----------------------------------------------------------------
if [ "${INSTALL_PAPER_MONITOR:-0}" = "1" ]; then
    echo "==> Instalando systemd service do paper monitor..."
    PMS_FILE="/etc/systemd/system/bot-paper-monitor.service"
    PMS_USER="${SUDO_USER:-$(whoami)}"
    sed \
        -e "s|__USER__|${PMS_USER}|g" \
        -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
        "${INSTALL_DIR}/scripts/bot-paper-monitor.service" \
        | $SUDO tee "$PMS_FILE" > /dev/null
    $SUDO systemctl daemon-reload
    echo "    Service instalado em $PMS_FILE."
    echo "    Para ativar:"
    echo "      $SUDO systemctl enable --now bot-paper-monitor"
    echo "      $SUDO journalctl -u bot-paper-monitor -f"
fi

# -----------------------------------------------------------------
# Notion sync service (opcional, ativa com INSTALL_NOTION_SYNC=1)
# -----------------------------------------------------------------
if [ "${INSTALL_NOTION_SYNC:-0}" = "1" ]; then
    echo "==> Instalando systemd service para sync do Notion..."
    SERVICE_FILE="/etc/systemd/system/bot-notion-sync.service"
    SERVICE_USER="${SUDO_USER:-$(whoami)}"
    sed \
        -e "s|__USER__|${SERVICE_USER}|g" \
        -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
        "${INSTALL_DIR}/scripts/bot-notion-sync.service" \
        | $SUDO tee "$SERVICE_FILE" > /dev/null
    $SUDO systemctl daemon-reload
    echo "    Service instalado em $SERVICE_FILE."
    echo "    Para ativar (depois de configurar NOTION_* no .env):"
    echo "      $SUDO systemctl enable --now bot-notion-sync"
    echo "      $SUDO journalctl -u bot-notion-sync -f"
fi

echo ""
echo "================================================================"
echo "  SETUP COMPLETO. Proximos passos:"
echo "================================================================"
echo ""
echo "  cd $INSTALL_DIR"
echo "  source .venv/bin/activate"
echo ""
echo "  # 1) Smoke test (sem capital, so leitura):"
echo "  python -m bot.cli list-markets --min-volume 10000"
echo ""
echo "  # 2) Paper trading no tmux (sobrevive desconexao SSH):"
echo "  tmux new -s bot"
echo "  source .venv/bin/activate && python -m bot.cli monitor --paper --interval 5"
echo "  # Ctrl+B  D  =  desconecta (bot continua rodando)"
echo "  # tmux attach -t bot  =  reconecta a qualquer hora"
echo ""
echo "  # 3) Apos 48-72h, conferir resultado:"
echo "  python -m bot.cli pnl --period 7d --mode paper"
echo ""
echo "  # 4) Dashboard no Notion (opcional):"
echo "  #    a) Cole NOTION_TOKEN no .env"
echo "  #    b) Crie pagina parent no Notion + connect a integracao Claude/Notion"
echo "  #    c) Bootstrap: python -m bot.cli notion-bootstrap --parent-page-id XXX"
echo "  #    d) Cole os 3 IDs retornados no .env"
echo "  #    e) Service: INSTALL_NOTION_SYNC=1 bash scripts/setup_vps.sh"
echo "  #       sudo systemctl enable --now bot-notion-sync"
echo ""
echo "  Edite .env so se quiser mudar limites (paper roda com defaults)."
echo "================================================================"
