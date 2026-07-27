#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Запустите через sudo: sudo bash scripts/install_ubuntu.sh"
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/cryptobot-userbot"
SERVICE_USER="telegram-userbot"
SERVICE_WAS_ACTIVE=0
RESTART_NEEDED=0
INSTALL_SUCCEEDED=0
BACKUP_DIR=""

restore_service() {
  if [[ ${INSTALL_SUCCEEDED} -eq 0 && -n "${BACKUP_DIR}" && -d "${BACKUP_DIR}" ]]; then
    rsync -a --delete \
      --exclude='.env' \
      --exclude='session' \
      --exclude='data' \
      "${BACKUP_DIR}/" "${INSTALL_DIR}/" || true
    if [[ -f "${BACKUP_DIR}/deploy/cryptobot-userbot.service" ]]; then
      install -m 0644 "${BACKUP_DIR}/deploy/cryptobot-userbot.service" \
        /etc/systemd/system/cryptobot-userbot.service || true
      systemctl daemon-reload || true
    fi
  fi
  if [[ ${RESTART_NEEDED} -eq 1 ]]; then
    systemctl start cryptobot-userbot.service || true
  fi
  if [[ -n "${BACKUP_DIR}" && -d "${BACKUP_DIR}" ]]; then
    rm -rf -- "${BACKUP_DIR}"
  fi
}
trap restore_service EXIT

if [[ "${SOURCE_DIR}" == "${INSTALL_DIR}" ]]; then
  echo "Запускайте установщик из распакованной исходной папки, не из ${INSTALL_DIR}"
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv rsync sqlite3

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "${INSTALL_DIR}" --create-home \
    --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
  "${INSTALL_DIR}" "${INSTALL_DIR}/session" "${INSTALL_DIR}/data"

if systemctl is-active --quiet cryptobot-userbot.service 2>/dev/null; then
  SERVICE_WAS_ACTIVE=1
  RESTART_NEEDED=1
  systemctl stop cryptobot-userbot.service
fi

if [[ -f "${INSTALL_DIR}/app/main.py" ]]; then
  BACKUP_DIR="$(mktemp -d /opt/cryptobot-userbot-backup.XXXXXX)"
  rsync -a \
    --exclude='.env' \
    --exclude='session' \
    --exclude='data' \
    "${INSTALL_DIR}/" "${BACKUP_DIR}/"
fi

rsync -a --delete \
  --exclude='.env' \
  --exclude='.venv' \
  --exclude='.git' \
  --exclude='.pytest_cache' \
  --exclude='session' \
  --exclude='data' \
  --exclude='release' \
  --exclude='tests' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.zip' \
  --exclude='*.pem' \
  "${SOURCE_DIR}/" "${INSTALL_DIR}/"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 0700 "${INSTALL_DIR}" "${INSTALL_DIR}/session" "${INSTALL_DIR}/data"

if [[ ! -d "${INSTALL_DIR}/.venv" ]]; then
  runuser -u "${SERVICE_USER}" -- python3 -m venv "${INSTALL_DIR}/.venv"
fi
runuser -u "${SERVICE_USER}" -- \
  "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
runuser -u "${SERVICE_USER}" -- \
  "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
runuser -u "${SERVICE_USER}" -- \
  "${INSTALL_DIR}/.venv/bin/python" -m compileall -q "${INSTALL_DIR}/app"

if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  install -m 0600 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
fi
chmod 0600 "${INSTALL_DIR}/.env"

install -m 0644 "${INSTALL_DIR}/deploy/cryptobot-userbot.service" \
  /etc/systemd/system/cryptobot-userbot.service
systemctl daemon-reload
INSTALL_SUCCEEDED=1

if [[ ${SERVICE_WAS_ACTIVE} -eq 1 ]]; then
  systemctl start cryptobot-userbot.service
  RESTART_NEEDED=0
fi

echo
echo "Установка завершена. Теперь:"
echo "1) sudo nano ${INSTALL_DIR}/.env"
echo "2) cd ${INSTALL_DIR} && sudo -u ${SERVICE_USER} .venv/bin/python -m app.auth"
echo "3) sudo systemctl enable --now cryptobot-userbot"
echo "4) sudo journalctl -u cryptobot-userbot -f"
