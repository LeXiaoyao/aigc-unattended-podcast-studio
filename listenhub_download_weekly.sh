#!/bin/zsh

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
DOWNLOAD_DIR="${HOME}/Downloads"
PODCAST_URL="https://listenhub.ai/zh/app/podcast"
JXA_SCRIPT="${ROOT_DIR}/listenhub_fetch_latest.jxa"
XIAOYUZHOU_SCRIPT="${ROOT_DIR}/xiaoyuzhou_prepare_episode.sh"
LOCK_DIR="${ROOT_DIR}/.listenhub-download.lock"
LOG_DIR="${ROOT_DIR}/logs"
ENV_FILE="${ROOT_DIR}/.env.listenhub"
PENDING_FILE="${ROOT_DIR}/.listenhub_pending_download.json"
LISTENHUB_API_BASE="https://api.marswave.ai/openapi/v1"
AUDIO_NORMALIZER="${ROOT_DIR}/audio_mixer.py"

mkdir -p "${LOG_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

run_jxa_json() {
  local raw
  raw="$(osascript -l JavaScript "$@" 2>&1 || true)"
  printf '%s\n' "${raw}" | python3 -c '
import sys

lines = [line.strip() for line in sys.stdin if line.strip()]
for line in reversed(lines):
    if line.startswith("{") and line.endswith("}"):
        print(line)
        sys.exit(0)
sys.exit(1)
'
}

cleanup() {
  rmdir "${LOCK_DIR}" 2>/dev/null || true
}

trap cleanup EXIT

if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
  log "已有任务正在运行，跳过本次执行。"
  exit 0
fi

json_get() {
  local key="$1"
  python3 -c '
import json
import sys

key = sys.argv[1]
data = json.load(sys.stdin)
value = data.get(key, "")
if isinstance(value, (dict, list)):
    print(json.dumps(value, ensure_ascii=False))
else:
    print(value)
' "$key"
}

sanitize_filename() {
  python3 -c '
import re
import sys

value = sys.stdin.read().strip()
value = re.sub(r"[\\/:*?\"<>|]+", " ", value)
value = re.sub(r"\s+", " ", value).strip()
print(value)
'
}

