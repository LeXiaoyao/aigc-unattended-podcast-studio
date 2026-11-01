#!/bin/zsh
# 飞书长连接桥启动器（launchd KeepAlive 常驻）

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
ENV_FILE="${ROOT_DIR}/shared/config/.env.listenhub"
FALLBACK_ENV_FILE="${ROOT_DIR}/.env"

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:${PATH}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
elif [[ -f "${FALLBACK_ENV_FILE}" ]]; then
  set -a
  source "${FALLBACK_ENV_FILE}"
  set +a
fi

PYTHON3="/opt/homebrew/bin/python3"
if [[ ! -x "${PYTHON3}" ]]; then
  PYTHON3="python3"
fi

exec "${PYTHON3}" "${ROOT_DIR}/feishu_bridge.py"
