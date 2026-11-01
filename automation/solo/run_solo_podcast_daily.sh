#!/bin/zsh

set -euo pipefail

ROOT_DIR="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
exec "${ROOT_DIR}/run_solo_podcast_daily.sh" "$@"