find_episode_mp3() {
  local episode_dir="$1"
  python3 -c '
from pathlib import Path
import sys

episode_dir = Path(sys.argv[1])
mp3_files = sorted(
    [path for path in episode_dir.glob("*.mp3") if path.is_file()],
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
print(mp3_files[0] if mp3_files else "")
' "${episode_dir}"
}

normalize_episode_audio() {
  local mp3_path="$1"
  local tmp_path
  if [[ -z "${mp3_path}" || ! -f "${mp3_path}" ]]; then
    return 1
  fi
  tmp_path="${mp3_path}.normalized.mp3"
  if python3 "${AUDIO_NORMALIZER}" normalize "${mp3_path}" "${tmp_path}" >/dev/null 2>&1; then
    mv "${tmp_path}" "${mp3_path}"
    log "音频已完成响度归一化: ${mp3_path}"
    return 0
  fi
  rm -f "${tmp_path}" 2>/dev/null || true
  log "音频响度归一化失败，保留原始文件: ${mp3_path}"
  return 1
}

has_local_episode_assets() {
  local episode_dir="$1"
  [[ -n "$(find_episode_mp3 "${episode_dir}")" ]] && return 0
  [[ -f "${episode_dir}/listenhub_create_result.json" ]] && return 0
  [[ -f "${episode_dir}/listenhub_episode_result.json" ]] && return 0
  [[ -f "${episode_dir}/xiaoyuzhou_publish_result.json" ]] && return 0
  return 1
}

download_cover_into_episode_dir() {
  local episode_dir="$1"
  local cover_url="$2"

  if [[ -z "${cover_url}" ]]; then
    log "未识别到封面图片地址。"
    return 0
  fi

  local tmp_cover final_cover backup_cover destination
  tmp_cover="${episode_dir}/.cover_source"
  final_cover="${episode_dir}/cover.jpeg"
  backup_cover="${episode_dir}/cover_listenhub_original.jpeg"
  destination="${final_cover}"

  if [[ -f "${final_cover}" ]]; then
    if [[ -f "${backup_cover}" ]]; then
      log "已存在本地封面与 ListenHub 原始封面，跳过封面覆盖: ${episode_dir}"
      return 0
    fi
    destination="${backup_cover}"
  fi

  if curl -L --fail --silent --show-error "${cover_url}" -o "${tmp_cover}"; then
    if sips -s format jpeg "${tmp_cover}" --out "${destination}" >/dev/null 2>&1; then
      rm -f "${tmp_cover}"
      if [[ "${destination}" == "${backup_cover}" ]]; then
        log "检测到已有本地封面，ListenHub 原始封面已另存为 ${destination}"
      else
        log "封面已保存到 ${destination}"
      fi
    else
      rm -f "${tmp_cover}"
      log "封面下载成功，但转换 jpeg 失败。"
    fi
  else
    rm -f "${tmp_cover}" 2>/dev/null || true
    log "封面下载失败: ${cover_url}"
  fi
}

publish_to_xiaoyuzhou_if_needed() {
  local episode_dir="$1"
  local result_file mp3_path

  result_file="${episode_dir}/xiaoyuzhou_publish_result.json"
  if [[ -f "${result_file}" ]]; then
    log "小宇宙结果文件已存在，跳过自动发布: ${result_file}"
    return 0
  fi

  if [[ ! -x "${XIAOYUZHOU_SCRIPT}" ]]; then
    log "未找到小宇宙发布脚本，跳过自动发布: ${XIAOYUZHOU_SCRIPT}"
    return 0
  fi

  mp3_path="$(find_episode_mp3 "${episode_dir}")"
  if [[ -z "${mp3_path}" ]]; then
    log "目录内尚无可发布音频，跳过自动发布: ${episode_dir}"
    return 0
  fi

  log "开始自动发布到小宇宙: ${episode_dir}"
  if "${XIAOYUZHOU_SCRIPT}" "${episode_dir}"; then
    log "小宇宙自动发布完成: ${episode_dir}"
    return 0
  fi

  log "小宇宙自动发布失败，等待下次下载轮询继续补发。"
  return 1
}

retry_today_publish_if_needed() {
  local episode_dir="$1"

  if [[ -f "${episode_dir}/xiaoyuzhou_publish_result.json" ]]; then
    return 0
  fi

  if [[ ! -f "${episode_dir}/episode_url.txt" ]]; then
    return 1
  fi

  if [[ -z "$(find_episode_mp3 "${episode_dir}")" ]]; then
    return 1
  fi

  log "检测到今天已有归档但尚未发布，尝试补发到小宇宙。"
  publish_to_xiaoyuzhou_if_needed "${episode_dir}" || true
  return 0
}

download_pending_episode() {
  if [[ ! -f "${PENDING_FILE}" ]]; then
    return 1
  fi

  if [[ -z "${LISTENHUB_API_KEY:-}" ]]; then
    log "检测到待下载作品，但缺少 LISTENHUB_API_KEY。"
    return 1
  fi

  local pending_json episode_id target_dir episode_url
  pending_json="$(cat "${PENDING_FILE}")"
  episode_id="$(printf '%s' "${pending_json}" | json_get episodeId)"
  target_dir="$(printf '%s' "${pending_json}" | json_get episodeDir)"
  episode_url="$(printf '%s' "${pending_json}" | json_get episodeUrl)"

  if [[ -z "${episode_id}" || -z "${target_dir}" ]]; then
    log "待下载文件缺少 episodeId 或 episodeDir，跳过。"
    return 1
  fi

  mkdir -p "${target_dir}"

  local status_json process_status title audio_url cover_url
  status_json="$(curl -L --fail --silent --show-error \
    -H "Authorization: Bearer ${LISTENHUB_API_KEY}" \
    "${LISTENHUB_API_BASE}/podcast/episodes/${episode_id}")"

  process_status="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(data.get("data", {}).get("processStatus", ""))
')"

  if [[ "${process_status}" != "success" ]]; then
    log "待下载作品尚未生成完成，当前状态: ${process_status:-unknown}"
    return 0
  fi

  title="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(data.get("data", {}).get("title", ""))
' | sanitize_filename)"
  audio_url="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(data.get("data", {}).get("audioUrl", ""))
')"
  cover_url="$(printf '%s' "${status_json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
print(data.get("data", {}).get("cover", ""))
')"

  if [[ -z "${title}" ]]; then
    title="ListenHub AI播客"
  fi

  if [[ -z "${audio_url}" ]]; then
    log "作品已完成，但未拿到 audioUrl。"
    return 0
  fi

  local final_mp3 tmp_mp3
  final_mp3="${target_dir}/${title}.mp3"
  tmp_mp3="${target_dir}/.audio_download"
  log "开始下载待归档音频。"
  curl -L --fail --silent --show-error "${audio_url}" -o "${tmp_mp3}"
  mv "${tmp_mp3}" "${final_mp3}"
  normalize_episode_audio "${final_mp3}" || true
  log "音频已归档到 ${final_mp3}"

  download_cover_into_episode_dir "${target_dir}" "${cover_url}"

  printf '%s\n' "${episode_url}" > "${target_dir}/episode_url.txt"
  printf '%s\n' "${title}" > "${target_dir}/title.txt"
  rm -f "${PENDING_FILE}"
  log "待下载作品已归档完成: ${target_dir}"
  publish_to_xiaoyuzhou_if_needed "${target_dir}" || true
  return 0
}

today_folder() {
  printf '%sE1' "$(date '+%Y%m%d')"
}

target_dir="${ROOT_DIR}/$(today_folder)"
mkdir -p "${target_dir}"

log "开始检查 ListenHub 最新 AI 播客。"

if download_pending_episode; then
  exit 0
fi

retry_today_publish_if_needed "${target_dir}" || true

latest_json="$(run_jxa_json "${JXA_SCRIPT}" latest "${PODCAST_URL}")"
if [[ "$(printf '%s' "${latest_json}" | json_get error)" != "" ]]; then
  log "获取最新作品失败: $(printf '%s' "${latest_json}" | json_get error)"
  exit 1
fi

episode_url="$(printf '%s' "${latest_json}" | json_get href)"
if [[ -z "${episode_url}" ]]; then
  log "未找到最新作品链接。"
  exit 1
fi

log "找到最新作品: ${episode_url}"

if [[ -f "${target_dir}/episode_url.txt" ]]; then
  existing_url="$(<"${target_dir}/episode_url.txt")"
  if [[ "${existing_url}" == "${episode_url}" ]]; then
    log "今天的作品已经归档过，检查是否需要自动发布。"
    publish_to_xiaoyuzhou_if_needed "${target_dir}" || true
    exit 0
  fi
fi

if has_local_episode_assets "${target_dir}"; then
  log "当天目录已经有一条归档作品，且最新作品链接不同；为避免同日混入第二条作品，跳过本次“抓最新作品”兜底下载。"
  exit 0
fi

start_epoch="$(date '+%s')"
meta_json="$(run_jxa_json "${JXA_SCRIPT}" export "${episode_url}")"
if [[ "$(printf '%s' "${meta_json}" | json_get error)" != "" ]]; then
  log "导出音频失败: $(printf '%s' "${meta_json}" | json_get error)"
  exit 1
fi

title="$(printf '%s' "${meta_json}" | json_get title | sanitize_filename)"
audio_url="$(printf '%s' "${meta_json}" | json_get audioUrl)"
cover_url="$(printf '%s' "${meta_json}" | json_get coverUrl)"

if [[ -z "${title}" ]]; then
  title="ListenHub AI播客"
fi

if [[ -z "${audio_url}" ]]; then
  log "未识别到音频下载地址。"
  exit 1
fi

final_mp3="${target_dir}/${title}.mp3"
tmp_mp3="${target_dir}/.audio_download"
log "开始直接下载音频。"
curl -L --fail --silent --show-error \
  -H 'Referer: https://listenhub.ai/' \
  -H 'Origin: https://listenhub.ai' \
  "${audio_url}" \
  -o "${tmp_mp3}"
mv "${tmp_mp3}" "${final_mp3}"
normalize_episode_audio "${final_mp3}" || true
log "音频已归档到 ${final_mp3}"

download_cover_into_episode_dir "${target_dir}" "${cover_url}"

printf '%s\n' "${episode_url}" > "${target_dir}/episode_url.txt"
printf '%s\n' "${title}" > "${target_dir}/title.txt"

log "归档完成: ${target_dir}"
publish_to_xiaoyuzhou_if_needed "${target_dir}" || true
