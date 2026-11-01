#!/bin/zsh

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ENV_FILE="${ROOT_DIR}/shared/config/.env.listenhub"
FALLBACK_ENV_FILE="${ROOT_DIR}/.env"
LOG_DIR="${ROOT_DIR}/logs"

# Ensure Homebrew tools (ffmpeg, ffprobe, python3) are on PATH for launchd
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:${PATH}"

PYTHON3="/opt/homebrew/bin/python3"
if [[ ! -x "${PYTHON3}" ]]; then
  PYTHON3="python3"
fi

mkdir -p "${LOG_DIR}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
elif [[ -f "${FALLBACK_ENV_FILE}" ]]; then
  set -a
  source "${FALLBACK_ENV_FILE}"
  set +a
else
  log "缺少环境文件: ${ENV_FILE} 或 ${FALLBACK_ENV_FILE}"
  exit 1
fi

# Check schedule config — skip if dual_host not scheduled for today
SHOULD_RUN=$("${PYTHON3}" -c "
import json, datetime, sys
try:
    cfg = json.load(open('${ROOT_DIR}/schedule_config.json'))
    dh = cfg.get('dual_host', {})
    if not dh.get('enabled', True):
        print('no')
        sys.exit(0)
    today = datetime.datetime.now().weekday()  # 0=Mon, 4=Fri, 6=Sun
    days = dh.get('days_of_week', list(range(7)))
    print('yes' if today in days else 'no')
except Exception:
    print('yes')  # If config unreadable, proceed
" 2>/dev/null || echo "yes")

if [[ "${SHOULD_RUN}" != "yes" ]]; then
  log "今天不在双人口播调度日，跳过。可通过 schedule_config.json 调整。"
  exit 0
fi

log "开始执行 AI 播客双人口播生成。"

"${PYTHON3}" "${ROOT_DIR}/ai_news_pipeline.py" build-episode \
  --audio-mode listenhub \
  --cover-provider gemini

log "AI 播客双人口播生成请求已提交，等待下载脚本轮询成品。"
