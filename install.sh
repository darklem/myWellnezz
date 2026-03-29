#!/usr/bin/env bash
# =============================================================================
#  myWellnezz – Installation script
#  Installs the web service and registers it as a systemd unit.
#
#  Usage:
#    sudo ./install.sh              # Install as a system service (runs as $SUDO_USER)
#    sudo ./install.sh --uninstall  # Remove the service and installation directory
# =============================================================================
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/mywellnezz"
SERVICE_NAME="mywellnezz"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}@.service"
ENV_FILE="/etc/mywellnezz.conf"
PYTHON="${PYTHON:-python3}"
PORT="${MYWELLNEZZ_PORT:-8080}"
HOST="${MYWELLNEZZ_HOST:-0.0.0.0}"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERR]${RESET}   $*" >&2; exit 1; }

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Ce script doit être exécuté avec sudo."

# Determine the non-root user who called sudo
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

# ── Uninstall ─────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    info "Désinstallation de myWellnezz…"
    systemctl stop  "${SERVICE_NAME}@${RUN_USER}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}@${RUN_USER}" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    rm -f "$ENV_FILE"
    systemctl daemon-reload
    warn "Le répertoire d'installation ${INSTALL_DIR} n'a pas été supprimé (données conservées)."
    warn "Supprimez-le manuellement avec: rm -rf ${INSTALL_DIR}"
    success "Service myWellnezz supprimé."
    exit 0
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║      myWellnezz – Installation          ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${RESET}"

# ── Prerequisite checks ───────────────────────────────────────────────────────
info "Vérification des prérequis…"

command -v "$PYTHON" >/dev/null 2>&1 || error "Python 3 introuvable. Installez python3."
command -v pip3      >/dev/null 2>&1 || error "pip3 introuvable. Installez python3-pip."

PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python version : $PY_VERSION"

# Require >= 3.11
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
[[ $PY_MAJOR -lt 3 || ($PY_MAJOR -eq 3 && $PY_MINOR -lt 11) ]] \
    && error "Python 3.11+ requis (trouvé $PY_VERSION)."

success "Python $PY_VERSION OK"

# ── Create installation directory ─────────────────────────────────────────────
info "Création du répertoire d'installation : ${INSTALL_DIR}"
mkdir -p "$INSTALL_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Copie des sources depuis : ${SCRIPT_DIR}"
cp -r "${SCRIPT_DIR}/." "${INSTALL_DIR}/"
chown -R "${RUN_USER}:${RUN_USER}" "$INSTALL_DIR"

success "Sources copiées."

# ── Create virtual environment ────────────────────────────────────────────────
VENV_DIR="${INSTALL_DIR}/venv"
if [[ ! -d "$VENV_DIR" ]]; then
    info "Création de l'environnement virtuel…"
    sudo -u "$RUN_USER" "$PYTHON" -m venv "$VENV_DIR"
fi

VENV_PIP="${VENV_DIR}/bin/pip"
VENV_PYTHON="${VENV_DIR}/bin/python"

info "Mise à jour de pip…"
sudo -u "$RUN_USER" "$VENV_PIP" install --quiet --upgrade pip

# ── Install dependencies ──────────────────────────────────────────────────────
info "Installation des dépendances Python…"
if [[ -f "${INSTALL_DIR}/requirements-web.txt" ]]; then
    sudo -u "$RUN_USER" "$VENV_PIP" install --quiet -r "${INSTALL_DIR}/requirements-web.txt"
elif [[ -f "${INSTALL_DIR}/pyproject.toml" ]]; then
    # Try poetry first, fall back to direct pip install
    if command -v poetry >/dev/null 2>&1; then
        info "Installation via Poetry…"
        cd "$INSTALL_DIR"
        sudo -u "$RUN_USER" poetry install --no-interaction 2>/dev/null \
            || sudo -u "$RUN_USER" "$VENV_PIP" install --quiet \
               fastapi "uvicorn[standard]" jinja2 aiohttp python-dateutil \
               loguru prettytable colorama psutil pillow pwinput aioconsole mouse
    else
        info "Poetry absent – installation directe via pip…"
        sudo -u "$RUN_USER" "$VENV_PIP" install --quiet \
            fastapi "uvicorn[standard]" jinja2 aiohttp python-dateutil \
            loguru prettytable colorama psutil pillow pwinput aioconsole mouse
    fi
else
    error "Aucun fichier de dépendances trouvé."
fi

success "Dépendances installées."

# ── Verify web app imports ────────────────────────────────────────────────────
info "Vérification des imports…"
cd "$INSTALL_DIR"
sudo -u "$RUN_USER" "$VENV_PYTHON" -c "
import sys
sys.path.insert(0, '${INSTALL_DIR}/mywellnezz')
from fastapi import FastAPI
from uvicorn import Config as UvConfig
print('FastAPI OK')
" || error "Vérification des imports échouée."
success "Imports OK."

# ── Environment file ──────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    info "Création du fichier de configuration : ${ENV_FILE}"
    cat > "$ENV_FILE" <<EOF
# myWellnezz environment configuration
# Edit this file to customise the service behaviour.

MYWELLNEZZ_HOST=${HOST}
MYWELLNEZZ_PORT=${PORT}
EOF
    chmod 640 "$ENV_FILE"
    chown "root:${RUN_USER}" "$ENV_FILE"
    success "Fichier de configuration créé."
else
    warn "${ENV_FILE} existe déjà – conservé sans modification."
fi

# ── Systemd service ───────────────────────────────────────────────────────────
info "Installation du service systemd…"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=myWellnezz – Gym Class Booking Web Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=%i
Group=%i
WorkingDirectory=${INSTALL_DIR}

ExecStart=${VENV_DIR}/bin/python -m uvicorn mywellnezz.web_app:app \\
    --host \${MYWELLNEZZ_HOST:-0.0.0.0} \\
    --port \${MYWELLNEZZ_PORT:-8080} \\
    --log-level info

EnvironmentFile=-${ENV_FILE}

Restart=on-failure
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=3

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${INSTALL_DIR}

StandardOutput=journal
StandardError=journal
SyslogIdentifier=mywellnezz

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable  "${SERVICE_NAME}@${RUN_USER}"
systemctl restart "${SERVICE_NAME}@${RUN_USER}"

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}@${RUN_USER}"; then
    success "Service ${SERVICE_NAME}@${RUN_USER} démarré avec succès."
else
    warn "Le service n'a pas démarré. Consultez les logs :"
    warn "  journalctl -u ${SERVICE_NAME}@${RUN_USER} -n 30 --no-pager"
    exit 1
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║  Installation terminée avec succès !                    ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${RESET}"
echo
echo -e "  ${BOLD}Interface web :${RESET}  http://localhost:${PORT}"
echo -e "  ${BOLD}Logs :${RESET}           journalctl -u ${SERVICE_NAME}@${RUN_USER} -f"
echo -e "  ${BOLD}Arrêter :${RESET}        sudo systemctl stop ${SERVICE_NAME}@${RUN_USER}"
echo -e "  ${BOLD}Redémarrer :${RESET}     sudo systemctl restart ${SERVICE_NAME}@${RUN_USER}"
echo -e "  ${BOLD}Désinstaller :${RESET}   sudo ./install.sh --uninstall"
echo
