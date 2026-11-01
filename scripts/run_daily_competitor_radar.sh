#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
EPISODES_ROOT="$PROJECT_ROOT/episodes"
LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/competitor_radar_daily.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_ROOT"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] competitor radar start"

  latest_episode="$(
    find "$EPISODES_ROOT" -maxdepth 1 -type d -name '20*E*' \
      | sed 's#^.*/##' \
      | grep -E '^[0-9]{8}E[0-9]+$' \
      | sort \
      | tail -n 1
  )"

  if [[ -z "${latest_episode:-}" ]]; then
    echo "No episode directory found under $EPISODES_ROOT"
    exit 2
  fi

  episode_dir="$EPISODES_ROOT/$latest_episode"
  if [[ ! -f "$episode_dir/search_results.json" ]]; then
    echo "Skip $latest_episode: missing search_results.json"
    exit 2
  fi
  if [[ ! -f "$episode_dir/podcast_script.txt" ]]; then
    echo "Skip $latest_episode: missing podcast_script.txt"
    exit 2
  fi

  python3 tools/competitor_radar/competitor_radar.py --episode-id "$latest_episode" --write-report
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] competitor radar done: $latest_episode"
} >> "$LOG_FILE" 2>&1

