#!/bin/zsh

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
LOG_DIR="${ROOT_DIR}/logs"
AUTH_REFRESH_SCRIPT="${ROOT_DIR}/xiaoyuzhou_auth_refresh.py"
ENV_FILE="${ROOT_DIR}/.env.listenhub"
NOTIFY_SCRIPT="${ROOT_DIR}/mobile_notify.py"

mkdir -p "${LOG_DIR}"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  echo "[$(timestamp)] $*"
}

notify() {
  local title="$1"
  local body="$2"
  if [[ -f "${NOTIFY_SCRIPT}" ]]; then
    python3 "${NOTIFY_SCRIPT}" "${title}" "${body}" --no-desktop >/dev/null 2>&1 || true
  fi
}

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

log "开始刷新小宇宙 token。"
python3 "${AUTH_REFRESH_SCRIPT}" refresh --notify-on-failure >/dev/null
log "小宇宙 token 刷新完成。"
notify "小宇宙 token 刷新成功" "定时刷新任务已完成：$(timestamp)"
