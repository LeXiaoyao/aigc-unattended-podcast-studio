#!/usr/bin/env python3
"""
对比新旧 shownotes 时间戳精准度。
用现有 podcast_voice_raw.mp3 通过静音检测得到 chunk 切割点，
再用 mix_podcast_from_chunks 拿到精准 chunk_timeline，
与现有 shownotes.txt 的时间戳逐条对比。
不调用外部 API，不重新生成 TTS。
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT_DIR))

_BIN = "/opt/homebrew/bin"
if _BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _BIN + ":" + os.environ.get("PATH", "")

from audio_mixer import mix_podcast_from_chunks, INTER_CHUNK_SILENCE_MS, VOICE_DELAY

SKIP_MARKERS = (
    "嗨，早上好", "早上好，今天是", "以上就是今天", "欢迎订阅",
    "今天的播报到这里结束", "我们明天", "好，我们一条一条来看",
    "真正跑赢这一轮", "如果今天这些新闻必须浓缩成一句话",
)


def detect_silences(audio_path: Path, noise_db: float = -40.0, min_dur: float = 0.25) -> list[tuple[float, float]]:
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
    silences.sort(key=lambda x: x[1] - x[0], reverse=True)
    return silences


def get_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return float(r.stdout.strip() or "0")


def split_audio(audio_path: Path, boundaries: list[float], out_dir: Path) -> list[Path]:
    total_dur = get_duration(audio_path)
    cuts = sorted([0.0] + boundaries + [total_dur])
    wav_paths = []
    for i, (start, end) in enumerate(zip(cuts, cuts[1:])):
        out = out_dir / f"chunk_{i:03d}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_path),
             "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
             "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", str(out)],
            capture_output=True, check=True,
        )
        wav_paths.append(out)
    return wav_paths


def extract_old_timestamps(shownotes_path: Path) -> list[tuple[str, str]]:
    """从 shownotes.txt 提取 [(time, label), ...]"""
    if not shownotes_path.exists():
        return []
    result = []
    for line in shownotes_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\d{2}:\d{2})\s+(.+)", line.strip())
        if m:
            result.append((m.group(1), m.group(2)[:60]))
    return result


def mmss_to_seconds(t: str) -> int:
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def run_episode(episode_id: str) -> None:
    episode_dir = ROOT_DIR / "episodes" / episode_id
    voice_path = episode_dir / "podcast_voice_raw.mp3"
    shownotes_path = episode_dir / "shownotes.txt"

    script_path = next(
        (episode_dir / f for f in ("podcast_script_codex.txt", "podcast_script.txt")
         if (episode_dir / f).exists()),
        None,
    )
    if not voice_path.exists() or script_path is None:
        print(f"[{episode_id}] 缺少 voice_raw.mp3 或脚本，跳过")
        return

    script_text = script_path.read_text(encoding="utf-8").strip()
    all_paragraphs = [p.strip() for p in re.split(r"\n{2,}", script_text) if p.strip()]
    n_chunks = len(all_paragraphs)

    chunk_labels: list[str] = []
    for para in all_paragraphs:
        if any(m in para for m in SKIP_MARKERS) or len(re.sub(r"\s+", "", para)) < 20:
            chunk_labels.append("")
        else:
            chunk_labels.append(para[:40].replace("\n", " "))

    # 静音检测 → 切割边界
    silences = detect_silences(voice_path)
    n_cuts = n_chunks - 1
    top_silences = sorted(silences[:n_cuts], key=lambda x: x[0])
    boundaries = [(s + e) / 2 for s, e in top_silences]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wav_paths = split_audio(voice_path, boundaries, tmp_path)
        actual = min(len(wav_paths), len(chunk_labels))
        wav_paths = wav_paths[:actual]
        labels = chunk_labels[:actual]

        # 用 mix_podcast_from_chunks 计算精准时间线（输出到临时文件，不覆盖正式音频）
        tmp_out = tmp_path / "mixed_test.mp3"
        _, new_timeline = mix_podcast_from_chunks(wav_paths, labels, tmp_out)

    # 保存 tts_chunk_timeline.json（供 regen-shownotes 复用）
    chunk_timeline_path = episode_dir / "tts_chunk_timeline.json"
    chunk_timeline_path.write_text(
        json.dumps(new_timeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[{episode_id}] tts_chunk_timeline.json 已保存（{len(new_timeline)} 条）")

    # 对比
    old_ts = extract_old_timestamps(shownotes_path)
    new_ts = [(item["time"], item["label"][:60]) for item in new_timeline]

    print(f"\n{'─'*70}")
    print(f"  {episode_id}  时间戳对比  （旧=heuristic  新=SFX chunk精准）")
    print(f"{'─'*70}")
    print(f"  {'#':<3}  {'旧时间':>6}  {'新时间':>6}  {'偏差':>6}  描述")
    print(f"{'─'*70}")

    n = max(len(old_ts), len(new_ts))
    for i in range(n):
        old_t, old_label = old_ts[i] if i < len(old_ts) else ("--:--", "")
        new_t, new_label = new_ts[i] if i < len(new_ts) else ("--:--", "")
        if old_t != "--:--" and new_t != "--:--":
            diff = mmss_to_seconds(new_t) - mmss_to_seconds(old_t)
            diff_str = f"{diff:+d}s"
        else:
            diff_str = "  N/A"
        label = (new_label or old_label)[:52]
        print(f"  {i+1:<3}  {old_t:>6}  {new_t:>6}  {diff_str:>6}  {label}")

    print(f"{'─'*70}\n")


def main():
    episodes = ["20260508E1", "20260507E1"]
    print(f"Shownotes 时间戳精准度对比 — {len(episodes)} 期\n")
    for ep in episodes:
        run_episode(ep)
    print("完成。tts_chunk_timeline.json 已写入各 episode 目录，")
    print("后续点「更新Shownotes」将自动使用精准时间线。")


if __name__ == "__main__":
    main()
