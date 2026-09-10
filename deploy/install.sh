#!/usr/bin/env bash
#
# Installiert TANSS Calendar Sync als systemd-Dienst auf Ubuntu.
# Getestet mit Ubuntu 22.04 LTS und 24.04 LTS.
#
# Aufruf:   sudo ./deploy/install.sh
#
# Das Skript ist idempotent: es kann zum Aktualisieren erneut ausgefuehrt
# werden, ohne Konfiguration oder Zustandsdaten zu ueberschreiben.

set -euo pipefail

APP_NAME="tanss-calendar-sync"
SERVICE_USER="tanss-sync"
INSTALL_DIR="/opt/${APP_NAME}"
CONFIG_DIR="/etc/${APP_NAME}"
STATE_DIR="/var/lib/${APP_NAME}"
LOG_DIR="/var/log/${APP_NAME}"
VENV_DIR="${INSTALL_DIR}/.venv"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m!!!\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "Bitte mit sudo ausfuehren."

# --- 1. Systemvoraussetzungen ---------------------------------------------
info "Pruefe Systemvoraussetzungen"

command -v systemctl >/dev/null || die "systemd nicht gefunden."

if ! command -v python3 >/dev/null; then
  die "python3 ist nicht installiert. Nachholen mit: apt install python3"
fi

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_MAJOR="${PY_VERSION%%.*}"
PY_MINOR="${PY_VERSION##*.}"
if (( PY_MAJOR < 3 || (PY_MAJOR == 3 && PY_MINOR < 11) )); then
  die "Python 3.11 oder neuer wird benoetigt, gefunden: ${PY_VERSION}"
fi
info "Python ${PY_VERSION} gefunden"

if ! python3 -c 'import venv' 2>/dev/null; then
  info "Installiere python3-venv"
  apt-get update -qq && apt-get install -y -qq python3-venv
fi

# --- 2. Dienstbenutzer ------------------------------------------------------
if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  info "Dienstbenutzer ${SERVICE_USER} existiert bereits"
else
  info "Lege Dienstbenutzer ${SERVICE_USER} an (ohne Login-Shell)"
  useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

# --- 3. Verzeichnisse -------------------------------------------------------
info "Lege Verzeichnisse an"
install -d -m 0755 -o root            -g root            "${INSTALL_DIR}"
install -d -m 0750 -o root            -g "${SERVICE_USER}" "${CONFIG_DIR}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${STATE_DIR}"
install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${LOG_DIR}"

# --- 4. Anwendung -----------------------------------------------------------
info "Kopiere Anwendung nach ${INSTALL_DIR}"
rsync -a --delete \
      --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
      --exclude 'config.json' --exclude '*.pem' \
      "${REPO_DIR}/" "${INSTALL_DIR}/"

info "Erzeuge virtuelle Python-Umgebung"
if [[ ! -d "${VENV_DIR}" ]]; then
  python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet "${INSTALL_DIR}"

chown -R root:root "${INSTALL_DIR}"
chmod -R go-w "${INSTALL_DIR}"

# --- 5. Konfiguration -------------------------------------------------------
if [[ -f "${CONFIG_DIR}/config.json" ]]; then
  info "Bestehende Konfiguration bleibt unveraendert"
else
  warn "Noch keine Konfiguration vorhanden."
  warn "Nach der Installation einmalig ausfuehren:"
  warn "    sudo -u ${SERVICE_USER} ${VENV_DIR}/bin/tanss-sync setup --config ${CONFIG_DIR}/config.json"
fi
touch "${CONFIG_DIR}/token"
chown root:"${SERVICE_USER}" "${CONFIG_DIR}/token"
chmod 0640 "${CONFIG_DIR}/token"

# --- 6. systemd -------------------------------------------------------------
info "Installiere systemd-Unit"
install -m 0644 "${SCRIPT_DIR}/${APP_NAME}.service" "/etc/systemd/system/${APP_NAME}.service"
systemctl daemon-reload

# enable sorgt fuer den Start nach jedem Reboot.
info "Aktiviere Dienst fuer den Systemstart"
systemctl enable "${APP_NAME}.service" >/dev/null

if [[ -f "${CONFIG_DIR}/config.json" ]]; then
  info "Starte Dienst neu"
  systemctl restart "${APP_NAME}.service"
  sleep 2
  systemctl --no-pager --lines=0 status "${APP_NAME}.service" || true
else
  warn "Dienst ist aktiviert, wird aber erst nach der Einrichtung gestartet."
fi

cat <<EOF

Installation abgeschlossen.

  Konfiguration   ${CONFIG_DIR}/config.json
  Zustand         ${STATE_DIR}/state.db
  Logs            journalctl -u ${APP_NAME} -f
  Status          systemctl status ${APP_NAME}
  Neustart        systemctl restart ${APP_NAME}

Der Dienst startet nach einem Reboot automatisch mit.

EOF
