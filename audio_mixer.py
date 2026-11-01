#!/usr/bin/env python3
"""Audio mixer — adds intro jingle, background music, and outro to podcast audio.

Broadcast-grade morning briefing audio style:
  [intro jingle 3-5s] → [fade to BGM -22dB under voice] → [voice+BGM body] → [outro jingle fade-in 3-5s]

Usage:
    from audio_mixer import mix_podcast_audio
    mix_podcast_audio(voice_mp3, output_mp3)
"""
from __future__ import annotations

import os
import random
import subprocess
import tempfile
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent)
ASSETS_DIR = ROOT_DIR / "audio_assets"

# Ensure Homebrew tools (ffmpeg/ffprobe) are on PATH for launchd environments
_HOMEBREW_BIN = "/opt/homebrew/bin"
if _HOMEBREW_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _HOMEBREW_BIN + ":" + os.environ.get("PATH", "/usr/bin:/bin")

# ── Configurable paths via env vars ──────────────────────────────
# Intro jingle: played at full volume, then fades out as voice starts
INTRO_JINGLE = os.environ.get(
    "PODCAST_INTRO_JINGLE",
    str(ASSETS_DIR / "intro" / "nastelbom-ambient-background-495888.mp3"),
)
# Outro jingle: fades in over the last few seconds
OUTRO_JINGLE = os.environ.get("PODCAST_OUTRO_JINGLE", str(ASSETS_DIR / "outro_jingle.mp3"))
# BGM pool directory: one file randomly selected per episode (legacy, kept for fallback)
BGM_DIR = os.environ.get("PODCAST_BGM_DIR", str(ASSETS_DIR / "bgm"))
# Fixed BGM file: if set and exists, always used instead of random pool pick
BGM_FIXED = os.environ.get(
    "PODCAST_BGM_FIXED",
    str(ASSETS_DIR / "bgm" / "nastelbom-ambient-background-495888.mp3"),
)
# Transition SFX between news segments
SFX_TRANSITION = os.environ.get(
    "PODCAST_SFX_TRANSITION",
    str(ASSETS_DIR / "sfx" / "universfield-interface-03-277552.mp3"),
)

# ── Audio parameters ─────────────────────────────────────────────
INTRO_JINGLE_DURATION = 4.0     # seconds of intro jingle to use
OUTRO_JINGLE_DURATION = 4.0     # seconds of outro jingle to use
INTRO_FADE_OUT = 2.0            # intro jingle fade-out duration
OUTRO_FADE_IN = 2.0             # outro jingle fade-in duration
BGM_VOLUME_DB = -22             # BGM volume under voice (dB) — legacy ffmpeg path
BGM_FADE_IN = 1.5               # BGM fade-in at start
BGM_FADE_OUT = 2.0              # BGM fade-out before outro
VOICE_DELAY = 3.0               # seconds after intro jingle starts before voice begins

# ── Dynamic ducking parameters (pydub path) ─────────────────────
# 注：按运营反馈（Ann 2026-04），资讯播客不需要传统电台"静默抬BGM"的做法，
# BGM 全程保持恒定低位即可，人声永远是主角。两个值设为相同即可取消动态变化。
BGM_DUCKED_DB = -22             # BGM volume when voice is playing
BGM_NORMAL_DB = -22             # BGM volume during silence gaps (与 ducked 一致 → 无起伏)
BGM_FADE_IN_MS = 3000           # BGM fade-in at podcast start
BGM_FADE_OUT_MS = 3000          # BGM fade-out at podcast end
DUCK_TRANSITION_MS = 300        # crossfade between ducked/normal BGM levels
INTER_CHUNK_SILENCE_MS = 2000   # silence gap between news chunks
SFX_VOLUME_DB = -12             # transition SFX volume relative to original


