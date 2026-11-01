#!/usr/bin/env python3
"""MiniMax T2A v2 TTS module — speech-2.8-hd, 普通话新闻女主播."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MINIMAX_API_BASE = "https://api.minimax.chat/v1/t2a_v2"
DEFAULT_MODEL = "speech-2.8-hd"
DEFAULT_VOICE_ID = "Chinese (Mandarin)_News_Anchor"
MANDARIN_SYSTEM_VOICES = [
    {"voice_id": "Chinese (Mandarin)_Reliable_Executive", "voice_name": "Reliable Executive"},
    {"voice_id": "Chinese (Mandarin)_News_Anchor", "voice_name": "News Anchor"},
    {"voice_id": "Chinese (Mandarin)_Unrestrained_Young_Man", "voice_name": "Unrestrained Young Man"},
    {"voice_id": "Chinese (Mandarin)_Mature_Woman", "voice_name": "Mature Woman"},
    {"voice_id": "Arrogant_Miss", "voice_name": "Arrogant Miss"},
    {"voice_id": "Robot_Armor", "voice_name": "Robot Armor"},
    {"voice_id": "Chinese (Mandarin)_Kind-hearted_Antie", "voice_name": "Kind-hearted Antie"},
    {"voice_id": "Chinese (Mandarin)_HK_Flight_Attendant", "voice_name": "HK Flight Attendant"},
    {"voice_id": "Chinese (Mandarin)_Humorous_Elder", "voice_name": "Humorous Elder"},
    {"voice_id": "Chinese (Mandarin)_Gentleman", "voice_name": "Gentleman"},
    {"voice_id": "Chinese (Mandarin)_Warm_Bestie", "voice_name": "Warm Bestie"},
    {"voice_id": "Chinese (Mandarin)_Stubborn_Friend", "voice_name": "Stubborn Friend"},
    {"voice_id": "Chinese (Mandarin)_Sweet_Lady", "voice_name": "Sweet Lady"},
    {"voice_id": "Chinese (Mandarin)_Southern_Young_Man", "voice_name": "Southern Young Man"},
    {"voice_id": "Chinese (Mandarin)_Wise_Women", "voice_name": "Wise Women"},
    {"voice_id": "Chinese (Mandarin)_Gentle_Youth", "voice_name": "Gentle Youth"},
    {"voice_id": "Chinese (Mandarin)_Warm_Girl", "voice_name": "Warm Girl"},
    {"voice_id": "Chinese (Mandarin)_Male_Announcer", "voice_name": "Male Announcer"},
    {"voice_id": "Chinese (Mandarin)_Kind-hearted_Elder", "voice_name": "Kind-hearted Elder"},
    {"voice_id": "Chinese (Mandarin)_Cute_Spirit", "voice_name": "Cute Spirit"},
    {"voice_id": "Chinese (Mandarin)_Radio_Host", "voice_name": "Radio Host"},
    {"voice_id": "Chinese (Mandarin)_Lyrical_Voice", "voice_name": "Lyrical Voice"},
    {"voice_id": "Chinese (Mandarin)_Straightforward_Boy", "voice_name": "Straightforward Boy"},
    {"voice_id": "Chinese (Mandarin)_Sincere_Adult", "voice_name": "Sincere Adult"},
    {"voice_id": "Chinese (Mandarin)_Gentle_Senior", "voice_name": "Gentle Senior"},
    {"voice_id": "Chinese (Mandarin)_Crisp_Girl", "voice_name": "Crisp Girl"},
    {"voice_id": "Chinese (Mandarin)_Pure-hearted_Boy", "voice_name": "Pure-hearted Boy"},
    {"voice_id": "Chinese (Mandarin)_Soft_Girl", "voice_name": "Soft Girl"},
    {"voice_id": "Chinese (Mandarin)_IntellectualGirl", "voice_name": "Intellectual Girl"},
    {"voice_id": "Chinese (Mandarin)_Warm_HeartedGirl", "voice_name": "Warm-hearted Girl"},
    {"voice_id": "Chinese (Mandarin)_Laid_BackGirl", "voice_name": "Laid-back Girl"},
    {"voice_id": "Chinese (Mandarin)_ExplorativeGirl", "voice_name": "Explorative Girl"},
    {"voice_id": "Chinese (Mandarin)_Warm-HeartedAunt", "voice_name": "Warm-hearted Aunt"},
    {"voice_id": "Chinese (Mandarin)_BashfulGirl", "voice_name": "Bashful Girl"},
]
DEFAULT_VOICE_ROTATION = [
    "Chinese (Mandarin)_News_Anchor",
    "Chinese (Mandarin)_Radio_Host",
    "Chinese (Mandarin)_IntellectualGirl",
    "Chinese (Mandarin)_Crisp_Girl",
    "Chinese (Mandarin)_Warm_Girl",
    "Chinese (Mandarin)_Male_Announcer",
    "Chinese (Mandarin)_Reliable_Executive",
]
MAX_CHARS_PER_CHUNK = 5000
PARAGRAPH_PAUSE_MARKER = "<#0.45#>"
SOFT_BREAK_PAUSE_MARKER = "<#0.18#>"
LONG_CLAUSE_PAUSE_MARKER = "<#0.12#>"


@dataclass
class MiniMaxTTSResult:
    voice_id: str
    output_mp3: str
    audio_length_ms: int | None
    subtitles: list[dict[str, object]]
    trace_ids: list[str]
    raw_subtitles: list[object]

    def to_dict(self) -> dict[str, object]:
        return {
            "voice_id": self.voice_id,
            "output_mp3": self.output_mp3,
            "audio_length_ms": self.audio_length_ms,
            "subtitle_count": len(self.subtitles),
            "subtitles": self.subtitles,
            "trace_ids": self.trace_ids,
            "raw_subtitles": self.raw_subtitles,
        }

# MiniMax TTS 的停顿控制标点：
#   官方支持 `<#x#>` 显式停顿标记（单位秒）。
#   这里把段落换行和长从句处改成显式停顿，而不是依赖破折号这类弱提示。

def _inject_breathing_pauses(text: str) -> str:
    """在口播稿中注入微停顿标记，增强 TTS 输出的自然断句感。

    策略：
    1. 段落间换行 → 显式补 `<#0.45#>` 长停顿
    2. 软换行 → 逗号 + `<#0.18#>`，避免句子被硬连
    3. 长从句逗号 → 逗号后补 `<#0.12#>`，让新闻口播更有气口
    """
    import re as _re

    text = str(text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""

    # 1) 段落换行处显式补长停顿
    text = _re.sub(r"([。！？!?])\n{2,}", rf"\1{PARAGRAPH_PAUSE_MARKER}\n\n", text)
    text = _re.sub(r"([^。！？!?\n])\n{2,}", rf"\1。{PARAGRAPH_PAUSE_MARKER}\n\n", text)

    # 2) 单个换行（同段内的软换行）→ 逗号 + 短停顿
    text = _re.sub(r"([^。！？!?，,；;：:\n])\n([^\n])", rf"\1，{SOFT_BREAK_PAUSE_MARKER}\n\2", text)

    # 3) 长从句的逗号处增强停顿
    def _enhance_comma(match: _re.Match) -> str:
        prefix = match.group(1)
        if len(re.sub(r"\s+", "", prefix)) >= 18:
            return prefix + f"，{LONG_CLAUSE_PAUSE_MARKER}"
        return match.group(0)

    text = _re.sub(r"([^。！？!?，,；;：:]{18,})，", _enhance_comma, text)

    return text


def _split_text(text: str) -> list[str]:
    """Split text at sentence boundaries if longer than MAX_CHARS_PER_CHUNK."""
    if len(text) <= MAX_CHARS_PER_CHUNK:
        return [text]
    # Split at sentence-ending punctuation
    sentences = re.split(r"(?<=[。！？\n])", text)
    chunks: list[str] = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) > MAX_CHARS_PER_CHUNK and current:
            chunks.append(current.strip())
            current = sent
        else:
            current += sent
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text]


_TTS_MAX_RETRIES = 5
_TTS_RETRY_BACKOFF = (5, 15, 30, 60, 120)  # seconds


def _is_transient_error(exc: Exception) -> bool:
    """判断是否为可重试的瞬态网络错误（SSL断连、超时、连接重置、响应被截断等）。"""
    import http.client as _httpclient

    # 响应体读到一半连接被截断：典型可重试瞬时错误
    if isinstance(exc, (_httpclient.IncompleteRead, ConnectionError)):
        return True
    msg = str(exc).lower()
    transient_keywords = (
        "ssl", "eof", "timeout", "timed out", "connection reset",
        "connection refused", "temporary failure", "urlopen error",
        "broken pipe", "network is unreachable", "incomplete read",
    )
    return any(kw in msg for kw in transient_keywords)


def _call_api_response(
    text: str,
    voice_id: str,
    api_key: str,
    speed: float,
    model: str,
    *,
    subtitle_enable: bool = False,
) -> dict[str, Any]:
    """Call MiniMax TTS API and return the decoded JSON body."""
    import time as _time

    payload_body: dict[str, Any] = {
        "model": model,
        "text": text,
        "stream": False,
        "subtitle_enable": subtitle_enable,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": speed,
            "vol": 1.0,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }
    # 锁定普通话：不传时 MiniMax 自行判语种，F67 起整期被读成粤语
    language_boost = os.environ.get("MINIMAX_LANGUAGE_BOOST", "Chinese").strip()
    if language_boost:
        payload_body["language_boost"] = language_boost
    payload = json.dumps(payload_body, ensure_ascii=False).encode("utf-8")

    last_exc: Exception | None = None
    for attempt in range(_TTS_MAX_RETRIES):
        req = urllib.request.Request(
            MINIMAX_API_BASE,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MiniMax API 错误 {exc.code}: {raw}") from exc
        except Exception as exc:
            last_exc = exc
            if attempt < _TTS_MAX_RETRIES - 1 and _is_transient_error(exc):
                wait = _TTS_RETRY_BACKOFF[min(attempt, len(_TTS_RETRY_BACKOFF) - 1)]
                print(f"[minimax-tts] 网络错误，{wait}s 后重试 ({attempt+1}/{_TTS_MAX_RETRIES}): {exc}")
                _time.sleep(wait)
                continue
            raise RuntimeError(f"MiniMax API 网络错误: {exc}") from exc
    else:
        raise RuntimeError(f"MiniMax API 重试 {_TTS_MAX_RETRIES} 次后仍失败: {last_exc}") from last_exc

    base_resp = body.get("base_resp", {})
    if base_resp.get("status_code", 0) != 0:
        raise RuntimeError(f"MiniMax API 失败: {base_resp.get('status_msg', body)}")

    return body


def _extract_audio_bytes(body: dict[str, Any]) -> bytes:
    audio_data = body.get("data", {}).get("audio", "")
    if not audio_data:
        raise RuntimeError(f"MiniMax API 返回空音频: {body}")

    # Try hex decode first, fall back to base64
    try:
        return bytes.fromhex(audio_data)
    except ValueError:
        import base64
        return base64.b64decode(audio_data)


def _call_api(text: str, voice_id: str, api_key: str, speed: float, model: str) -> bytes:
    body = _call_api_response(text, voice_id, api_key, speed, model, subtitle_enable=False)
    return _extract_audio_bytes(body)


def _parse_timecode_to_ms(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+)(?:[.,](\d{1,3}))?", text)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    milliseconds = int((match.group(4) or "0").ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _coerce_timestamp(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, round(value * 1000))

    text = str(value).strip()
    if not text:
        return None
    timecode = _parse_timecode_to_ms(text)
    if timecode is not None:
        return timecode
    if re.fullmatch(r"\d+\.\d+", text):
        return max(0, round(float(text) * 1000))
    if re.fullmatch(r"\d+", text):
        return max(0, int(text))
    return None


def _clean_subtitle_text(text: Any) -> str:
    cleaned = str(text or "").replace("\r", "\n")
    cleaned = re.sub(r"<#\d+(?:\.\d+)?#>", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _lookup_case_insensitive(mapping: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, Any]:
    lowered = {str(key).lower(): key for key in mapping.keys()}
    for candidate in keys:
        original = lowered.get(candidate.lower())
        if original is not None:
            return str(original), mapping[original]
    return None, None


def _dict_to_subtitle_cue(node: dict[str, Any]) -> dict[str, object] | None:
    _, text_value = _lookup_case_insensitive(
        node,
        ("text", "sentence", "content", "utterance", "subtitle", "caption", "transcript", "value"),
    )
    _, start_value = _lookup_case_insensitive(
        node,
        (
            "start",
            "start_ms",
            "starttime",
            "start_time",
            "begin",
            "begin_ms",
            "begin_time",
            "beginTime",
            "offset",
            "timestamp",
        ),
    )
    _, end_value = _lookup_case_insensitive(
        node,
        ("end", "end_ms", "endtime", "end_time", "stop", "to", "finish", "finish_time"),
    )
    _, duration_value = _lookup_case_insensitive(node, ("duration", "duration_ms", "length"))

    text = _clean_subtitle_text(text_value)
    start_ms = _coerce_timestamp(start_value)
    end_ms = _coerce_timestamp(end_value)
    duration_ms = _coerce_timestamp(duration_value)
    if start_ms is None or not text:
        return None
    if end_ms is None and duration_ms is not None:
        end_ms = start_ms + duration_ms
    if end_ms is None:
        end_ms = start_ms
    return {
        "start_ms": max(int(start_ms), 0),
        "end_ms": max(int(end_ms), int(start_ms)),
        "text": text,
    }


def _parse_srt_like(text: str) -> list[dict[str, object]]:
    cues: list[dict[str, object]] = []
    if not text or "-->" not in text:
        return cues

    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if len(lines) >= 2 and re.fullmatch(r"\d+", lines[0]):
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        start_raw, _, end_raw = lines[0].partition("-->")
        start_ms = _parse_timecode_to_ms(start_raw.strip())
        end_ms = _parse_timecode_to_ms(end_raw.strip())
        text_value = _clean_subtitle_text(" ".join(lines[1:]))
        if start_ms is None or end_ms is None or not text_value:
            continue
        cues.append({"start_ms": start_ms, "end_ms": max(end_ms, start_ms), "text": text_value})
    return cues


def _normalize_subtitle_payload(payload: Any) -> list[dict[str, object]]:
    if payload is None:
        return []
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return []
        if text.startswith("[") or text.startswith("{"):
            try:
                return _normalize_subtitle_payload(json.loads(text))
            except Exception:
                pass
        return _parse_srt_like(text)
    if isinstance(payload, dict):
        direct = _dict_to_subtitle_cue(payload)
        if direct:
            return [direct]
        cues: list[dict[str, object]] = []
        preferred_keys = (
            "subtitles",
            "subtitle",
            "captions",
            "caption",
            "segments",
            "items",
            "sentences",
            "paragraphs",
            "words",
            "timestamps",
            "alignment",
            "alignments",
            "data",
        )
        for key in preferred_keys:
            _, value = _lookup_case_insensitive(payload, (key,))
            if value is None:
                continue
            cues.extend(_normalize_subtitle_payload(value))
            if cues:
                return cues
        for value in payload.values():
            cues.extend(_normalize_subtitle_payload(value))
            if cues:
                return cues
        return cues
    if isinstance(payload, list):
        cues: list[dict[str, object]] = []
        for item in payload:
            item_cues = _normalize_subtitle_payload(item)
            if item_cues:
                cues.extend(item_cues)
        return cues
    return []


def _collect_subtitle_candidates(node: Any, candidates: list[Any]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("subtitle", "caption", "timestamp", "align")):
                candidates.append(value)
            _collect_subtitle_candidates(value, candidates)
        return
    if isinstance(node, list):
        for item in node:
            _collect_subtitle_candidates(item, candidates)


def _extract_audio_length_ms(body: dict[str, Any]) -> int | None:
    extra_info = body.get("extra_info")
    if isinstance(extra_info, dict):
        audio_length = extra_info.get("audio_length")
        if isinstance(audio_length, (int, float)):
            return max(1, int(audio_length))
    return None


def _probe_audio_length_ms(audio_path: Path) -> int | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return max(1, round(float(result.stdout.strip()) * 1000))
    except Exception:
        return None


def _normalize_subtitle_scale(
    subtitles: list[dict[str, object]],
    audio_length_ms: int | None,
) -> list[dict[str, object]]:
    if not subtitles:
        return []

    last_end = max(int(item.get("end_ms", 0) or 0) for item in subtitles)
    scale = 1
    if audio_length_ms and 0 < last_end < max(audio_length_ms // 20, 500):
        scale = 1000
    elif 0 < last_end < 100 and len(subtitles) >= 2:
        scale = 1000

    normalized: list[dict[str, object]] = []
    for item in subtitles:
        start_ms = int(item.get("start_ms", 0) or 0) * scale
        end_ms = int(item.get("end_ms", start_ms) or start_ms) * scale
        text = _clean_subtitle_text(item.get("text", ""))
        if not text:
            continue
        normalized.append(
            {
                "start_ms": max(start_ms, 0),
                "end_ms": max(end_ms, start_ms),
                "text": text,
            }
        )
    return normalized


def _extract_subtitles(body: dict[str, Any]) -> tuple[list[dict[str, object]], list[object]]:
    candidates: list[Any] = []
    _collect_subtitle_candidates(body, candidates)
    best: list[dict[str, object]] = []
    best_payloads: list[object] = []
    for candidate in candidates:
        parsed = _normalize_subtitle_payload(candidate)
        if len(parsed) > len(best):
            best = parsed
            best_payloads = [candidate]
    return best, best_payloads


def _trace_id_from_body(body: dict[str, Any]) -> str | None:
    extra_info = body.get("extra_info")
    if isinstance(extra_info, dict):
        trace_id = extra_info.get("trace_id")
        if trace_id:
            return str(trace_id)
    return None


def _offset_subtitles(
    subtitles: list[dict[str, object]],
    offset_ms: int,
) -> list[dict[str, object]]:
    adjusted: list[dict[str, object]] = []
    for item in subtitles:
        start_ms = int(item.get("start_ms", 0) or 0) + offset_ms
        end_ms = int(item.get("end_ms", start_ms) or start_ms) + offset_ms
        adjusted.append(
            {
                "start_ms": max(start_ms, 0),
                "end_ms": max(end_ms, start_ms),
                "text": str(item.get("text", "")).strip(),
            }
        )
    return adjusted


def _parse_voice_rotation(raw_value: str) -> list[str]:
    return [
        item.strip().strip('"').strip("'")
        for item in (raw_value or "").split(",")
        if item.strip()
    ]


def resolve_voice_id(explicit_voice_id: str | None, rotation_key: str) -> str:
    if explicit_voice_id:
        return explicit_voice_id

    # Fixed voice — no rotation. Override via MINIMAX_VOICE_ID env var.
    return os.environ.get("MINIMAX_VOICE_ID", DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID


def minimax_tts_with_alignment(
    text: str,
    output_mp3: Path,
    *,
    voice_id: str | None = None,
    rotation_key: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    speed: float = 1.10,
) -> MiniMaxTTSResult:
    """Generate MP3 audio and return subtitle/timestamp metadata when available."""
    api_key = api_key or os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 MINIMAX_API_KEY 环境变量")
    model = model or os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
    resolved_voice_id = resolve_voice_id(voice_id, rotation_key or output_mp3.stem)
    prepared_text = _inject_breathing_pauses(text)
    chunks = _split_text(prepared_text)
    print(
        f"[minimax-tts] 整段模式: {len(chunks)} 段，model={model}, "
        f"voice={resolved_voice_id}, speed={speed}"
    )

    def _generate_once(active_voice_id: str) -> MiniMaxTTSResult:
        trace_ids: list[str] = []
        raw_subtitles: list[object] = []
        subtitles: list[dict[str, object]] = []
        output_mp3.parent.mkdir(parents=True, exist_ok=True)

        if len(chunks) == 1:
            body = _call_api_response(
                chunks[0],
                active_voice_id,
                api_key,
                speed,
                model,
                subtitle_enable=True,
            )
            audio_bytes = _extract_audio_bytes(body)
            output_mp3.write_bytes(audio_bytes)
            audio_length_ms = _extract_audio_length_ms(body) or _probe_audio_length_ms(output_mp3)
            parsed_subtitles, subtitle_payloads = _extract_subtitles(body)
            subtitles.extend(_normalize_subtitle_scale(parsed_subtitles, audio_length_ms))
            raw_subtitles.extend(subtitle_payloads)
            trace_id = _trace_id_from_body(body)
            if trace_id:
                trace_ids.append(trace_id)
            print(f"[minimax-tts] 生成完成: {output_mp3} ({len(audio_bytes):,} bytes)")
            return MiniMaxTTSResult(
                voice_id=active_voice_id,
                output_mp3=str(output_mp3),
                audio_length_ms=audio_length_ms,
                subtitles=subtitles,
                trace_ids=trace_ids,
                raw_subtitles=raw_subtitles,
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chunk_files: list[Path] = []
            cumulative_ms = 0
            total_audio_length_ms = 0
            for i, chunk in enumerate(chunks):
                print(f"[minimax-tts] 处理第 {i + 1}/{len(chunks)} 段...")
                body = _call_api_response(
                    chunk,
                    active_voice_id,
                    api_key,
                    speed,
                    model,
                    subtitle_enable=True,
                )
                audio_bytes = _extract_audio_bytes(body)
                chunk_file = tmp_path / f"chunk_{i:03d}.mp3"
                chunk_file.write_bytes(audio_bytes)
                chunk_files.append(chunk_file)

                audio_length_ms = _extract_audio_length_ms(body) or _probe_audio_length_ms(chunk_file) or 0
                total_audio_length_ms += audio_length_ms
                parsed_subtitles, subtitle_payloads = _extract_subtitles(body)
                scaled_subtitles = _normalize_subtitle_scale(parsed_subtitles, audio_length_ms or None)
                subtitles.extend(_offset_subtitles(scaled_subtitles, cumulative_ms))
                raw_subtitles.extend(subtitle_payloads)
                cumulative_ms += audio_length_ms

                trace_id = _trace_id_from_body(body)
                if trace_id:
                    trace_ids.append(trace_id)

            list_file = tmp_path / "filelist.txt"
            list_file.write_text(
                "\n".join(f"file '{f}'" for f in chunk_files), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_file),
                    "-c",
                    "copy",
                    str(output_mp3),
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg 合并失败: {result.stderr}")

        probed_total_ms = _probe_audio_length_ms(output_mp3)
        return MiniMaxTTSResult(
            voice_id=active_voice_id,
            output_mp3=str(output_mp3),
            audio_length_ms=probed_total_ms or total_audio_length_ms or None,
            subtitles=subtitles,
            trace_ids=trace_ids,
            raw_subtitles=raw_subtitles,
        )

    try:
        return _generate_once(resolved_voice_id)
    except RuntimeError as exc:
        if "voice id not exist" not in str(exc):
            raise
        fallback_voice_id = os.environ.get("MINIMAX_VOICE_ID", DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID
        if fallback_voice_id == resolved_voice_id:
            raise
        print(f"[minimax-tts] 音色 {resolved_voice_id} 不可用，回退到 {fallback_voice_id}")
        return _generate_once(fallback_voice_id)


def minimax_tts(
    text: str,
    output_mp3: Path,
    *,
    voice_id: str | None = None,
    rotation_key: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    speed: float = 1.10,
) -> str:
    """Generate MP3 audio from text using MiniMax TTS."""
    result = minimax_tts_with_alignment(
        text,
        output_mp3,
        voice_id=voice_id,
        rotation_key=rotation_key,
        api_key=api_key,
        model=model,
        speed=speed,
    )
    return result.voice_id


def minimax_tts_chunks(
    chunks: list[str],
    output_dir: Path,
    *,
    voice_id: str | None = None,
    rotation_key: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    speed: float = 1.10,
) -> tuple[list[Path], str]:
    """为每个文本切片生成独立的 WAV 文件，返回 (wav路径列表, 使用的voice_id)。

    使用 WAV 格式避免 MP3 帧填充带来的时长偏移。
    """
    api_key = api_key or os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 MINIMAX_API_KEY 环境变量")
    model = model or os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
    resolved_voice_id = resolve_voice_id(voice_id, rotation_key or "chunk")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[minimax-tts] 切片模式: {len(chunks)} 段，model={model}, voice={resolved_voice_id}, speed={speed}")

    wav_paths: list[Path] = []
    for i, chunk_text in enumerate(chunks):
        chunk_text = chunk_text.strip()
        if not chunk_text:
            continue

        # 断点续传：已存在则直接复用
        wav_path = output_dir / f"chunk_{i:03d}.wav"
        if wav_path.exists():
            wav_paths.append(wav_path)
            print(f"[minimax-tts] 切片 {i}/{len(chunks)-1} 已存在，跳过: {wav_path.name}")
            continue

        # 注入微停顿标记，增强 TTS 呼吸感
        chunk_text = _inject_breathing_pauses(chunk_text)
        # 长文本需要二次拆分
        sub_chunks = _split_text(chunk_text)
        tmp_mp3 = output_dir / f"chunk_{i:03d}_raw.mp3"

        if len(sub_chunks) == 1:
            audio_bytes = _call_api(sub_chunks[0], resolved_voice_id, api_key, speed, model)
            tmp_mp3.write_bytes(audio_bytes)
        else:
            # 多个子段先各自生成再拼接
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                sub_files: list[Path] = []
                for j, sub in enumerate(sub_chunks):
                    audio_bytes = _call_api(sub, resolved_voice_id, api_key, speed, model)
                    sf = tmp_path / f"sub_{j:03d}.mp3"
                    sf.write_bytes(audio_bytes)
                    sub_files.append(sf)
                list_file = tmp_path / "filelist.txt"
                list_file.write_text(
                    "\n".join(f"file '{f}'" for f in sub_files), encoding="utf-8"
                )
                result = subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", str(list_file), "-c", "copy", str(tmp_mp3)],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"ffmpeg 合并子段失败: {result.stderr}")

        # MP3 → WAV 无损转码，消除帧填充偏移
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(tmp_mp3),
             "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1",
             str(wav_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg MP3→WAV 转码失败: {result.stderr}")
        tmp_mp3.unlink(missing_ok=True)
        wav_paths.append(wav_path)
        print(f"[minimax-tts] 切片 {i}/{len(chunks)-1} 完成: {wav_path.name}")

    print(f"[minimax-tts] 切片生成完成: 共 {len(wav_paths)} 个 WAV 文件")
    return wav_paths, resolved_voice_id


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MiniMax TTS 生成工具")
    parser.add_argument("text_file", help="文本文件路径")
    parser.add_argument("output_mp3", help="输出 MP3 路径")
    parser.add_argument("--voice-id", default=None, help="声音 ID（默认普通话新闻女主播）")
    parser.add_argument("--model", default=None, help="模型 ID（默认 speech-2.8-hd）")
    parser.add_argument("--speed", type=float, default=1.10, help="语速（0.5-2.0，默认 1.10）")
    args = parser.parse_args()
    text = Path(args.text_file).read_text(encoding="utf-8")
    minimax_tts(text, Path(args.output_mp3), voice_id=args.voice_id, model=args.model, speed=args.speed)
    print(f"生成完成: {args.output_mp3}")
