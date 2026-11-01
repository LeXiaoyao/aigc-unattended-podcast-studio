#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
EPISODES_ROOT="$PROJECT_ROOT/episodes"
LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/news_review_daily.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_ROOT"

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] news review start"

  latest_episode=""
  while IFS= read -r episode; do
    episode_dir="$EPISODES_ROOT/$episode"
    if [[ ! -f "$episode_dir/search_results.json" ]]; then
      continue
    fi
    if [[ ! -f "$episode_dir/podcast_script.txt" ]]; then
      echo "Skip $episode: missing podcast_script.txt"
      continue
    fi
    if [[ -f "$episode_dir/flow_type.txt" ]] && grep -qi 'solo' "$episode_dir/flow_type.txt"; then
      latest_episode="$episode"
      break
    fi
    if [[ -f "$episode_dir/podcast_solo.mp3" || -f "$episode_dir/podcast_solo_voice.mp3" ]]; then
      latest_episode="$episode"
      break
    fi
    if [[ -f "$episode_dir/research.json" ]] && grep -q '"flow_type"[[:space:]]*:[[:space:]]*"solo"' "$episode_dir/research.json"; then
      latest_episode="$episode"
      break
    fi
    if [[ -z "${latest_episode:-}" ]]; then
      latest_episode="$episode"
    fi
  done < <(
    find "$EPISODES_ROOT" -maxdepth 1 -type d -name '20*E*' \
      | sed 's#^.*/##' \
      | grep -E '^[0-9]{8}E[0-9]+$' \
      | sort -r
  )

  if [[ -z "${latest_episode:-}" ]]; then
    echo "No episode directory with search_results.json and podcast_script.txt found under $EPISODES_ROOT"
    exit 2
  fi

  episode_dir="$EPISODES_ROOT/$latest_episode"
  if [[ ! -f "$episode_dir/podcast_script.txt" ]]; then
    echo "Skip $latest_episode: missing podcast_script.txt; TTS is not triggered by this review job"
    exit 2
  fi
  if [[ ! -f "$episode_dir/search_results.json" ]]; then
    echo "Skip $latest_episode: missing search_results.json"
    exit 2
  fi

  python3 tools/solo_review/review_daily_news.py --episode-id "$latest_episode" --write-report
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] news review done: $latest_episode"
} >> "$LOG_FILE" 2>&1