def _get_duration(mp3_path: str | Path) -> float:
    """Get audio duration in seconds via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(mp3_path)],
        capture_output=True, text=True,
    )
    return float(result.stdout.strip() or "0")


def _pick_bgm() -> str | None:
    """Return the fixed BGM if available, else fall back to a random pool pick."""
    if BGM_FIXED and Path(BGM_FIXED).exists():
        return BGM_FIXED
    bgm_dir = Path(BGM_DIR)
    if not bgm_dir.is_dir():
        return None
    candidates = [f for f in bgm_dir.iterdir() if f.suffix.lower() in (".mp3", ".wav", ".m4a")]
    return str(random.choice(candidates)) if candidates else None


def _file_exists(path: str | None) -> bool:
    return bool(path) and Path(path).exists()


def voice_start_delay_ms(intro_jingle: str | None = None) -> int:
    intro = intro_jingle or INTRO_JINGLE
    return int(VOICE_DELAY * 1000) if _file_exists(intro) else 0


def normalize_podcast_loudness(
    input_mp3: Path,
    output_mp3: Path,
    *,
    target_i: float = -14.5,
    true_peak: float = -1.5,
    lra: float = 10.0,
) -> Path:
    """Normalize an existing podcast file to a slightly louder, steadier level."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_mp3),
        "-af", f"loudnorm=I={target_i}:TP={true_peak}:LRA={lra}",
        "-codec:a", "libmp3lame", "-b:a", "192k",
        "-ar", "44100", "-ac", "2",
        str(output_mp3),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[audio_mixer] 响度归一化失败: {result.stderr[-500:]}")
        if input_mp3 != output_mp3:
            import shutil
            shutil.copy2(str(input_mp3), str(output_mp3))
        return output_mp3
    print(f"[audio_mixer] 响度归一化完成: {output_mp3} ({_get_duration(output_mp3):.1f}s)")
    return output_mp3


def mix_podcast_audio(
    voice_mp3: Path,
    output_mp3: Path,
    *,
    intro_jingle: str | None = None,
    outro_jingle: str | None = None,
    bgm_file: str | None = None,
) -> Path:
    """Mix voice audio with intro jingle, BGM, and outro jingle.

    If no music assets are available, copies voice_mp3 to output_mp3 unchanged.

    Returns the output path.
    """
    intro = intro_jingle or INTRO_JINGLE
    outro = outro_jingle or OUTRO_JINGLE
    bgm = bgm_file or _pick_bgm()

    has_intro = _file_exists(intro)
    has_outro = _file_exists(outro)
    has_bgm = _file_exists(bgm)

    if not has_intro and not has_outro and not has_bgm:
        # No assets — just copy
        import shutil
        shutil.copy2(str(voice_mp3), str(output_mp3))
        print("[audio_mixer] 无音乐素材，使用原始语音")
        return output_mp3

    voice_duration = _get_duration(voice_mp3)
    total_duration = VOICE_DELAY + voice_duration + OUTRO_JINGLE_DURATION

    # Build ffmpeg filter_complex
    inputs = ["-i", str(voice_mp3)]  # input 0: voice
    input_idx = 1
    filter_parts = []

    # Voice: delay by VOICE_DELAY seconds, normalize
    filter_parts.append(
        f"[0:a]adelay={int(VOICE_DELAY * 1000)}|{int(VOICE_DELAY * 1000)},"
        f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[voice]"
    )

    mix_inputs = ["[voice]"]

    # Intro jingle
    if has_intro:
        inputs.extend(["-i", str(intro)])
        filter_parts.append(
            f"[{input_idx}:a]atrim=0:{INTRO_JINGLE_DURATION},"
            f"afade=t=out:st={INTRO_JINGLE_DURATION - INTRO_FADE_OUT}:d={INTRO_FADE_OUT},"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[intro]"
        )
        mix_inputs.append("[intro]")
        input_idx += 1

    # BGM: loop to fill entire duration, set low volume, fade in/out
    if has_bgm:
        bgm_loop_duration = total_duration + 5  # a bit extra to be safe
        inputs.extend(["-stream_loop", "-1", "-i", str(bgm)])
        filter_parts.append(
            f"[{input_idx}:a]atrim=0:{bgm_loop_duration},"
            f"volume={BGM_VOLUME_DB}dB,"
            f"afade=t=in:st=0:d={BGM_FADE_IN},"
            f"afade=t=out:st={total_duration - BGM_FADE_OUT}:d={BGM_FADE_OUT},"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[bgm]"
        )
        mix_inputs.append("[bgm]")
        input_idx += 1

    # Outro jingle
    if has_outro:
        outro_start_ms = int((VOICE_DELAY + voice_duration - 0.5) * 1000)
        inputs.extend(["-i", str(outro)])
        filter_parts.append(
            f"[{input_idx}:a]atrim=0:{OUTRO_JINGLE_DURATION},"
            f"afade=t=in:st=0:d={OUTRO_FADE_IN},"
            f"adelay={outro_start_ms}|{outro_start_ms},"
            f"aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[outro]"
        )
        mix_inputs.append("[outro]")
        input_idx += 1

    # Mix all streams
    n_inputs = len(mix_inputs)
    mix_label = "".join(mix_inputs)
    filter_parts.append(
        f"{mix_label}amix=inputs={n_inputs}:duration=longest:dropout_transition=2,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11[out]"
    )

    filter_complex = ";\n".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-codec:a", "libmp3lame", "-b:a", "192k",
        "-ar", "44100", "-ac", "2",
        str(output_mp3),
    ]

    print(f"[audio_mixer] 混音: intro={has_intro}, bgm={has_bgm}, outro={has_outro}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[audio_mixer] ffmpeg 混音失败: {result.stderr[-500:]}")
        # Fallback: copy raw voice
        import shutil
        shutil.copy2(str(voice_mp3), str(output_mp3))
        return output_mp3

    print(f"[audio_mixer] 混音完成: {output_mp3} ({_get_duration(output_mp3):.1f}s)")
    return output_mp3


