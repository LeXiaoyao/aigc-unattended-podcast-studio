#!/usr/bin/env python3
"""用现有的 voice chunks 重新混音一版，用于对比新旧 BGM 效果。

Usage:
    python remix_with_new_bgm.py                              # 默认混今天的 20260414E1
    python remix_with_new_bgm.py 20260414E1                   # 指定期目录名
    python remix_with_new_bgm.py 20260414E1 --chunks fast     # 用 tts_chunks_fast
    python remix_with_new_bgm.py 20260414E1 --bgm /path.mp3   # 临时指定其他 BGM 对比
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from audio_mixer import mix_podcast_from_chunks

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode", nargs="?", default="20260414E1", help="期目录名，如 20260414E1")
    ap.add_argument("--chunks", choices=["normal", "fast"], default="normal",
                    help="使用 tts_chunks 或 tts_chunks_fast")
    ap.add_argument("--bgm", default=None, help="临时指定 BGM 文件路径（覆盖默认）")
    ap.add_argument("--suffix", default="v6_bgm_new", help="输出文件后缀")
    args = ap.parse_args()

    episode_dir = ROOT_DIR / args.episode
    chunks_dir = episode_dir / ("tts_chunks_fast" if args.chunks == "fast" else "tts_chunks")

    if not chunks_dir.is_dir():
        raise SystemExit(f"切片目录不存在: {chunks_dir}")

    chunk_paths = sorted(chunks_dir.glob("chunk_*.wav"))
    if not chunk_paths:
        raise SystemExit(f"未找到 chunk_*.wav 切片: {chunks_dir}")

    # 标签空字符串即可——只对比 BGM，不关心时间戳
    chunk_labels = [""] * len(chunk_paths)

    output_mp3 = episode_dir / f"podcast_solo_{args.suffix}.mp3"

    print(f"[remix] episode={args.episode} chunks={chunks_dir.name} count={len(chunk_paths)}")
    print(f"[remix] BGM 来源: {'CLI 覆盖' if args.bgm else '默认（audio_mixer.BGM_FIXED）'}")

    mix_podcast_from_chunks(
        chunk_paths,
        chunk_labels,
        output_mp3,
        bgm_file=args.bgm,
    )
    print(f"[remix] 完成 → {output_mp3}")


if __name__ == "__main__":
    main()
