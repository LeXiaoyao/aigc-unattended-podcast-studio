#!/bin/zsh

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ENV_FILE="${ROOT_DIR}/shared/config/.env.listenhub"
FALLBACK_ENV_FILE="${ROOT_DIR}/.env"
LOG_DIR="${ROOT_DIR}/logs"
SOLO_PUBLISH_MODE="${SOLO_PUBLISH_MODE:-auto}"

# Ensure Homebrew tools (ffmpeg, ffprobe, python3) are on PATH for launchd
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:${PATH}"

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

PYTHON3="/opt/homebrew/bin/python3"
if [[ ! -x "${PYTHON3}" ]]; then
  PYTHON3="python3"
fi

cmd=(
  "${PYTHON3}" "${ROOT_DIR}/solo_pipeline.py" build-episode
  --cover-provider none
  --draft-only
)

# SOLO_FORCE=1 from admin panel triggers skip schedule check
if [[ "${SOLO_FORCE:-}" == "1" ]]; then
  cmd+=(--force)
fi

if [[ "${SOLO_PUBLISH_MODE}" == "auto" ]]; then
  log "开始执行单人口播日报草稿生成（不含 TTS/发布，等待 Codex 稿后手动合成）。"
else
  log "开始执行单人口播日报草稿生成（不含 TTS/发布）。"
fi

# 合并 stderr 到 stdout，确保 dashboard 日志能看到所有输出（含错误）
if "${cmd[@]}" 2>&1; then
  log "单人口播日报草稿生成完成，等待 Codex 稿后在 webapp 合成 TTS。"
else
  log "单人口播日报生成失败 (exit code: $?)，请检查日志。"
  "${PYTHON3}" "${ROOT_DIR}/mobile_notify.py" "AI观测站 · 日报生成失败" "步骤：run_solo_podcast_daily.sh 总入口。请检查 solo-podcast-generator 日志。" --no-desktop >/dev/null 2>&1 || true
  exit 1
fi
