#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL_DIR="/opt/cryptobot-userbot"
SERVICE_USER="telegram-userbot"
PURGE_DATA=0

if [[ ${1:-} == "--purge-data" ]]; then
  PURGE_DATA=1
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Запустите через sudo"
  exit 1
fi

systemctl disable --now cryptobot-userbot.service 2>/dev/null || true
rm -f /etc/systemd/system/cryptobot-userbot.service
systemctl daemon-reload

if [[ ${PURGE_DATA} -eq 1 ]]; then
  if [[ "${INSTALL_DIR}" != "/opt/cryptobot-userbot" ]]; then
    echo "Некорректный путь удаления"
    exit 1
  fi
  rm -rf -- "${INSTALL_DIR}"
  userdel "${SERVICE_USER}" 2>/dev/null || true
  echo "Сервис и приватные данные удалены без возможности восстановления"
else
  echo "Сервис удалён. Приватные данные сохранены в ${INSTALL_DIR}"
  echo "Для полного удаления: sudo bash scripts/uninstall_ubuntu.sh --purge-data"
fi

