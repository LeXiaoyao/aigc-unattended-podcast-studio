#!/bin/zsh

set -euo pipefail

DEST_ROOT="${PODCAST_ROOT_DIR:-$(cd "$(dirname "$0")" && pwd)}"
SOURCE_DIR="${HOME}/Downloads"
MODE="move"
DRY_RUN=0

usage() {
  cat <<'EOF'
用法:
  ./import_listenhub_videos.sh [--source 目录] [--dest 目录] [--copy] [--dry-run]

说明:
  把下载好的视频文件按日期归档到目标目录下:
  1. 优先从文件名里提取日期，例如 20260326 / 2026-03-26 / 2026_03_26
  2. 如果文件名里没有日期，则使用文件修改日期

选项:
  --source   视频来源目录，默认 ~/Downloads
  --dest     目标根目录，默认当前项目根目录
  --copy     复制文件，保留原文件
  --dry-run  只显示将要执行的操作，不真正移动/复制
  --help     显示帮助
EOF
}

extract_date() {
  local filename="$1"
  local basename="${filename:t}"

  if [[ "$basename" =~ (20[0-9]{2})[-_]?([0-9]{2})[-_]?([0-9]{2}) ]]; then
    echo "${match[1]}${match[2]}${match[3]}"
    return 0
  fi

  stat -f "%Sm" -t "%Y%m%d" "$filename"
}

is_video_file() {
  local filename="${1:l}"
  [[ "$filename" == *.mp4 || "$filename" == *.mov || "$filename" == *.m4v || "$filename" == *.webm || "$filename" == *.mkv ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE_DIR="$2"
      shift 2
      ;;
    --dest)
      DEST_ROOT="$2"
      shift 2
      ;;
    --copy)
      MODE="copy"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -d "$SOURCE_DIR" ]]; then
  echo "来源目录不存在: $SOURCE_DIR" >&2
  exit 1
fi

typeset -a files
while IFS= read -r file; do
  files+=("$file")
done < <(find "$SOURCE_DIR" -maxdepth 1 -type f)

if [[ ${#files[@]} -eq 0 ]]; then
  echo "来源目录里没有文件: $SOURCE_DIR"
  exit 0
fi

processed=0

for file in "${files[@]}"; do
  if ! is_video_file "$file"; then
    continue
  fi

  date_dir="$(extract_date "$file")"
  target_dir="${DEST_ROOT}/${date_dir}"
  target_file="${target_dir}/${file:t}"

  if [[ -e "$target_file" ]]; then
    echo "跳过已存在文件: $target_file"
    continue
  fi

  echo "归档 ${file:t} -> $target_dir"

  if [[ "$DRY_RUN" -eq 0 ]]; then
    mkdir -p "$target_dir"
    if [[ "$MODE" == "copy" ]]; then
      cp "$file" "$target_file"
    else
      mv "$file" "$target_file"
    fi
  fi

  ((processed += 1))
done

if [[ "$processed" -eq 0 ]]; then
  echo "没有找到可归档的视频文件。支持: mp4, mov, m4v, webm, mkv"
else
  echo "完成，共处理 $processed 个视频文件。"
fi
