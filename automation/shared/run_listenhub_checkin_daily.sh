#!/bin/zsh

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
export PODCAST_ROOT_DIR="${ROOT_DIR}"
LOG_DIR="${ROOT_DIR}/logs"
OUT_LOG="${LOG_DIR}/listenhub-checkin.out.log"
ERR_LOG="${LOG_DIR}/listenhub-checkin.err.log"
SUCCESS_STAMP_FILE="${LOG_DIR}/listenhub-checkin.last-success.txt"
JXA_SCRIPT="${ROOT_DIR}/automation/shared/listenhub_daily_checkin.jxa"

mkdir -p "${LOG_DIR}"

if [[ "${XPC_SERVICE_NAME:-}" != *"listenhub-checkin"* ]]; then
  exec > >(tee -a "${OUT_LOG}") 2> >(tee -a "${ERR_LOG}" >&2)
fi

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

today() {
  # 凌晨人工验证不应跳过当天 08:05 的计划任务。
  TZ=Asia/Shanghai date -v-8H -v-5M '+%Y-%m-%d'
}

python_bin() {
  if [[ -x /usr/bin/python3 ]]; then
    printf '%s\n' /usr/bin/python3
    return
  fi
  command -v python3
}

if [[ ! -f "${JXA_SCRIPT}" ]]; then
  log "ListenHub 签到 JXA 不存在: ${JXA_SCRIPT}"
  exit 127
fi

if [[ -f "${SUCCESS_STAMP_FILE}" ]] && grep -q "^date=$(today)$" "${SUCCESS_STAMP_FILE}"; then
  log "本轮 08:05 签到周期已验证成功，跳过本次补跑。"
  exit 0
fi

log "开始执行 ListenHub 每日签到。"

output_file="$(mktemp "${TMPDIR:-/tmp}/listenhub-checkin.XXXXXX")"
trap 'rm -f "${output_file}"' EXIT

set +e
osascript -l JavaScript "${JXA_SCRIPT}" > "${output_file}" 2>&1
exit_code=$?
set -e

output="$(cat "${output_file}")"
printf '%s\n' "${output}"

PYTHON3="$(python_bin)"
set +e
parse_result="$(CHECKIN_OUTPUT="${output}" "${PYTHON3}" - "${SUCCESS_STAMP_FILE}" "$(today)" <<'PY'
import json
import os
import sys
from datetime import datetime

stamp_path = sys.argv[1]
raw = os.environ.get("CHECKIN_OUTPUT", "").strip()

try:
    data = json.loads(raw)
except json.JSONDecodeError as exc:
    decoder = json.JSONDecoder()
    data = None
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "status" in candidate:
            data = candidate
            break
    if data is None:
        print(f"parse_error={exc}", file=sys.stderr)
        sys.exit(2)

status = str(data.get("status") or "")
result = str(data.get("result") or "")
before = data.get("before_points")
after = data.get("after_points")
has_points = type(before) in (int, float) and type(after) in (int, float)
success = result == "success" and has_points and (
    status == "already_checked_in" or (status == "checked_in" and after > before)
)
run_time = data.get("run_time") or datetime.now().isoformat(timespec="seconds")
stamp_date = sys.argv[2]

if success:
    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write(f"date={stamp_date}\n")
        f.write(f"run_time={run_time}\n")
        f.write(f"before_points={before}\n")
        f.write(f"after_points={after}\n")
        f.write(f"status={status or result}\n")
        f.write(f"detail={data.get('detail') or ''}\n")
    print("marker_written=true")
    sys.exit(0)

print("marker_written=false")
sys.exit(1)
PY
)"
parse_code=$?
set -e

printf '%s\n' "${parse_result}"

if [[ ${exit_code} -eq 0 && ${parse_code} -eq 0 ]]; then
  log "ListenHub 每日签到执行完成。"
  exit 0
fi

log "ListenHub 每日签到执行失败或状态不确定。exit_code=${exit_code} parse_code=${parse_code}"
exit 1
