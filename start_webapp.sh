#!/bin/zsh

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
ENV_FILE="${ROOT_DIR}/.env.listenhub"
FALLBACK_ENV_FILE="${ROOT_DIR}/.env"
SHARED_ENV_FILE="${ROOT_DIR}/shared/config/.env.listenhub"

cd "${ROOT_DIR}"

# 先加载共享配置（FEISHU_EVENT_TOKEN、发布确认白名单等），再用根目录文件覆盖。
# 飞书发布确认依赖 FEISHU_EVENT_TOKEN 进入 webapp 环境，缺了 /feishu/events 会 503。
if [[ -f "${SHARED_ENV_FILE}" ]]; then
  set -a
  source "${SHARED_ENV_FILE}"
  set +a
fi

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  source "${ENV_FILE}"
  set +a
fi

if [[ -f "${FALLBACK_ENV_FILE}" ]]; then
  set -a
  source "${FALLBACK_ENV_FILE}"
  set +a
fi

# Install dependencies if needed
pip3 install fastapi uvicorn jinja2 python-multipart aiofiles --quiet 2>/dev/null || true

# Run database migration on first launch
if [[ ! -f "${ROOT_DIR}/db/podcast.db" ]]; then
  echo "首次启动，迁移历史数据..."
  python3 "${ROOT_DIR}/db/migrate.py" || true
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# 提高文件描述符上限，防 sqlite/socket 累计耗尽（macOS 默认仅 256）
ulimit -n 4096 || true

echo "启动 AI观测站 web 服务: http://localhost:8765"
# 用 exec 让 uvicorn 替换 shell，这样 launchd 重启时能直接 SIGTERM 到本体，避免 orphan
exec /opt/homebrew/bin/uvicorn webapp.main:app --host 0.0.0.0 --port 8765