def _load_audio(path: str, target_sr: int = 44100, channels: int = 2) -> "AudioSegment":
    """加载音频文件并统一采样率和声道数。"""
    from pydub import AudioSegment
    seg = AudioSegment.from_file(path)
    if seg.frame_rate != target_sr:
        seg = seg.set_frame_rate(target_sr)
    if seg.channels != channels:
        seg = seg.set_channels(channels)
    return seg


def _loop_to_length(seg: "AudioSegment", target_ms: int) -> "AudioSegment":
    """将音频段循环延伸到至少 target_ms 毫秒。

    自动裁剪尾部 fade-out（低于 -30dB 的段落），在 loop 接缝做交叉淡化，
    避免原文件末尾静音在循环拼接时产生断点。
    """
    TRIM_THRESH_DB = -30    # 尾部低于此值即视为衰减区
    SCAN_STEP_MS = 300      # 扫描步长
    CROSSFADE_MS = 800      # loop 接缝交叉淡化时长

    # 从末尾向前扫描，找到最后一个音量正常的位置，截断 fade-out 尾部
    trim_to = len(seg)
    cursor = len(seg)
    while cursor >= SCAN_STEP_MS:
        cursor -= SCAN_STEP_MS
        if seg[cursor: cursor + SCAN_STEP_MS].dBFS > TRIM_THRESH_DB:
            trim_to = cursor + SCAN_STEP_MS
            break
    if trim_to < len(seg) - SCAN_STEP_MS:
        seg = seg[:trim_to]

    if not len(seg):
        from pydub import AudioSegment as _AS
        return _AS.empty()

    if len(seg) >= target_ms:
        return seg[:target_ms]

    # 带交叉淡化的循环拼接，避免接缝处出现明显电平跳变
    cf = min(CROSSFADE_MS, len(seg) // 4)
    looped = seg
    step = max(len(seg) - cf, 1)
    repeats = (target_ms - len(seg)) // step + 2
    for _ in range(repeats):
        looped = looped.append(seg, crossfade=cf)
        if len(looped) >= target_ms:
            break
    return looped[:target_ms]


def _build_ducked_bgm(
    bgm_raw: "AudioSegment",
    total_ms: int,
    voice_regions: list[tuple[int, int]],
    *,
    start_offset_ms: int = 0,
    prepend_silence_ms: int = 0,
) -> "AudioSegment":
    """构建动态闪避 BGM 轨道。

    voice_regions: [(start_ms, end_ms), ...] 人声区间列表
    人声区间内: BGM_DUCKED_DB；静音间隙: BGM_NORMAL_DB
    开头 BGM_FADE_IN_MS 淡入，结尾 BGM_FADE_OUT_MS 淡出
    """
    from pydub import AudioSegment

    if start_offset_ms and len(bgm_raw) > 0:
        offset = start_offset_ms % len(bgm_raw)
        bgm_raw = bgm_raw[offset:] + bgm_raw[:offset]

    bgm_target_ms = max(total_ms - prepend_silence_ms, 0)
    bgm = _loop_to_length(bgm_raw, bgm_target_ms) if bgm_target_ms else AudioSegment.empty()
    if prepend_silence_ms > 0:
        bgm = AudioSegment.silent(duration=prepend_silence_ms, frame_rate=44100) + bgm
    bgm = bgm[:total_ms]

    # 先把整条 BGM 设为「静音间隙」音量（较高）
    bgm_normal = bgm + BGM_NORMAL_DB
    # 再做一条全程「人声压低」音量的版本
    bgm_ducked = bgm + BGM_DUCKED_DB

    # 用人声区间拼合：人声段取 ducked，间隙段取 normal
    # 排序并合并重叠区间
    merged: list[tuple[int, int]] = []
    for start, end in sorted(voice_regions):
        if merged and start <= merged[-1][1] + DUCK_TRANSITION_MS:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # 仅当两个电平真正不同时才做 duck 过渡；若相同则省略 fade，保持全程恒定音量
    _do_duck_transitions = abs(BGM_NORMAL_DB - BGM_DUCKED_DB) > 1.0

    # 逐段拼接
    result = AudioSegment.empty()
    cursor = 0
    for v_start, v_end in merged:
        # 间隙段（cursor → v_start）: normal volume
        if v_start > cursor:
            gap = bgm_normal[cursor:v_start]
            if _do_duck_transitions and len(gap) > DUCK_TRANSITION_MS:
                gap = gap.fade_out(DUCK_TRANSITION_MS)
            result += gap
            cursor = v_start
        # 人声段（v_start → v_end）: ducked volume
        ducked_slice = bgm_ducked[v_start:v_end]
        if _do_duck_transitions and len(ducked_slice) > DUCK_TRANSITION_MS:
            ducked_slice = ducked_slice.fade_in(DUCK_TRANSITION_MS)
        result += ducked_slice
        cursor = v_end

    # 最后的尾巴（如果人声结束后还有 BGM 时间）
    if cursor < total_ms:
        tail = bgm_normal[cursor:total_ms]
        if _do_duck_transitions and len(tail) > DUCK_TRANSITION_MS:
            tail = tail.fade_in(DUCK_TRANSITION_MS)
        result += tail

    # 全局淡入淡出
    if len(result) > BGM_FADE_IN_MS:
        result = result.fade_in(BGM_FADE_IN_MS)
    if len(result) > BGM_FADE_OUT_MS:
        result = result.fade_out(BGM_FADE_OUT_MS)

    return result


def mix_podcast_from_chunks(
    chunk_wav_paths: list[Path],
    chunk_labels: list[str],
    output_mp3: Path,
    *,
    intro_jingle: str | None = None,
    outro_jingle: str | None = None,
    bgm_file: str | None = None,
    sfx_file: str | None = None,
    inter_chunk_silence_ms: int | None = None,
    sfx_at: set[int] | None = None,
) -> tuple[Path, list[dict[str, str]]]:
    """专业播客级切片混音：SFX 转场 + BGM 动态闪避 + 精准时间戳。

    流程:
      1. 片头 Jingle（全音量）→ 语音延迟
      2. 逐条拼接人声切片，切片之间插入 2s 静音 + 叠加转场 SFX
      3. BGM 动态闪避：人声段 -18dB，静音间隙 -5dB，头尾各 3s fade
      4. 片尾 Jingle 叠加在语音结束前
      5. loudnorm 响度归一化导出 192kbps MP3

    返回 (输出路径, 时间戳列表)。
    """
    from pydub import AudioSegment

    if inter_chunk_silence_ms is None:
        inter_chunk_silence_ms = INTER_CHUNK_SILENCE_MS

    intro = intro_jingle or INTRO_JINGLE
    outro = outro_jingle or OUTRO_JINGLE
    bgm = bgm_file or _pick_bgm()
    sfx = sfx_file or SFX_TRANSITION

    has_intro = _file_exists(intro)
    has_outro = _file_exists(outro)
    has_bgm = _file_exists(bgm)
    has_sfx = _file_exists(sfx)

    print(f"[audio_mixer] 切片混音: intro={has_intro}, bgm={has_bgm}, "
          f"outro={has_outro}, sfx={has_sfx}, chunks={len(chunk_wav_paths)}")

    # ── 0. 预加载素材 ──
    sfx_seg = None
    if has_sfx:
        sfx_seg = _load_audio(sfx)
        sfx_seg = sfx_seg + SFX_VOLUME_DB
        # 转场 SFX 不要超过静音时长
        if len(sfx_seg) > inter_chunk_silence_ms:
            sfx_seg = sfx_seg[:inter_chunk_silence_ms]

    intro_seg = None
    if has_intro:
        intro_seg = _load_audio(intro)
        intro_seg = intro_seg[:int(INTRO_JINGLE_DURATION * 1000)]
        intro_seg = intro_seg.fade_out(int(INTRO_FADE_OUT * 1000))

    outro_seg = None
    if has_outro:
        outro_seg = _load_audio(outro)
        outro_seg = outro_seg[:int(OUTRO_JINGLE_DURATION * 1000)]
        outro_seg = outro_seg.fade_in(int(OUTRO_FADE_IN * 1000))

    # ── 1. 加载语音切片 ──
    voice_segments: list[AudioSegment] = []
    for wp in chunk_wav_paths:
        seg = AudioSegment.from_file(str(wp))
        seg = seg.set_frame_rate(44100).set_channels(2)
        voice_segments.append(seg)

    silence = AudioSegment.silent(duration=inter_chunk_silence_ms, frame_rate=44100)
    intro_delay_ms = int(VOICE_DELAY * 1000) if has_intro else 0

    # ── 2. 拼接语音轨道 + 时间戳打点 + 记录人声区间 ──
    current_ms = intro_delay_ms
    timeline: list[dict[str, str]] = []
    voice_regions: list[tuple[int, int]] = []   # (start, end) 人声区间

    # 前置静音（为片头 Jingle 留空间）
    combined = AudioSegment.silent(duration=intro_delay_ms, frame_rate=44100)

    for i, (seg, label) in enumerate(zip(voice_segments, chunk_labels)):
        # 打时间戳
        if label.strip():
            timeline.append({"time": _ms_to_mmss(current_ms), "label": label.strip()})

        # 记录人声区间
        voice_start = current_ms
        combined += seg
        current_ms += len(seg)
        voice_regions.append((voice_start, current_ms))

        # 段落间隙（最后一段不加）
        if i < len(voice_segments) - 1:
            # 插入静音
            gap = silence
            # 在静音中间叠加转场 SFX（sfx_at=None 表示全部边界；否则只在指定边界插）
            use_sfx = sfx_at is None or i in sfx_at
            if sfx_seg is not None and use_sfx:
                sfx_offset = max(0, (inter_chunk_silence_ms - len(sfx_seg)) // 2)
                gap = gap.overlay(sfx_seg, position=sfx_offset)
            combined += gap
            current_ms += inter_chunk_silence_ms

    total_ms = len(combined)

    # ── 3. 叠加片头 Jingle ──
    if intro_seg is not None:
        combined = combined.overlay(intro_seg, position=0)

    # ── 4. 叠加片尾 Jingle ──
    if outro_seg is not None:
        # 片尾从人声结束前 0.5s 开始淡入
        outro_position = max(0, total_ms - len(outro_seg) - 500)
        # 确保不超出总长，必要时延长
        needed_ms = outro_position + len(outro_seg)
        if needed_ms > len(combined):
            combined += AudioSegment.silent(duration=needed_ms - len(combined), frame_rate=44100)
            total_ms = len(combined)
        combined = combined.overlay(outro_seg, position=outro_position)

    # ── 5. BGM 动态闪避叠加 ──
    if has_bgm:
        bgm_raw = _load_audio(bgm)
        bgm_start_offset_ms = 0
        bgm_prepend_silence_ms = 0
        if has_intro and Path(intro).resolve() == Path(bgm).resolve():
            # 同一条音频既做片头又做底垫时，让底垫从片头裁掉的 4s 之后继续，
            # 避免开头双层叠加，也避免听感上像重新起一遍 BGM。
            bgm_start_offset_ms = int(INTRO_JINGLE_DURATION * 1000)
            bgm_prepend_silence_ms = bgm_start_offset_ms
        ducked_bgm = _build_ducked_bgm(
            bgm_raw,
            total_ms,
            voice_regions,
            start_offset_ms=bgm_start_offset_ms,
            prepend_silence_ms=bgm_prepend_silence_ms,
        )
        # 确保 BGM 和主轨道同长
        if len(ducked_bgm) < len(combined):
            ducked_bgm += AudioSegment.silent(
                duration=len(combined) - len(ducked_bgm), frame_rate=44100,
            )
        elif len(ducked_bgm) > len(combined):
            ducked_bgm = ducked_bgm[:len(combined)]
        combined = combined.overlay(ducked_bgm)

    # ── 6. 导出中间 WAV → ffmpeg loudnorm → 最终 MP3 ──
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_wav = Path(tmp.name)
    combined.export(str(tmp_wav), format="wav")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(tmp_wav),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-codec:a", "libmp3lame", "-b:a", "192k",
        "-ar", "44100", "-ac", "2",
        str(output_mp3),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    tmp_wav.unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"[audio_mixer] loudnorm 导出失败，回退无归一化: {result.stderr[-300:]}")
        combined.export(str(output_mp3), format="mp3", bitrate="192k",
                        parameters=["-ar", "44100", "-ac", "2"])

    duration_s = _get_duration(output_mp3)
    print(f"[audio_mixer] 切片混音完成: {duration_s:.1f}s, {len(timeline)} 个时间戳, "
          f"ducking={has_bgm}, sfx={has_sfx}")
    return output_mp3, timeline


def _ms_to_mmss(ms: int) -> str:
    """毫秒转 MM:SS 格式。"""
    total_seconds = max(int(ms / 1000), 0)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def generate_highlight_clip(
    mixed_mp3: Path,
    output_mp3: Path,
    *,
    clip_duration: float = 15.0,
    start_offset: float | None = None,
) -> Path:
    """Generate a 15-second highlight clip for Douyin/Xiaohongshu.

    By default, clips from right after the intro (the "quick headlines" section).
    Adds fade-in/out and slightly boosts volume.
    """
    total = _get_duration(mixed_mp3)
    if start_offset is None:
        # Start from the quick headlines section (right after intro jingle)
        start_offset = VOICE_DELAY + 0.5

    # Ensure we don't go past the end
    if start_offset + clip_duration > total:
        start_offset = max(0, total - clip_duration)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(mixed_mp3),
        "-ss", str(start_offset),
        "-t", str(clip_duration),
        "-af", (
            "afade=t=in:st=0:d=0.5,"
            "afade=t=out:st=14:d=1,"
            "volume=1.3"
        ),
        "-codec:a", "libmp3lame", "-b:a", "192k",
        str(output_mp3),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[audio_mixer] 精华片段生成失败: {result.stderr[-300:]}")
        return output_mp3

    print(f"[audio_mixer] 15s精华片段: {output_mp3} ({_get_duration(output_mp3):.1f}s)")
    return output_mp3


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="播客音频混音器")
    sub = parser.add_subparsers(dest="cmd")

    mix_p = sub.add_parser("mix", help="混音：语音+片头+BGM+片尾")
    mix_p.add_argument("voice_mp3", help="纯语音 MP3 路径")
    mix_p.add_argument("output_mp3", help="输出混音 MP3 路径")
    mix_p.add_argument("--bgm", default=None, help="指定 BGM 文件")

    clip_p = sub.add_parser("clip", help="生成15秒精华片段")
    clip_p.add_argument("input_mp3", help="混音后的 MP3")
    clip_p.add_argument("output_mp3", help="输出片段 MP3")
    clip_p.add_argument("--start", type=float, default=None, help="起始秒数")

    norm_p = sub.add_parser("normalize", help="对现有播客音频做响度归一化")
    norm_p.add_argument("input_mp3", help="输入 MP3")
    norm_p.add_argument("output_mp3", help="输出 MP3")

    args = parser.parse_args()
    if args.cmd == "mix":
        mix_podcast_audio(Path(args.voice_mp3), Path(args.output_mp3), bgm_file=args.bgm)
    elif args.cmd == "clip":
        generate_highlight_clip(Path(args.input_mp3), Path(args.output_mp3), start_offset=args.start)
    elif args.cmd == "normalize":
        normalize_podcast_loudness(Path(args.input_mp3), Path(args.output_mp3))
    else:
        parser.print_help()
