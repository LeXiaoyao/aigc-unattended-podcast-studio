#!/bin/zsh

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
for _env_file in "${ROOT_DIR}/shared/config/.env.listenhub" "${ROOT_DIR}/.env.listenhub" "${ROOT_DIR}/.env"; do
  if [[ -f "${_env_file}" ]]; then
    set -a
    source "${_env_file}"
    set +a
  fi
done
LOG_DIR="${ROOT_DIR}/logs"
JXA_SCRIPT="${ROOT_DIR}/xiaoyuzhou_api_session.jxa"
PYTHON_SCRIPT="${ROOT_DIR}/xiaoyuzhou_publish_pipeline.py"
AUTH_REFRESH_SCRIPT="${ROOT_DIR}/xiaoyuzhou_auth_refresh.py"
PODCAST_PID="${XIAOYUZHOU_PODCAST_PID:-}"
PODCAST_PAGE_URL="https://podcaster.xiaoyuzhoufm.com/podcast/${PODCAST_PID}/library"
AUTH_CACHE_FILE="${ROOT_DIR}/.xiaoyuzhou_auth.json"
DRY_RUN="${DRY_RUN:-false}"

mkdir -p "${LOG_DIR}"

notify() {
  local title="$1"
  local body="$2"
  python3 "${ROOT_DIR}/mobile_notify.py" "${title}" "${body}" >/dev/null 2>&1 || \
    osascript -e "display notification \"${body//\"/\\\"}\" with title \"${title//\"/\\\"}\"" >/dev/null 2>&1 || true
}

usage() {
  cat <<'EOF'
用法:
  ./xiaoyuzhou_prepare_episode.sh <episode_dir> [python_args...]

说明:
  从当前 Chrome 登录态读取小宇宙 token，
  然后调用 Python 脚本完成：
  1. 音频上传 / 复用资源库资源
  2. 封面上传
  3. 创建或更新单集

环境变量:
  DRY_RUN=true    只输出计划动作，不做上传和发布

示例:
  ./xiaoyuzhou_prepare_episode.sh ./episodes/20260326E1
  DRY_RUN=true ./xiaoyuzhou_prepare_episode.sh ./episodes/20260326E1
  ./xiaoyuzhou_prepare_episode.sh ./episodes/20260326E1 --publish-at 2026-03-27T09:30:00+08:00
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 1
fi

episode_dir="$1"
shift

if [[ ! -d "${episode_dir}" ]]; then
  echo "目录不存在: ${episode_dir}" >&2
  exit 1
fi

extra_args=("$@")
if [[ "${DRY_RUN}" == "true" ]]; then
  extra_args=("--dry-run" "${extra_args[@]}")
  python3 "${PYTHON_SCRIPT}" "${episode_dir}" "${extra_args[@]}"
  exit 0
fi

auth_error_log="${LOG_DIR}/xiaoyuzhou-auth-$(basename "${episode_dir}")-$(date +%Y%m%dT%H%M%S)-$$.err.log"
auth_json="$(python3 "${AUTH_REFRESH_SCRIPT}" get --min-ttl-seconds 3600 --notify-on-failure 2>"${auth_error_log}")" || auth_json=""

if [[ -z "${auth_json}" ]]; then
  echo "登录态诊断日志：${auth_error_log}" >&2
  echo "未拿到可用的小宇宙登录态，请先在 Chrome 打开并登录小宇宙 Podcaster" >&2
  notify "小宇宙发布链路异常" "$(basename "${episode_dir}") 步骤：获取登录态。未拿到可用 token，请检查 Chrome 登录态或刷新日志。"
  exit 1
fi

if ! XIAOYUZHOU_AUTH_JSON="${auth_json}" python3 "${PYTHON_SCRIPT}" "${episode_dir}" "${extra_args[@]}"; then
  notify "小宇宙发布失败" "$(basename "${episode_dir}") 步骤：执行发布脚本。详细失败步骤已由 Python 发布流程单独通知。"
  exit 1
fi
