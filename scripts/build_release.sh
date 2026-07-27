#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(tr -d '[:space:]' < "${ROOT_DIR}/VERSION")"
OUTPUT="${1:-${ROOT_DIR}/release/cryptobot-link-catcher-v${VERSION}.zip}"
STAGE_ROOT="$(mktemp -d)"
STAGE_DIR="${STAGE_ROOT}/cryptobot-link-catcher"

cleanup() {
  rm -rf -- "${STAGE_ROOT}"
}
trap cleanup EXIT

mkdir -p "${STAGE_DIR}/app" "${STAGE_DIR}/deploy" \
  "${STAGE_DIR}/scripts" "${STAGE_DIR}/tests" "$(dirname "${OUTPUT}")"

install -m 0644 "${ROOT_DIR}/.env.example" "${STAGE_DIR}/.env.example"
install -m 0644 "${ROOT_DIR}/.gitignore" "${STAGE_DIR}/.gitignore"
install -m 0644 "${ROOT_DIR}/README.md" "${STAGE_DIR}/README.md"
install -m 0644 "${ROOT_DIR}/CHANGELOG.md" "${STAGE_DIR}/CHANGELOG.md"
install -m 0644 "${ROOT_DIR}/SECURITY.md" "${STAGE_DIR}/SECURITY.md"
install -m 0644 "${ROOT_DIR}/LICENSE.txt" "${STAGE_DIR}/LICENSE.txt"
install -m 0644 "${ROOT_DIR}/VERSION" "${STAGE_DIR}/VERSION"
install -m 0644 "${ROOT_DIR}/requirements.txt" "${STAGE_DIR}/requirements.txt"
install -m 0644 "${ROOT_DIR}/requirements-dev.txt" \
  "${STAGE_DIR}/requirements-dev.txt"
install -m 0644 "${ROOT_DIR}/pyproject.toml" "${STAGE_DIR}/pyproject.toml"
install -m 0644 "${ROOT_DIR}"/app/*.py "${STAGE_DIR}/app/"
install -m 0644 "${ROOT_DIR}"/deploy/* "${STAGE_DIR}/deploy/"
install -m 0755 "${ROOT_DIR}"/scripts/*.sh "${STAGE_DIR}/scripts/"
install -m 0644 "${ROOT_DIR}"/tests/*.py "${STAGE_DIR}/tests/"

if rg -n -i \
  --glob '!**/build_release.sh' \
  '(BEGIN (RSA |OPENSSH )?PRIVATE KEY|API_HASH=[a-f0-9]{20,}|CONTROL_BOT_TOKEN=[0-9]{6,}:|/home/[^/[:space:]]+/)' \
  "${STAGE_DIR}"; then
  echo "Проверка приватности релиза не пройдена"
  exit 1
fi

(
  cd "${STAGE_ROOT}"
  zip -q -r "${STAGE_ROOT}/release.zip" cryptobot-link-catcher
)
install -m 0644 "${STAGE_ROOT}/release.zip" "${OUTPUT}"
(
  cd "$(dirname "${OUTPUT}")"
  sha256sum "$(basename "${OUTPUT}")" > "$(basename "${OUTPUT}").sha256"
)
echo "${OUTPUT}"
echo "${OUTPUT}.sha256"
