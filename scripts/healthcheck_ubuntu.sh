#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="/opt/cryptobot-userbot"
SERVICE="cryptobot-userbot.service"

if [[ ${EUID} -ne 0 ]]; then
  echo "Запустите: sudo bash scripts/healthcheck_ubuntu.sh"
  exit 1
fi

systemctl is-enabled --quiet "${SERVICE}"
systemctl is-active --quiet "${SERVICE}"
runuser -u telegram-userbot -- bash -c \
  "cd '${INSTALL_DIR}' && .venv/bin/python -c \"import telethon, aiosqlite, dotenv; from app import __version__; print('VERSION=' + __version__)\""

SESSION_FILE="${INSTALL_DIR}/session/telegram_user.session"
[[ -f "${SESSION_FILE}" ]]
[[ "$(stat -c '%a' "${SESSION_FILE}")" == "600" ]]
[[ "$(stat -c '%a' "${INSTALL_DIR}/.env")" == "600" ]]

DB_FILE="${INSTALL_DIR}/data/processed.sqlite3"
[[ -f "${DB_FILE}" ]]
[[ "$(sqlite3 "${DB_FILE}" 'PRAGMA quick_check;')" == "ok" ]]
[[ "$(sqlite3 "${DB_FILE}" \
  "SELECT value FROM runtime_settings WHERE key='worker_connected';")" == "1" ]]
[[ "$(sqlite3 "${DB_FILE}" \
  "SELECT COUNT(*) FROM system_ignored_chats;")" -ge 1 ]]

echo "SERVICE=active"
echo "DATABASE=ok"
echo "PERMISSIONS=ok"
echo "HEALTHCHECK=passed"
