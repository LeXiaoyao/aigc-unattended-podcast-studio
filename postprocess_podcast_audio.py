#!/usr/bin/env python3
"""
postprocess_podcast_audio.py

研报口播专用音频后期处理。链路：
    highpass 80Hz
    -> light compression (acompressor, ratio 2.5, threshold -22dB)
    -> de-esser (ffmpeg deesser)
    -> loudness normalize 到 -16 LUFS / true peak -1.5 dB / LRA 11
    -> 输出 mp3 (libmp3lame, 192k)

用法：
    python3 postprocess_podcast_audio.py --in voice.mp3 --out voice_post.mp3
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path

FILTER_CHAIN = (
    "highpass=f=80,"
    "acompressor=threshold=-22dB:ratio=2.5:attack=20:release=180:makeup=2dB,"
    "deesser=i=0.5:m=0.5:f=0:s=o,"
    "loudnorm=I=-16:TP=-1.5:LRA=11"
)


def run(input_path: Path, output_path: Path, ffmpeg: str = "/opt/homebrew/bin/ffmpeg") -> Path:
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y",
        "-i", str(input_path),
        "-af", FILTER_CHAIN,
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(output_path),
    ]
    print(f"[postprocess] {' '.join(shlex.quote(c) for c in cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stderr)
        raise RuntimeError(f"ffmpeg failed (exit {res.returncode})")
    print(f"[postprocess] -> {output_path}")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="src", required=True)
    parser.add_argument("--out", dest="dst", required=True)
    parser.add_argument("--ffmpeg", default="/opt/homebrew/bin/ffmpeg")
    args = parser.parse_args()
    run(Path(args.src), Path(args.dst), ffmpeg=args.ffmpeg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
