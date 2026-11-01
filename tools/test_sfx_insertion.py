#!/usr/bin/env python3
"""
测试 SFX 转场插入效果。
用现有 podcast_voice_raw.mp3 按脚本段落数切成 N 个 WAV，
再走 mix_podcast_from_chunks，输出 test_sfx_<episode>.mp3。
不调用 MiniMax TTS，纯本地测试混音逻辑。
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT_DIR))

# Homebrew PATH
_BIN = "/opt/homebrew/bin"
if _BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _BIN + ":" + os.environ.get("PATH", "")

from audio_mixer import mix_podcast_from_chunks

__test__ = False

SKIP_MARKERS = (
    "嗨，早上好", "早上好，今天是", "以上就是今天", "欢迎订阅",
    "今天的播报到这里结束", "我们明天", "好，我们一条一条来看",
    "真正跑赢这一轮", "如果今天这些新闻必须浓缩成一句话",
)


def detect_silences(audio_path: Path, noise_db: float = -40.0, min_dur: float = 0.3) -> list[tuple[float, float]]:
    """返回 [(start_s, end_s), ...] 静音段列表（按 duration 降序）。"""
    result = subprocess.run(
        ["ffmpeg", "-i", str(audio_path),
         "-af", f"silencedetect=n={noise_db}dB:d={min_dur}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    output = result.stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", output)]
    ends_raw = re.findall(r"silence_end:\s*([\d.]+)", output)
    ends = [float(x) for x in ends_raw]
    silences = list(zip(starts, ends[:len(starts)]))
    # 按静音时长降序
    silences.sort(key=lambda x: x[1] - x[0], reverse=True)
    return silences


def split_audio_by_boundaries(audio_path: Path, boundaries: list[float], out_dir: Path) -> list[Path]:
    """在给定时间点切割音频，返回 WAV 文件路径列表。"""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(audio_path)],
        capture_output=True, text=True,
    )
    total_dur = float(result.stdout.strip() or "0")

    cuts = sorted([0.0] + boundaries + [total_dur])
    wav_paths: list[Path] = []
    for i, (start, end) in enumerate(zip(cuts, cuts[1:])):
        out = out_dir / f"chunk_{i:03d}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_path),
             "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
             "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2",
             str(out)],
            capture_output=True, check=True,
        )
        wav_paths.append(out)
    return wav_paths


def test_episode(episode_id: str, out_dir: Path) -> Path:
    episode_dir = ROOT_DIR / "episodes" / episode_id
    voice_path = episode_dir / "podcast_voice_raw.mp3"
    script_path = next(
        (episode_dir / f for f in ("podcast_script_codex.txt", "podcast_script.txt")
         if (episode_dir / f).exists()),
        None,
    )
    if not voice_path.exists():
        raise FileNotFoundError(f"找不到语音文件: {voice_path}")
    if script_path is None:
        raise FileNotFoundError(f"找不到脚本文件: {episode_dir}")

    script_text = script_path.read_text(encoding="utf-8").strip()
    all_paragraphs = [p.strip() for p in re.split(r"\n{2,}", script_text) if p.strip()]
    n_chunks = len(all_paragraphs)

    print(f"\n{'='*60}")
    print(f"[{episode_id}] 脚本 {n_chunks} 段落，语音: {voice_path.name}")

    # 标签（开场/结尾留空，不进 timeline）
    chunk_labels: list[str] = []
    for para in all_paragraphs:
        if any(m in para for m in SKIP_MARKERS) or len(re.sub(r"\s+", "", para)) < 20:
            chunk_labels.append("")
        else:
            chunk_labels.append(para[:40].replace("\n", " "))
    news_count = sum(1 for l in chunk_labels if l)
    print(f"[{episode_id}] {news_count} 段进入时间线（其余为开场/结尾）")

    # 静音检测 → 找 n_chunks-1 个最长静音中点作为切割点
    print(f"[{episode_id}] 静音检测中...")
    silences = detect_silences(voice_path, noise_db=-40.0, min_dur=0.25)
    print(f"[{episode_id}] 检测到 {len(silences)} 个静音段（>= 0.25s）")

    n_cuts = n_chunks - 1
    # 取前 n_cuts 个（按时长降序），用中点作为切割位置
    top_silences = sorted(silences[:n_cuts], key=lambda x: x[0])  # 按时间顺序排
    boundaries = [(s + e) / 2 for s, e in top_silences]
    print(f"[{episode_id}] 切割点 ({len(boundaries)}): {[f'{b:.2f}s' for b in boundaries]}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wav_paths = split_audio_by_boundaries(voice_path, boundaries, tmp_path)
        actual_chunks = min(len(wav_paths), len(chunk_labels))
        wav_paths = wav_paths[:actual_chunks]
        labels = chunk_labels[:actual_chunks]

        print(f"[{episode_id}] 分成 {len(wav_paths)} 个 WAV chunk")

        output_mp3 = out_dir / f"test_sfx_{episode_id}.mp3"
        mixed_path, timeline = mix_podcast_from_chunks(
            wav_paths,
            labels,
            output_mp3,
        )

    print(f"[{episode_id}] ✓ 输出: {output_mp3}")
    print(f"[{episode_id}] 时间线 ({len(timeline)} 条):")
    for item in timeline:
        print(f"   {item['time']}  {item['label'][:50]}")
    return output_mp3


def main() -> None:
    episodes = ["20260508E1", "20260507E1", "20260506E1"]
    out_dir = ROOT_DIR / ".claude" / "worktrees" / "condescending-khayyam-cc29f2" / "tools"

    print(f"SFX 转场测试 — {len(episodes)} 期单口音频")
    print(f"输出目录: {out_dir}\n")

    results: list[tuple[str, str]] = []
    for ep in episodes:
        try:
            path = test_episode(ep, out_dir)
            results.append((ep, str(path)))
        except Exception as exc:
            print(f"[{ep}] ✗ 失败: {exc}", file=sys.stderr)

    print(f"\n{'='*60}")
    print("测试完成，请试听以下文件确认 SFX 转场效果：")
    for ep, path in results:
        print(f"  {ep}: {path}")


if __name__ == "__main__":
    main()
