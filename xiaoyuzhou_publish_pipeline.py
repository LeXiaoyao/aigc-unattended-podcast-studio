#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent)


def _load_env_defaults() -> None:
    for rel in ("shared/config/.env.listenhub", ".env.listenhub", ".env"):
        env_file = ROOT_DIR / rel
        if not env_file.is_file():
            continue
        for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("\"'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_env_defaults()
PODCAST_PID = os.environ.get("XIAOYUZHOU_PODCAST_PID", "").strip()
API_BASE = "https://podcaster-api.xiaoyuzhoufm.com/v1"
BALLOON_BASE = "https://balloon.xiaoyuzhoufm.com"
DEFAULT_BUILD_TIME = "2026-03-16 15:05:01 +0800"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
EPISODE_URL_TEMPLATE = f"https://podcaster.xiaoyuzhoufm.com/podcasts/{PODCAST_PID}/episode/{{eid}}"
EPISODE_EDIT_URL_TEMPLATE = f"https://podcaster.xiaoyuzhoufm.com/podcasts/{PODCAST_PID}/{{eid}}/episode?type=null"
PODCAST_PAGE_URL = f"https://podcaster.xiaoyuzhoufm.com/podcast/{PODCAST_PID}/library"
JXA_SCRIPT = ROOT_DIR / "xiaoyuzhou_api_session.jxa"
SHOWNOTE_TRANSITION_PATTERNS = (
    r"好，我们一条一条来看",
    r"先看(?:第?[一二三四五六七八九十\d]+条)?",
    r"首先(?:来关注|关注)?(?:今天的)?第?[一二三四五六七八九十\d]+条(?:消息|动态)?",
    r"接下来(?:是|请关注)?第?[一二三四五六七八九十\d]+条(?:消息|动态)?",
    r"第[一二三四五六七八九十\d]+条(?:是关于[^。！？]*|值得关注的动态|是美光的另一则消息|消息)?",
    r"下面(?:请)?关注第?[一二三四五六七八九十\d]+条(?:消息|动态)?",
    r"此外(?:还有一条重要消息)?",
    r"最后(?:再看)?(?:一条(?:动态|消息)|一条(?:是[^。！？]*)?)?",
    r"然后(?:我们)?(?:关注一下|继续关注|再看|看一下)?[^。！？]*",
)
SHOWNOTE_DISCOURSE_PREFIXES = (
    "最近",
    "目前",
    "现在",
    "今天",
    "有消息称",
    "消息称",
    "据悉",
    "据报道",
    "报道称",
    "另外",
    "此外",
)
SHOWNOTE_COMMENTARY_PREFIXES = (
    "这说明",
    "这意味着",
    "这也说明",
    "这也意味着",
    "这预示着",
    "如果觉得有用",
    "欢迎订阅AI观测站",
    "感谢收听AI观测站",
    "我们明天同一时间再见",
)
EPISODE_TITLE_VISIBLE_LIMIT = 36
EPISODE_TITLE_BODY_VISIBLE_LIMIT = 24
STRUCTURED_BULLET_LIMIT = 12
STRUCTURED_TIMELINE_LIMIT = 16
STRUCTURED_REF_LIMIT = 8
LISTENHUB_MARKERS = (
    "listenhub_episode_result.json",
    "listenhub_submit_payload.json",
    "listenhub_create_result.json",
    "listenhub_payload.json",
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_markdown(text: str) -> str:
    cleaned = text.replace("**", "").replace("__", "")
    cleaned = re.sub(r"^\s*#+\s*", "", cleaned)
    cleaned = re.sub(r"^\s*[*-]\s*", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def split_spoken_sentences(text: str) -> list[str]:
    parts = re.findall(r"[^。！？!?]+[。！？!?]?", text or "")
    return [part.strip() for part in parts if part.strip()]


def is_shownote_transition_sentence(sentence: str) -> bool:
    compact = re.sub(r"\s+", "", sentence or "").strip("。！？!?")
    if not compact:
        return True
    return any(re.fullmatch(pattern, compact) for pattern in SHOWNOTE_TRANSITION_PATTERNS)


def strip_shownote_sentence_prefix(sentence: str) -> str:
    cleaned = re.sub(r"^\s*(?:第?[一二三四五六七八九十\d]+[、，,:：]\s*)", "", sentence or "").strip()
    while True:
        original = cleaned
        for prefix in SHOWNOTE_DISCOURSE_PREFIXES:
            cleaned = re.sub(rf"^\s*{re.escape(prefix)}[，,:：]\s*", "", cleaned).strip()
        if cleaned == original:
            return cleaned


def trim_text_for_notes(text: str, limit: int = 90) -> str:
    text = strip_markdown(text)
    text = re.sub(r"\s*([，。！？；：])\s*", r"\1", text)
    text = re.sub(r"(?:\.{3,}|…+)$", "", text).strip(" ，、,:：;；-")
    text = re.sub(r"(?:，?(?:这说明|这意味着|这也说明|这也意味着|这预示着|也意味着|也标志着|标志着).*)$", "", text)
    if not text:
        return ""
    if any(text.startswith(prefix) for prefix in SHOWNOTE_COMMENTARY_PREFIXES):
        return ""
    if len(text) > limit:
        clauses = [part.strip() for part in re.split(r"[，；]", text.rstrip("。！？!?")) if part.strip()]
        best = ""
        for clause in clauses:
            candidate = clause if not best else f"{best}，{clause}"
            if len(candidate) <= limit:
                best = candidate
            else:
                break
        text = best.strip(" ，、,:：;；-")
    if not text:
        return ""
    if not re.search(r"[。！？!?]$", text):
        text += "。"
    return text


def clean_section_heading(text: str) -> str:
    heading = strip_markdown(text)
    heading = re.sub(r"^本期要点[:：]?\s*", "", heading)
    return heading


def normalize_shownote_heading(text: str) -> str:
    heading = strip_markdown(text)
    heading = re.sub(r"^[^\w\u4e00-\u9fff]+", "", heading)
    heading = re.sub(r"\s+", "", heading)
    return heading.strip()


def parse_reference_line(text: str) -> dict[str, str] | None:
    raw = text.strip()
    if not raw:
        return None
    raw = re.sub(r"^\d+[.)、]\s*", "", raw)
    raw = re.sub(r"^[•\-]\s*", "", raw).strip()
    if not raw:
        return None

    markdown_match = re.match(r"^\[([^\]]+)\]\((https?://[^)]+)\)$", raw)
    if markdown_match:
        return {"title": markdown_match.group(1).strip(), "url": markdown_match.group(2).strip()}

    angle_match = re.match(r"^(.+?)[:：]\s*<([^>]+)>$", raw)
    if angle_match:
        return {"title": strip_markdown(angle_match.group(1)), "url": angle_match.group(2).strip()}

    return {"title": strip_markdown(raw), "url": ""}


def section_detail_line(heading: str, bullet: str) -> str:
    detail = strip_markdown(bullet)
    if not detail:
        return ""
    if detail.startswith(heading):
        detail = detail[len(heading):].lstrip("：:，, ")
    return trim_text_for_notes(detail)


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def visible_title_length(text: str) -> int:
    length = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            length += 2
        elif ch.strip():
            length += 1
    return length


def trim_title_body(body: str, limit: int = EPISODE_TITLE_BODY_VISIBLE_LIMIT) -> str:
    body = re.sub(r"\s+", "", body)
    body = re.sub(r"[，、,;；]+(?:[A-Za-z].*)?$", "", body)
    body = re.sub(r"(?:[-–—]\s*)?[A-Za-z][A-Za-z0-9 .&'/:+-]*$", "", body).strip(" ，、,:：;；-")
    if visible_title_length(body) <= limit:
        return body

    for separator in ["，", "、", ",", "；", ";", " ", "·", "/", "|"]:
        if separator in body:
            candidate = body.split(separator, 1)[0].strip(" ，、,:：;；-/|")
            if candidate and visible_title_length(candidate) <= limit:
                return candidate

    result = []
    score = 0
    for ch in body:
        if not ch.strip():
            continue
        ch_score = 2 if "\u4e00" <= ch <= "\u9fff" else 1
        if score + ch_score > limit:
            break
        result.append(ch)
        score += ch_score
    return "".join(result).strip(" ，、,:：;；-/|")


def shorten_episode_title(raw_title: str) -> str:
    title = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    if not title:
        return "AI播客"

    if has_cjk(title) and visible_title_length(title) <= EPISODE_TITLE_VISIBLE_LIMIT:
        return title

    normalized = title.replace("AI日报", "AI").replace("AI 日报", "AI")
    normalized = normalized.replace("： ", "：").replace(": ", "：")
    normalized = re.sub(r"\s*[|｜-]\s*[^:：]+$", "", normalized)

    prefix = ""
    body = normalized
    if "：" in normalized:
        prefix, body = normalized.split("：", 1)
    elif ":" in normalized:
        prefix, body = normalized.split(":", 1)

    if re.match(r"^\d{4}年\d{1,2}月\d{1,2}日AI$", prefix):
        prefix = re.sub(r"^(\d{4}年\d{1,2}月)\d{1,2}日AI$", r"\1AI", prefix)
    elif not prefix and re.match(r"^\d{4}年\d{1,2}月\d{1,2}日AI", body):
        matched_prefix = re.match(r"^(\d{4}年\d{1,2}月)\d{1,2}日AI[:：]?(.*)$", body)
        if matched_prefix:
            prefix = f"{matched_prefix.group(1)}AI"
            body = matched_prefix.group(2)

    body = trim_title_body(body)
    if prefix and body:
        candidate = f"{prefix}：{body}"
    else:
        candidate = body or prefix or normalized

    candidate = candidate.strip(" ，、,:：;；-/|")
    if has_cjk(candidate):
        return candidate
    return "AI播客"


def build_generic_episode_title(record: dict, episode_dir: Path) -> str:
    raw_date = str(record.get("日期", "")).strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw_date)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        return f"{year}年{month}月AI：今日焦点"

    folder_match = re.match(r"^(\d{4})(\d{2})(\d{2})", episode_dir.name)
    if folder_match:
        year = int(folder_match.group(1))
        month = int(folder_match.group(2))
        return f"{year}年{month}月AI：今日焦点"

    return "AI：今日焦点"


def load_listenhub_outline(episode_dir: Path) -> str:
    source, _, text = load_listenhub_shownotes_source(episode_dir)
    return text if source != "missing" else ""


def is_listenhub_episode(episode_dir: Path, record: dict | None = None) -> bool:
    if any((episode_dir / marker).exists() for marker in LISTENHUB_MARKERS):
        return True
    record = record or {}
    record_text = json.dumps(record, ensure_ascii=False).lower()
    if "listenhub" in record_text:
        return True
    for mp3_path in episode_dir.glob("*.mp3"):
        if "listenhub" in mp3_path.name.lower():
            return True
    return False


def load_listenhub_result(episode_dir: Path) -> dict:
    for path in [
        episode_dir / "listenhub_episode_result.json",
        episode_dir / "listenhub_create_result.json",
    ]:
        if not path.exists():
            continue
        try:
            data = load_json(path)
        except Exception:
            continue
        if data:
            return data
    return {}


def load_listenhub_title(episode_dir: Path) -> str:
    data = load_listenhub_result(episode_dir)
    return str(data.get("title") or "").strip()


def load_listenhub_shownotes_source(episode_dir: Path) -> tuple[str, str, str]:
    data = load_listenhub_result(episode_dir)
    if not data:
        return "missing", "plain", ""

    rich_candidates = (
        "outline_html",
        "outlineHtml",
        "outlineRichText",
        "outline_rich_text",
        "richText",
        "rich_text",
    )
    for key in rich_candidates:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return key, "html", value.strip()

    markdown_candidates = ("outline_markdown", "outlineMarkdown", "outline", "content")
    for key in markdown_candidates:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return key, "markdown", value.strip()

    plain_candidates = ("outline_plain", "outlinePlain", "summary", "description")
    for key in plain_candidates:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return key, "plain", value.strip()

    return "missing", "plain", ""


def render_inline_markdown(text: str) -> str:
    escaped = html.escape(text.strip())
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def markdown_outline_to_html(markdown: str) -> str:
    rendered: list[str] = []
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            rendered.append("</ul>")
            in_ul = False
        if in_ol:
            rendered.append("</ol>")
            in_ol = False

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            close_lists()
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            close_lists()
            level = min(6, len(heading_match.group(1)))
            rendered.append(f"<h{level}>{render_inline_markdown(heading_match.group(2))}</h{level}>")
            continue

        bullet_match = re.match(r"^[*-]\s+(.+)$", stripped)
        if bullet_match:
            if in_ol:
                rendered.append("</ol>")
                in_ol = False
            if not in_ul:
                rendered.append("<ul>")
                in_ul = True
            rendered.append(f"<li>{render_inline_markdown(bullet_match.group(1))}</li>")
            continue

        ordered_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if ordered_match:
            if in_ul:
                rendered.append("</ul>")
                in_ul = False
            if not in_ol:
                rendered.append("<ol>")
                in_ol = True
            rendered.append(f"<li>{render_inline_markdown(ordered_match.group(1))}</li>")
            continue

        close_lists()
        rendered.append(f"<p>{render_inline_markdown(stripped)}</p>")

    close_lists()
    return "".join(rendered).strip()


def build_listenhub_shownotes(episode_dir: Path) -> tuple[str, dict]:
    source, source_format, text = load_listenhub_shownotes_source(episode_dir)
    if not text:
        return "", {
            "shownotes_source": "fallback_script",
            "payload_format": "html",
            "fallback_reason": "ListenHub outline missing, fallback to script-generated shownotes",
        }
    if source_format == "html":
        shownotes = text
        payload_format = "html"
    elif source_format == "markdown":
        shownotes = markdown_outline_to_html(text)
        payload_format = "html"
    else:
        shownotes = format_shownotes_for_xiaoyuzhou(text, episode_dir)
        payload_format = "plain"
    return shownotes, {
        "shownotes_source": source,
        "source_format": source_format,
        "payload_format": payload_format,
    }


def analyze_shownotes_payload(shownotes: str, meta: dict | None = None) -> dict:
    meta = dict(meta or {})
    has_html = bool(re.search(r"<(?:p|h[1-6]|ul|ol|li|strong|br|a)\b", shownotes))
    has_bold = "<strong>" in shownotes or "<b>" in shownotes
    has_bullet = "<li>" in shownotes or "•" in shownotes
    block_count = len(re.findall(r"<(?:p|h[1-6]|li)\b", shownotes))
    plain_text = re.sub(r"<[^>]+>", "\n", shownotes)
    nonempty_lines = [line.strip() for line in plain_text.splitlines() if line.strip()]
    looks_like_flat_plain = (not has_html and len(nonempty_lines) <= 1) or (has_html and block_count <= 1)
    meta.update(
        {
            "payload_format": meta.get("payload_format") or ("html" if has_html else "plain"),
            "payload_has_bold": has_bold,
            "payload_has_bullet": has_bullet,
            "payload_block_count": block_count,
            "payload_looks_like_flat_plain": looks_like_flat_plain,
            "shownotes_sample": shownotes[:500],
        }
    )
    return meta


def validate_listenhub_shownotes_payload(shownotes: str, meta: dict) -> None:
    analysis = analyze_shownotes_payload(shownotes, meta)
    if analysis.get("payload_looks_like_flat_plain"):
        raise RuntimeError(
            "ListenHub shownotes payload 仍然像一整段纯文本，已禁止提交；"
            f"source={analysis.get('shownotes_source')} format={analysis.get('payload_format')}"
        )


def build_compact_outline_shownotes(outline: str) -> str:
    lines = [line.strip() for line in outline.splitlines() if line.strip()]
    intro_parts: list[str] = []
    sections: list[dict] = []
    current: dict | None = None

    for line in lines:
        if line.startswith("###"):
            current = {
                "heading": strip_markdown(line),
                "bullets": [],
            }
            sections.append(current)
            continue

        if line.startswith("*") or line.startswith("-"):
            bullet = strip_markdown(line)
            if current and bullet:
                current["bullets"].append(bullet)
            continue

        if not sections:
            intro_parts.append(strip_markdown(line))
        elif current is not None and line:
            current["bullets"].append(strip_markdown(line))

    summary_lines: list[str] = []
    intro = " ".join(part for part in intro_parts if part)
    if intro:
        summary_lines.append(trim_text_for_notes(intro, limit=220))

    if sections:
        for section in sections[:3]:
            heading = clean_section_heading(section["heading"])
            bullets = [item for item in section["bullets"] if item]
            summary_lines.append("")
            summary_lines.append(heading)
            for bullet in bullets[:3]:
                detail = trim_text_for_notes(strip_markdown(bullet), limit=150)
                if detail:
                    summary_lines.append("")
                    summary_lines.append(f"• {detail}")

    shownotes = "\n".join(summary_lines).strip()
    if len(shownotes) > 1200:
        shownotes = shownotes[:1197].rstrip() + "..."
    return shownotes


def build_fallback_shownotes(record: dict, title: str) -> str:
    trend_lines = []
    for raw_line in str(record.get("三大趋势", "")).splitlines():
        cleaned = strip_markdown(raw_line)
        if cleaned:
            trend_lines.append(cleaned.replace("趋势1：", "").replace("趋势2：", "").replace("趋势3：", ""))

    bullets = [trim_text_for_notes(trend, limit=150) for trend in trend_lines[:6] if trim_text_for_notes(trend, limit=150)]
    if not bullets:
        bullets = [f"这一期聚焦 {title}，梳理当天最值得关注的 AI 产业与产品变化。"]
    return "\n\n".join(f"{i+1}) {bullet}" for i, bullet in enumerate(bullets))


def load_prepared_shownotes(episode_dir: Path) -> str:
    shownotes_path = episode_dir / "shownotes.txt"
    if not shownotes_path.exists():
        return ""
    text = shownotes_path.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    return text


def _probe_audio_duration_seconds(audio_path: Path) -> int | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            try:
                return max(1, round(float(proc.stdout.strip())))
            except ValueError:
                pass

    afinfo = shutil.which("afinfo")
    if afinfo:
        proc = subprocess.run([afinfo, str(audio_path)], text=True, capture_output=True, check=False)
        match = re.search(r"estimated duration:\s*([\d.]+)\s*sec", proc.stdout)
        if match:
            return max(1, round(float(match.group(1))))
    return None


def _parse_mmss_seconds(value: str) -> int | None:
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d)", str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _extract_shownotes_timestamps(text: str) -> list[int]:
    timestamps: list[int] = []
    for match in re.finditer(r"(?<!\d)(\d{1,3}:[0-5]\d)(?!\d)", text or ""):
        seconds = _parse_mmss_seconds(match.group(1))
        if seconds is not None:
            timestamps.append(seconds)
    return timestamps


def _brief_text(value: str, limit: int = 300) -> str:
    compact = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip()


def preflight_solo_shownotes(episode_dir: Path, mp3_path: Path, shownotes_text: str) -> dict:
    shownotes_path = (episode_dir / "shownotes.txt").resolve()
    expected_path = shownotes_path
    if shownotes_path.name != "shownotes.txt" or "shownotes_before" in str(shownotes_path):
        raise RuntimeError(f"shownotes 内容源路径非法: {shownotes_path}")
    if not shownotes_path.exists():
        raise RuntimeError(f"最终 shownotes 文件不存在，禁止 fallback 发布: {shownotes_path}")
    if shownotes_path != expected_path:
        raise RuntimeError(f"shownotes 内容源不是当前 episode 正式文件: {shownotes_path}")
    if not shownotes_text.strip():
        raise RuntimeError(f"最终 shownotes 文件为空，禁止 fallback 发布: {shownotes_path}")

    audio_duration_seconds = _probe_audio_duration_seconds(mp3_path)
    timestamps = _extract_shownotes_timestamps(shownotes_text)
    last_timestamp_seconds = timestamps[-1] if timestamps else None
    if audio_duration_seconds and last_timestamp_seconds is not None:
        if last_timestamp_seconds > audio_duration_seconds + 5:
            raise RuntimeError(
                f"shownotes 最后时间戳超过真实音频时长: last={last_timestamp_seconds}s audio={audio_duration_seconds}s"
            )
        if last_timestamp_seconds < audio_duration_seconds * 0.6:
            raise RuntimeError(
                f"shownotes 最后时间戳明显早于真实音频: last={last_timestamp_seconds}s audio={audio_duration_seconds}s"
            )

    stat = shownotes_path.stat()
    sha256 = hashlib.sha256(shownotes_path.read_bytes()).hexdigest()
    meta = {
        "shownotes_path": str(shownotes_path),
        "shownotes_sha256": sha256,
        "shownotes_mtime": datetime.fromtimestamp(stat.st_mtime, BEIJING_TZ).isoformat(timespec="seconds"),
        "shownotes_size": stat.st_size,
        "shownotes_head_300": _brief_text(shownotes_text[:300], 300),
        "shownotes_tail_300": _brief_text(shownotes_text[-300:], 300),
        "shownotes_timestamp_count": len(timestamps),
        "shownotes_last_timestamp": (
            f"{last_timestamp_seconds // 60:02d}:{last_timestamp_seconds % 60:02d}"
            if last_timestamp_seconds is not None
            else ""
        ),
        "audio_duration_seconds": audio_duration_seconds,
    }
    print("[xiaoyuzhou preflight] final shownotes source:")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


def load_reference_links(episode_dir: Path, limit: int = 3) -> list[dict[str, str]]:
    research_json_path = episode_dir / "research.json"
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    if research_json_path.exists():
        try:
            payload = load_json(research_json_path)
        except Exception:
            payload = {}
        for item in payload.get("sources", []) or []:
            url = str(item.get("url") or item.get("content") or "").strip()
            title = str(item.get("title") or item.get("name") or url).strip()
            if not url or url in seen:
                continue
            refs.append({"title": title, "url": url})
            seen.add(url)
            if len(refs) >= limit:
                return refs

    research_md_path = episode_dir / "research.md"
    if research_md_path.exists() and len(refs) < limit:
        text = research_md_path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"-\s*([^：\n]+)：<([^>]+)>", text):
            title = match.group(1).strip()
            url = match.group(2).strip()
            if not url or url in seen:
                continue
            refs.append({"title": title, "url": url})
            seen.add(url)
            if len(refs) >= limit:
                break
    return refs


def parse_structured_shownotes(text: str) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    bullets: list[str] = []
    timeline: list[dict[str, str]] = []
    refs: list[dict[str, str]] = []
    current_section = "bullets"

    for raw_line in (text or "").splitlines():
        raw = raw_line.strip()
        if not raw:
            continue
        normalized_heading = normalize_shownote_heading(raw)
        if normalized_heading in {"本期要点", "本期内容"}:
            current_section = "bullets"
            continue
        if normalized_heading == "时间线":
            current_section = "timeline"
            continue
        if normalized_heading in {"参考资料与延伸阅读", "参考资料", "延伸阅读", "信息源"}:
            current_section = "refs"
            continue

        line = strip_markdown(raw).strip()
        if current_section == "timeline":
            match = re.match(r"^(\d{1,2}:\d{2})\s+(.+)$", line)
            if match:
                label = trim_text_for_notes(match.group(2), limit=140)
                if label:
                    timeline.append({"time": match.group(1), "label": label})
                continue
        if current_section == "refs":
            ref = parse_reference_line(raw)
            if ref and ref.get("title"):
                refs.append(ref)
            continue
        cleaned = re.sub(r"^\d+[.)、]\s*", "", line)
        cleaned = re.sub(r"^[•\-]\s*", "", cleaned).strip()
        if cleaned:
            bullets.append(cleaned)

    return (
        bullets[:STRUCTURED_BULLET_LIMIT],
        timeline[:STRUCTURED_TIMELINE_LIMIT],
        refs[:STRUCTURED_REF_LIMIT],
    )


def _plain_paragraphs_to_html(text: str) -> str:
    """Convert plain-text shownotes to <p> HTML, preserving structure without numbering."""
    raw = (text or "").strip()
    if not raw:
        return ""
    if "<p>" in raw or "<br" in raw:
        return raw
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", raw) if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paragraphs)


def format_shownotes_for_xiaoyuzhou(text: str, episode_dir: Path | None = None) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    if "<p>" in raw or "<br" in raw:
        return raw

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", raw) if part.strip()]
    if not paragraphs:
        return html.escape(raw)

    bullets, timeline, parsed_refs = parse_structured_shownotes(raw)
    rendered: list[str] = []
    if bullets:
        rendered.append("<p>🧠 本期要点</p>")
        for index, bullet in enumerate(bullets, start=1):
            escaped = html.escape(bullet).replace("\n", "<br/>")
            rendered.append(f"<p>{index}) {escaped}</p>")
    if timeline:
        rendered.append("<p>⏱ 时间线</p>")
        for item in timeline:
            escaped = html.escape(item["label"]).replace("\n", "<br/>")
            rendered.append(f'<p><a class="timestamp">{item["time"]}</a> {escaped}</p>')
    if not rendered:
        rendered.append("<p>🧠 本期要点</p>")
        for index, paragraph in enumerate(paragraphs, start=1):
            escaped = html.escape(paragraph).replace("\n", "<br/>")
            rendered.append(f"<p>{index}) {escaped}</p>")

    loaded_refs = load_reference_links(episode_dir, limit=3) if episode_dir else []
    refs = parsed_refs
    if refs and not any(ref.get("url") for ref in refs) and loaded_refs:
        refs = loaded_refs
    if refs:
        rendered.append("<p>🔗 参考资料与延伸阅读</p>")
        for ref in refs:
            title = html.escape(ref.get("title", ""))
            url = html.escape(ref.get("url", ""), quote=True)
            if url:
                rendered.append(f'<p>• <a href="{url}">{title}</a></p>')
            elif title:
                rendered.append(f"<p>• {title}</p>")
    return "".join(rendered)


def build_script_based_shownotes(episode_dir: Path, title: str) -> str:
    script_path = episode_dir / "podcast_script.txt"
    if not script_path.exists():
        return ""

    script_text = script_path.read_text(encoding="utf-8").strip()
    if not script_text:
        return ""
    opening_pattern = (
        r"^观众朋友们，大家好。这里是AI观测站，我是您的主播。"
        r"今天是\d{4}年\d{2}月\d{2}日，以下是今日AI产业重要动态。"
    )
    body = re.sub(opening_pattern, "", script_text).strip()
    body = re.sub(r"^嗨，早上好.*?好，我们一条一条来看。", "", body).strip()
    body = re.sub(r"以上是今天的AI产业全链路观察，感谢收听AI观测站，我们明天见。?$", "", body).strip()
    body = re.sub(r"以上就是今天的AI产业观察。.*$", "", body).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", body) if part.strip()]
    bullets: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        sentences = split_spoken_sentences(paragraph) or [paragraph.strip()]
        summary = ""
        for sentence in sentences:
            if is_shownote_transition_sentence(sentence):
                continue
            candidate = strip_shownote_sentence_prefix(sentence)
            if not candidate:
                continue
            if any(candidate.startswith(prefix) for prefix in SHOWNOTE_COMMENTARY_PREFIXES):
                continue
            summary = trim_text_for_notes(candidate, limit=90)
            if summary:
                break
        key = strip_markdown(summary).rstrip("。！？!? ")
        if summary and key and key not in seen:
            bullets.append(summary)
            seen.add(key)
        if len(bullets) >= 10:
            break

    if not bullets:
        return ""
    return "\n\n".join(f"{i+1}) {bullet}" for i, bullet in enumerate(bullets))


def build_shownotes(record: dict, episode_dir: Path, title: str) -> str:
    shownotes, meta = build_shownotes_with_metadata(record, episode_dir, title)
    return shownotes


def build_shownotes_with_metadata(record: dict, episode_dir: Path, title: str) -> tuple[str, dict]:
    flow_type = detect_flow_type(episode_dir)
    if flow_type == "solo":
        shownotes_path = episode_dir / "shownotes.txt"
        prepared_shownotes = load_prepared_shownotes(episode_dir)
        if not prepared_shownotes:
            print(
                f"[xiaoyuzhou WARNING] 最终 shownotes 缺失或为空，禁止 solo 发布 fallback: {shownotes_path}",
                file=sys.stderr,
            )
            raise RuntimeError(f"缺少最终 shownotes 文件，禁止 fallback 发布: {shownotes_path}")
        shownotes = format_shownotes_for_xiaoyuzhou(prepared_shownotes, episode_dir)
        return shownotes, analyze_shownotes_payload(
            shownotes,
            {
                "shownotes_source": "shownotes_txt",
                "source_path": str(shownotes_path.resolve()),
            },
        )

    if is_listenhub_episode(episode_dir, record):
        listenhub_shownotes, meta = build_listenhub_shownotes(episode_dir)
        if listenhub_shownotes:
            validate_listenhub_shownotes_payload(listenhub_shownotes, meta)
            return listenhub_shownotes, analyze_shownotes_payload(listenhub_shownotes, meta)
        print("ListenHub outline missing, fallback to script-generated shownotes", file=sys.stderr)
        script_shownotes = build_script_based_shownotes(episode_dir, title)
        if script_shownotes:
            shownotes = format_shownotes_for_xiaoyuzhou(script_shownotes, episode_dir)
            return shownotes, analyze_shownotes_payload(shownotes, meta)
        shownotes = format_shownotes_for_xiaoyuzhou(build_fallback_shownotes(record, title), episode_dir)
        return shownotes, analyze_shownotes_payload(shownotes, meta)

    prepared_shownotes = load_prepared_shownotes(episode_dir)
    if prepared_shownotes:
        shownotes = format_shownotes_for_xiaoyuzhou(prepared_shownotes, episode_dir)
        return shownotes, analyze_shownotes_payload(shownotes, {"shownotes_source": "shownotes_txt"})
    script_shownotes = build_script_based_shownotes(episode_dir, title)
    if script_shownotes:
        shownotes = format_shownotes_for_xiaoyuzhou(script_shownotes, episode_dir)
        return shownotes, analyze_shownotes_payload(shownotes, {"shownotes_source": "script_generated"})
    outline = load_listenhub_outline(episode_dir)
    if outline:
        shownotes = build_compact_outline_shownotes(outline)
        if shownotes:
            formatted = format_shownotes_for_xiaoyuzhou(shownotes, episode_dir)
            return formatted, analyze_shownotes_payload(formatted, {"shownotes_source": "compact_outline"})
    shownotes = format_shownotes_for_xiaoyuzhou(build_fallback_shownotes(record, title), episode_dir)
    return shownotes, analyze_shownotes_payload(shownotes, {"shownotes_source": "record_fallback"})


def read_episode_inputs(episode_dir: Path) -> dict:
    record_path = episode_dir / "record.json"
    record = load_json(record_path) if record_path.exists() else {}

    mp3_files = sorted(
        (
            path
            for path in episode_dir.glob("*.mp3")
            if path.is_file()
            and not path.name.startswith("highlight_")
            and not path.name.endswith("_raw.mp3")
        ),
        key=lambda path: (path.name != "podcast_solo.mp3", -path.stat().st_mtime),
    )
    if not mp3_files:
        raise RuntimeError(f"目录里没有 mp3: {episode_dir}")

    title_path = episode_dir / "title.txt"
    latest_title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""

    preferred_audio = record.get("音频路径")
    mp3_path = Path(preferred_audio).expanduser() if preferred_audio else mp3_files[0]
    if not mp3_path.exists():
        mp3_path = mp3_files[0]

    listenhub_episode = is_listenhub_episode(episode_dir, record)
    listenhub_title = load_listenhub_title(episode_dir) if listenhub_episode else ""
    title = (
        listenhub_title
        or latest_title
        or record.get("标题")
        or record.get("ListenHub标题")
        or mp3_path.stem
    )
    if title == "AI播客":
        title = build_generic_episode_title(record, episode_dir)

    cover_candidates: list[Path] = []
    if record.get("封面路径"):
        cover_candidates.append(Path(record["封面路径"]).expanduser())
    cover_candidates.extend(
        [
            episode_dir / "cover.jpeg",
            episode_dir / "cover.jpg",
            episode_dir / "cover.png",
            episode_dir / "image.png",
            episode_dir / "封面.jpg",
            episode_dir / "封面.jpeg",
            episode_dir / "封面.png",
        ]
    )
    cover_path = next((path for path in cover_candidates if path.exists()), None)

    shownotes, shownotes_meta = build_shownotes_with_metadata(record, episode_dir, title)
    flow_type = detect_flow_type(episode_dir)
    if flow_type == "solo":
        preflight_meta = preflight_solo_shownotes(episode_dir, mp3_path, load_prepared_shownotes(episode_dir))
        shownotes_meta.update(preflight_meta)

    # 可选：置顶与每日荐书模块（仅在配置了 JD_CPS_VENUE_URL 时启用）
    cps_html = ""
    try:
        from jd_cps_book import build_daily_book_html, build_venue_html

        cps_html = build_venue_html()
        book_html = build_daily_book_html()
        if book_html:
            cps_html += book_html
            shownotes_meta["jd_cps_book"] = True
    except Exception as exc:
        print(f"[xiaoyuzhou WARNING] 京东CPS 模块不可用，跳过: {exc}", file=sys.stderr)
    if cps_html:
        shownotes = cps_html + shownotes
        shownotes_meta["jd_cps_venue"] = True

    return {
        "title": title,
        "mp3_path": mp3_path,
        "cover_path": cover_path,
        "shownotes": shownotes,
        "shownotes_meta": shownotes_meta,
        "is_listenhub_episode": listenhub_episode,
        "record": record,
    }


def ensure_publishable_cover(episode_dir: Path, inputs: dict) -> None:
    cover_path: Path | None = inputs.get("cover_path")
    cover_meta_path = episode_dir / "cover_generation.json"
    if cover_path is None or not cover_path.exists():
        raise RuntimeError("封面图片尚未准备好；按当前规则，先上传资料库，等封面生成成功后再发布。")

    if not cover_meta_path.exists():
        raise RuntimeError("缺少 cover_generation.json；按当前规则，未确认生成成功的封面不能直接发布。")

    try:
        cover_meta = load_json(cover_meta_path)
    except Exception as exc:
        raise RuntimeError(f"封面生成记录不可读：{exc}") from exc

    if cover_meta.get("status") != "success":
        error = str(cover_meta.get("error", "")).strip()
        suffix = f" 失败原因：{error}" if error else ""
        raise RuntimeError(f"封面尚未生成成功；按当前规则，先不要发布。{suffix}")


def load_existing_result(episode_dir: Path) -> dict:
    result_path = episode_dir / "xiaoyuzhou_publish_result.json"
    if not result_path.exists():
        return {}
    try:
        return load_json(result_path)
    except Exception:
        return {}


def detect_flow_type(episode_dir: Path, existing: dict | None = None) -> str | None:
    flow_type_path = episode_dir / "flow_type.txt"
    if flow_type_path.exists():
        value = flow_type_path.read_text(encoding="utf-8").strip()
        if value:
            return value

    record_path = episode_dir / "record.json"
    if record_path.exists():
        try:
            record = load_json(record_path)
        except Exception:
            record = {}
        value = str(record.get("flow_type", "")).strip()
        if value:
            return value
        if record.get("ListenHub作品链接") or record.get("ListenHub标题"):
            return "dual_host"

    if (episode_dir / "listenhub_episode_result.json").exists() or (
        episode_dir / "listenhub_create_result.json"
    ).exists():
        return "dual_host"

    if existing:
        value = str(existing.get("flow_type", "")).strip()
        if value:
            return value

    return None


def load_schedule_publish_config(flow_type: str | None) -> dict | None:
    if not flow_type:
        return None
    schedule_path = ROOT_DIR / "schedule_config.json"
    if not schedule_path.exists():
        return None
    try:
        payload = load_json(schedule_path)
    except Exception:
        return None
    flow_config = payload.get(flow_type)
    return flow_config if isinstance(flow_config, dict) else None


def compute_configured_publish_at(flow_type: str | None) -> tuple[str | None, str | None]:
    config = load_schedule_publish_config(flow_type)
    if not config:
        return None, None

    try:
        publish_hour = int(config.get("publish_hour", 9))
        publish_minute = int(config.get("publish_minute", 30))
        publish_day_offset = int(config.get("publish_day_offset", 0))
        days_of_week = config.get("days_of_week", list(range(7)))
        days_of_week = [int(day) for day in days_of_week] or list(range(7))
    except Exception:
        return None, None

    now = datetime.now(BEIJING_TZ)
    min_publish_at = now + timedelta(hours=4, minutes=5)
    min_publish_at = min_publish_at.replace(second=0, microsecond=0)

    if publish_day_offset > 0:
        candidate = (now + timedelta(days=publish_day_offset)).replace(
            hour=publish_hour,
            minute=publish_minute,
            second=0,
            microsecond=0,
        )
        if candidate >= min_publish_at:
            return candidate.isoformat(timespec="seconds"), None

    today_candidate = now.replace(
        hour=publish_hour,
        minute=publish_minute,
        second=0,
        microsecond=0,
    )
    if now.weekday() in days_of_week:
        if today_candidate >= min_publish_at:
            return today_candidate.isoformat(timespec="seconds"), None
        warning = (
            f"{flow_type} 配置发布时间 {publish_hour:02d}:{publish_minute:02d} "
            f"早于小宇宙最短预约窗口，已顺延到 {min_publish_at.strftime('%Y-%m-%d %H:%M')}。"
        )
        return min_publish_at.isoformat(timespec="seconds"), warning

    for offset in range(1, 8):
        candidate = now + timedelta(days=offset)
        if candidate.weekday() not in days_of_week:
            continue
        candidate = candidate.replace(
            hour=publish_hour,
            minute=publish_minute,
            second=0,
            microsecond=0,
        )
        if candidate >= min_publish_at:
            return candidate.isoformat(timespec="seconds"), None

    return min_publish_at.isoformat(timespec="seconds"), None


def resolve_publish_at(existing: dict, episode_dir: Path, override: str | None) -> tuple[str, str | None]:
    if override:
        return compute_publish_at(episode_dir, override), None

    existing_pub_date = str(existing.get("pub_date", "")).strip()
    if existing_pub_date:
        dt = datetime.fromisoformat(existing_pub_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BEIJING_TZ)
        existing_local = dt.astimezone(BEIJING_TZ)
        if override is None:
            return existing_local.isoformat(timespec="seconds"), None
        min_publish_at = datetime.now(BEIJING_TZ) + timedelta(hours=4, minutes=5)
        min_publish_at = min_publish_at.replace(second=0, microsecond=0)
        if existing_local >= min_publish_at:
            return existing_local.isoformat(timespec="seconds"), None
        warning = (
            "原预约发布时间已不满足小宇宙最短预约窗口，"
            f"已自动顺延到 {min_publish_at.strftime('%Y-%m-%d %H:%M')}。"
        )
        return min_publish_at.isoformat(timespec="seconds"), warning

    flow_type = detect_flow_type(episode_dir, existing)
    configured_publish_at, warning = compute_configured_publish_at(flow_type)
    if configured_publish_at:
        return configured_publish_at, warning

    return compute_publish_at(episode_dir, None), None


def should_include_pub_date_on_update(existing: dict, publish_at: str) -> bool:
    existing_pub_date = str(existing.get("pub_date", "")).strip()
    if not existing_pub_date:
        return True
    try:
        scheduled_dt = datetime.fromisoformat(publish_at)
        if scheduled_dt.tzinfo is None:
            scheduled_dt = scheduled_dt.replace(tzinfo=BEIJING_TZ)
        scheduled_dt = scheduled_dt.astimezone(BEIJING_TZ)
    except Exception:
        return True

    min_schedulable = datetime.now(BEIJING_TZ) + timedelta(hours=4)
    return scheduled_dt >= min_schedulable


def compute_publish_at(episode_dir: Path, override: str | None) -> str:
    if override:
        dt = datetime.fromisoformat(override)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BEIJING_TZ)
        return dt.astimezone(BEIJING_TZ).isoformat(timespec="seconds")

    now = datetime.now(BEIJING_TZ)
    # XiaoYuZhou requires scheduled publishes to be at least 4 hours later.
    publish_at = now + timedelta(hours=4, minutes=5)
    publish_at = publish_at.replace(second=0, microsecond=0)
    if publish_at <= now + timedelta(hours=4):
        publish_at = publish_at + timedelta(minutes=1)
    return publish_at.isoformat(timespec="seconds")


def to_api_pub_date(local_iso: str) -> str:
    dt = datetime.fromisoformat(local_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    utc_dt = dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def compact_dict(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if value is not None}


def format_publish_at(local_iso: str) -> str:
    dt = datetime.fromisoformat(local_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    dt = dt.astimezone(BEIJING_TZ)
    return dt.strftime("%Y-%m-%d %H:%M (北京时间)")


def probe_audio_summary(mp3_path: Path) -> str:
    size_mb = mp3_path.stat().st_size / (1024 * 1024)
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            try:
                seconds = max(1, round(float(proc.stdout.strip())))
                return f"{seconds}秒 / {size_mb:.1f}MB"
            except ValueError:
                pass
    return f"{size_mb:.1f}MB"


def probe_cover_summary(cover_path: Path | None) -> str:
    if not cover_path or not cover_path.exists():
        return "未找到封面"
    proc = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", "-g", "format", str(cover_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    width_match = re.search(r"pixelWidth:\s*(\d+)", proc.stdout)
    height_match = re.search(r"pixelHeight:\s*(\d+)", proc.stdout)
    format_match = re.search(r"format:\s*([A-Za-z]+)", proc.stdout)
    if width_match and height_match:
        image_format = format_match.group(1).upper() if format_match else cover_path.suffix.lstrip(".").upper()
        return f"{width_match.group(1)}x{height_match.group(1)} {image_format}"
    return cover_path.name


def extract_shownote_bullets(shownotes: str, limit: int = 6) -> list[str]:
    bullets: list[str] = []
    for raw_line in shownotes.splitlines():
        line = re.sub(r"<[^>]+>", "", raw_line)
        heading = normalize_shownote_heading(line)
        if heading in {"参考资料与延伸阅读", "参考资料", "延伸阅读", "信息源"}:
            break
        line = strip_markdown(line).strip()
        if not line or heading in {"本期要点", "时间线", "信息源"} or line.startswith("AI观测站"):
            continue
        if re.match(r"^\d{1,2}:\d{2}\s+", line):
            continue
        line = re.sub(r"^\d+[.)、]\s*", "", line)
        line = re.sub(r"^[•\-]\s*", "", line)
        if not line:
            continue
        bullets.append(trim_text_for_notes(line, limit=120))
        if len(bullets) >= limit:
            break
    return bullets


def sync_publish_metadata(episode_dir: Path, inputs: dict, result: dict) -> None:
    record_path = episode_dir / "record.json"
    record = load_json(record_path) if record_path.exists() else {}
    record["标题"] = inputs["title"]
    record["音频路径"] = str(inputs["mp3_path"])
    record["封面路径"] = str(inputs["cover_path"]) if inputs.get("cover_path") else record.get("封面路径", "")
    record["小宇宙链接"] = result.get("episode_url", "")
    record["小宇宙后台链接"] = result.get("episode_edit_url", "")
    record["小宇宙发布时间"] = result.get("pub_date", "")
    record["发布状态"] = "published"
    record["小宇宙提审状态"] = "submitted_to_xiaoyuzhou"
    write_json(record_path, record)

    try:
        sys.path.insert(0, str(ROOT_DIR))
        from db.schema import get_episode, upsert_episode

        existing = get_episode(episode_dir.name) or {}
        flow_type = existing.get("flow_type") or record.get("flow_type") or "solo"
        fallback_date = episode_dir.name[:8]
        if re.match(r"^\d{8}$", fallback_date):
            fallback_date = f"{fallback_date[:4]}-{fallback_date[4:6]}-{fallback_date[6:8]}"
        date_value = existing.get("date") or record.get("日期") or fallback_date
        upsert_episode(
            {
                "id": episode_dir.name,
                "date": date_value,
                "flow_type": flow_type,
                "title": inputs["title"],
                "trends": existing.get("trends"),
                "products": existing.get("products"),
                "script_path": existing.get("script_path") or record.get("口播稿路径", ""),
                "audio_path": str(inputs["mp3_path"]),
                "cover_path": str(inputs["cover_path"]) if inputs.get("cover_path") else existing.get("cover_path", ""),
                "listenhub_url": existing.get("listenhub_url"),
                "listenhub_title": existing.get("listenhub_title"),
                "xiaoyuzhou_eid": result.get("eid"),
                "xiaoyuzhou_pub_date": result.get("pub_date"),
                "xiaoyuzhou_url": result.get("episode_url"),
                "status": "published",
            }
        )
    except Exception:
        pass


def _push_publish_review_package(episode_dir: Path, inputs: dict) -> None:
    """发布提交后把封面图、shownotes 全文、口播正文全文推到飞书，供人工验收。

    自动发布模式下没有发布前的人工确认环节，这个包就是验收入口：
    看完没问题就不用管；有问题去小宇宙后台改（预约发布前都来得及）。
    每条消息之间隔 1.5s，避开飞书 webhook 频率限制（code:11232）。
    """
    try:
        from mobile_notify import notify_feishu_image, notify_feishu_long_text
    except Exception:
        return

    episode = episode_dir.name

    cover_path = inputs.get("cover_path")
    if cover_path and Path(cover_path).exists():
        try:
            time.sleep(1.5)
            notify_feishu_image(str(cover_path), f"AI观测站 · {episode} 封面（已提交小宇宙）")
        except Exception as exc:
            print(f"[notify] 验收包封面推送失败: {exc}", file=sys.stderr)

    # shownotes：优先推 shownotes.txt 原文；没有（如 dual 从 outline 生成）就把
    # 提交给小宇宙的 HTML 去标签后推纯文本
    shownotes_text = ""
    shownotes_path = episode_dir / "shownotes.txt"
    if shownotes_path.exists():
        shownotes_text = shownotes_path.read_text(encoding="utf-8").strip()
    if not shownotes_text:
        shownotes_text = re.sub(r"<[^>]+>", "\n", str(inputs.get("shownotes") or ""))
        shownotes_text = re.sub(r"\n{3,}", "\n\n", shownotes_text).strip()
    if shownotes_text:
        try:
            time.sleep(1.5)
            notify_feishu_long_text(f"AI观测站 · {episode} shownotes 全文（已提交）", shownotes_text)
        except Exception as exc:
            print(f"[notify] 验收包 shownotes 推送失败: {exc}", file=sys.stderr)

    # 正文：dual 实际播出的是 ListenHub 对话稿，solo 是 podcast_script.txt
    for candidate in ("listenhub_script.txt", "podcast_script.txt"):
        script_path = episode_dir / candidate
        if not script_path.exists():
            continue
        script_text = script_path.read_text(encoding="utf-8").strip()
        if script_text:
            try:
                time.sleep(1.5)
                notify_feishu_long_text(f"AI观测站 · {episode} 正文全文（已提交）", script_text)
            except Exception as exc:
                print(f"[notify] 验收包正文推送失败: {exc}", file=sys.stderr)
        break


def notify_publish_success(episode_dir: Path, inputs: dict, result: dict) -> None:
    try:
        from mobile_notify import notify_rich
    except Exception:
        return

    bullets = extract_shownote_bullets(inputs.get("shownotes", ""))
    sections = [
        {"label": "发布状态", "text": "已提交小宇宙，等待平台审核发布"},
        {"label": "标题", "text": inputs["title"]},
        {"label": "音频", "text": probe_audio_summary(inputs["mp3_path"])},
        {"label": "封面", "text": probe_cover_summary(inputs.get("cover_path"))},
        {"label": "预约发布时间", "text": format_publish_at(result["pub_date"])},
    ]
    edit_url = result.get("episode_edit_url") or result.get("episode_url")
    if edit_url:
        sections.append({"label": "编辑链接", "text": "小宇宙后台", "href": edit_url})
    if bullets:
        sections.append({"label": "本期要点", "text": "\n".join(f"{idx}. {bullet}" for idx, bullet in enumerate(bullets, start=1))})
    if result.get("warnings"):
        sections.append({"label": "提示", "text": "；".join(result["warnings"][:3])})
    # 发布已经成功，成功通知只是锦上添花：飞书 POST 超时/失败绝不能拖垮返回码，
    # 否则 webapp 会把已发布的单集误判成失败并回退到待确认（2026-07-08 实测）。
    try:
        notify_rich(f"AI观测站 · {episode_dir.name} 已提交小宇宙", sections, desktop=False)
    except Exception as exc:
        print(f"[notify] 发布成功通知推送失败（不影响发布结果）: {exc}")
    # 验收包：封面 + shownotes + 正文全文，看完没问题就不用管
    try:
        _push_publish_review_package(episode_dir, inputs)
    except Exception as exc:
        print(f"[notify] 发布验收包推送失败（不影响发布结果）: {exc}")


def notify_publish_failure(
    episode_dir: Path,
    *,
    step: str,
    error: str,
    title: str = "",
    publish_at: str = "",
) -> None:
    try:
        from mobile_notify import notify_rich
    except Exception:
        return

    sections = [
        {"label": "失败步骤", "text": step},
        {"label": "期号", "text": episode_dir.name},
    ]
    if title:
        sections.append({"label": "标题", "text": title})
    if publish_at:
        try:
            sections.append({"label": "预约发布时间", "text": format_publish_at(publish_at)})
        except Exception:
            sections.append({"label": "预约发布时间", "text": publish_at})
    sections.append({"label": "错误", "text": trim_text_for_notes(error, limit=220)})
    # 401 / code:21 = 小宇宙判定登录态失效，自动强刷也救不回（会话已过期），必须人工重登。
    if "401" in error or '"code":21' in error or "code:21" in error:
        next_step = "小宇宙登录态已失效：请在 Chrome 重新登录小宇宙 Podcaster 后台，再重发确认指令。"
    else:
        next_step = "请检查发布日志、封面状态和小宇宙登录态。"
    sections.append({"label": "下一步", "text": next_step})
    notify_rich(f"AI观测站 · {episode_dir.name} 发布失败", sections, desktop=False)


AUTH_CACHE_FILE = ROOT_DIR / ".xiaoyuzhou_auth.json"


class BrowserSessionRequestError(RuntimeError):
    def __init__(self, method: str, url: str, status: int | None, body: str) -> None:
        super().__init__(f"浏览器会话请求失败: {method} {url} status={status} body={body}")
        self.status = status
        self.body = body


def decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without signature verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


# 小宇宙后台 tab 按需开关：发布流程自动开 tab，结束时只关掉自己开的，
# 用户手动开着的 tab 不动。XYZ_KEEP_TAB=1 可关闭此行为（调试用）。
_BROWSER_TAB_STATE = {
    "ensured": False,
    "created": False,
    "login_required": False,
    "recorded": False,
    "block_decision": None,
}

# Chrome 会话失效（后台页 302 到登录页）时，再怎么开 tab 也救不回，只能人工重登。
# 连续失败到阈值后熔断：不再新开 tab，避免定时任务一天 6 次地往 Chrome 里灌登录页。
#
# 熔断是"冷却"不是"闩死"：解除熔断的 clear_login_block() 只在成功取到 token 后调用，
# 而熔断时 ensure_podcaster_tab() 直接 return、根本走不到那一步——2026-08-16 就这样
# 在用户已人工重登后仍全天锁死。因此这里给两条自愈路径：
#   1) 零副作用探测：只读 Chrome 里现成的后台 tab（人工重登后必然留着），成功即解除；
#   2) 冷却兜底：tab 被关掉/Chrome 没开时，每 COOLDOWN 小时放行"一次"开 tab 的探测，
#      失败会刷新 last_failure_at 从而重新计时——最坏情况每 N 小时多一个登录页，
#      而不是恢复到"一天灌 6 次"。
LOGIN_REQUIRED_MARKER = "XYZ_LOGIN_REQUIRED"
LOGIN_BLOCK_FILE = ROOT_DIR / ".xiaoyuzhou_login_block.json"
LOGIN_RECOVERY_FILE = ROOT_DIR / ".xiaoyuzhou_login_recovered.json"
LOGIN_BLOCK_THRESHOLD = 3
LOGIN_BLOCK_MAX_FAILURES = 99  # 计数封顶，熔断后无意义地涨到几百没有信息量
try:
    LOGIN_BLOCK_COOLDOWN_HOURS = float(os.environ.get("XYZ_LOGIN_BLOCK_COOLDOWN_HOURS") or 2)
except ValueError:
    LOGIN_BLOCK_COOLDOWN_HOURS = 2.0


def _read_login_block() -> dict:
    try:
        return json.loads(LOGIN_BLOCK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record_login_required() -> int:
    """记一次"会话失效"。同一进程内只累加一次，避免一轮跑出多次计数把熔断提前触发。"""
    if _BROWSER_TAB_STATE.get("recorded"):
        return int(_read_login_block().get("consecutive_failures", 0))
    _BROWSER_TAB_STATE["recorded"] = True
    state = _read_login_block()
    count = min(int(state.get("consecutive_failures", 0)) + 1, LOGIN_BLOCK_MAX_FAILURES)
    LOGIN_BLOCK_FILE.write_text(
        json.dumps(
            {
                "consecutive_failures": count,
                "last_failure_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
                "reason": "Chrome 会话已失效，需人工重新登录小宇宙后台",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return count


def clear_login_block() -> None:
    if not LOGIN_BLOCK_FILE.exists():
        return
    blocked = _read_login_block()
    try:
        LOGIN_BLOCK_FILE.unlink()
    except Exception:
        return
    if int(blocked.get("consecutive_failures", 0)) < LOGIN_BLOCK_THRESHOLD:
        return
    # 从"已熔断"恢复过来才落时间戳：编排器据此给因登录态失败而放弃的单集补一次重试额度
    try:
        LOGIN_RECOVERY_FILE.write_text(
            json.dumps(
                {
                    "recovered_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
                    "previous_failures": int(blocked.get("consecutive_failures", 0)),
                    "previous_failure_at": blocked.get("last_failure_at", ""),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _probe_existing_session() -> bool:
    """只读 Chrome 里现成的后台 tab 判断会话是否已恢复：不新开 tab、不抢焦点。"""
    if not JXA_SCRIPT.exists():
        return False
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", str(JXA_SCRIPT), "probe", PODCAST_PAGE_URL],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        return False
    try:
        data = json.loads(lines[-1])
    except Exception:
        return False
    return bool(data.get("ok") and data.get("accessToken"))


def _login_block_cooldown_expired(state: dict) -> bool:
    raw = str(state.get("last_failure_at") or "").strip()
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=BEIJING_TZ)
    elapsed = (datetime.now(BEIJING_TZ) - last).total_seconds()
    return elapsed >= LOGIN_BLOCK_COOLDOWN_HOURS * 3600


def login_block_active() -> bool:
    if os.environ.get("XYZ_IGNORE_LOGIN_BLOCK") == "1":
        return False
    state = _read_login_block()
    if int(state.get("consecutive_failures", 0)) < LOGIN_BLOCK_THRESHOLD:
        return False

    cached = _BROWSER_TAB_STATE.get("block_decision")
    if cached is not None:
        return bool(cached)

    if _probe_existing_session():
        print("[browser] 探测到 Chrome 后台页登录态已恢复，解除熔断", file=sys.stderr)
        clear_login_block()
        active = False
    elif _login_block_cooldown_expired(state):
        print(
            f"[browser] 登录熔断已冷却 {LOGIN_BLOCK_COOLDOWN_HOURS:g}h，放行一次探测",
            file=sys.stderr,
        )
        active = False
    else:
        active = True
    _BROWSER_TAB_STATE["block_decision"] = active
    return active


def ensure_podcaster_tab() -> None:
    if _BROWSER_TAB_STATE["ensured"] or not JXA_SCRIPT.exists():
        return
    _BROWSER_TAB_STATE["ensured"] = True
    if login_block_active():
        _BROWSER_TAB_STATE["login_required"] = True
        print(
            "[browser] 小宇宙登录态已失效（连续刷新失败已熔断），跳过开 tab。"
            "请在 Chrome 重新登录 podcaster.xiaoyuzhoufm.com 后重试。",
            file=sys.stderr,
        )
        return
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", str(JXA_SCRIPT), "ensure", PODCAST_PAGE_URL],
            capture_output=True, text=True, check=False, timeout=40,
        )
        for line in reversed((proc.stdout + proc.stderr).strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                data = json.loads(line)
                if data.get("ok"):
                    _BROWSER_TAB_STATE["created"] = bool(data.get("created"))
                    if data.get("created"):
                        print("[browser] 已自动打开小宇宙后台 tab（结束后自动关闭）", file=sys.stderr)
                elif data.get("loginRequired"):
                    _BROWSER_TAB_STATE["login_required"] = True
                    count = record_login_required()
                    print(
                        f"[browser] 小宇宙登录态已失效（第 {count} 次），需在 Chrome 手动重新登录后台。",
                        file=sys.stderr,
                    )
                return
    except Exception as exc:
        print(f"[browser] ensure tab 失败（继续尝试现有会话）: {exc}", file=sys.stderr)


def close_podcaster_tab_if_created() -> None:
    # 会话失效那一路也要收尾：JXA 侧已关掉自己开的 tab，这里再扫一遍历史残留的登录页
    if os.environ.get("XYZ_KEEP_TAB") == "1":
        return
    if not _BROWSER_TAB_STATE["created"] and not _BROWSER_TAB_STATE["login_required"]:
        return
    try:
        subprocess.run(
            ["osascript", "-l", "JavaScript", str(JXA_SCRIPT), "close"],
            capture_output=True, text=True, check=False, timeout=20,
        )
        _BROWSER_TAB_STATE["created"] = False
        print("[browser] 已关闭本流程打开的小宇宙后台 tab", file=sys.stderr)
    except Exception as exc:
        print(f"[browser] 关闭 tab 失败（可手动关闭）: {exc}", file=sys.stderr)


def _run_jxa_auth() -> dict | None:
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", str(JXA_SCRIPT), "auth", PODCAST_PAGE_URL],
            # 冷启动（自动新开 tab）时 JXA 侧最多轮询 25s 等 SPA 写入 token
            capture_output=True, text=True, check=False, timeout=45,
        )
        raw = (proc.stdout + proc.stderr).strip()
        if LOGIN_REQUIRED_MARKER in raw:
            _BROWSER_TAB_STATE["login_required"] = True
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                data = json.loads(line)
                if data.get("accessToken") and data.get("refreshToken"):
                    return data
    except Exception:
        pass
    return None


def _token_age_seconds(access_token: str) -> float | None:
    payload = decode_jwt_payload(access_token)
    iat = payload.get("iat")
    if not isinstance(iat, (int, float)):
        return None
    return max(0.0, time.time() - float(iat))


def fetch_auth_from_chrome(*, require_fresh: bool = False, max_wait_seconds: float = 30.0) -> dict | None:
    """Try to get fresh auth from Chrome via JXA. Returns auth dict or None.

    require_fresh=True 时轮询等待 SPA 自刷新：自动新开的 tab 里 localStorage
    先出现的是上次留下的旧 token，SPA 启动后几秒到几十秒才写入新 token。
    20260613E1 发布 401 的根因就是抓到了旧 token（连浏览器会话都带着它被拒）。
    """
    if not JXA_SCRIPT.exists():
        return None
    ensure_podcaster_tab()
    if _BROWSER_TAB_STATE["login_required"]:
        return None
    deadline = time.monotonic() + max_wait_seconds
    last: dict | None = None
    while True:
        data = _run_jxa_auth()
        if data:
            last = data
            clear_login_block()
            if not require_fresh:
                return data
            age = _token_age_seconds(data.get("accessToken", ""))
            if age is None or age < 600:
                return data
        elif _BROWSER_TAB_STATE["login_required"]:
            # 会话失效，轮询等下去也只是反复打开登录页
            record_login_required()
            return None
            print(
                f"[token-refresh] localStorage 里仍是旧 token（age={int(age)}s），等 SPA 自刷新后重取...",
                file=sys.stderr,
            )
        if time.monotonic() >= deadline:
            return last
        time.sleep(8)


def load_auth(raw_auth_json: str | None) -> dict:
    if not raw_auth_json:
        raise RuntimeError("缺少小宇宙登录态，请通过 xiaoyuzhou_prepare_episode.sh 运行或传入 --auth-json")
    auth = json.loads(raw_auth_json)
    if not auth.get("accessToken") or not auth.get("refreshToken"):
        raise RuntimeError("auth JSON 缺少 accessToken/refreshToken")
    auth.setdefault("buildTime", DEFAULT_BUILD_TIME)
    return auth


def persist_auth_cache(auth: dict) -> None:
    import stat

    enriched = {**auth, "cachedAt": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"), "source": "chrome"}
    AUTH_CACHE_FILE.write_text(json.dumps(enriched, ensure_ascii=False), encoding="utf-8")
    AUTH_CACHE_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def refresh_auth_from_chrome(auth: dict, *, reason: str) -> bool:
    print(f"[token-refresh] {reason}，尝试从 Chrome 刷新...", file=sys.stderr)
    fresh = fetch_auth_from_chrome(require_fresh=True)
    if not fresh:
        if _BROWSER_TAB_STATE["login_required"]:
            print(
                "[token-refresh] 小宇宙登录态已失效：Chrome 后台页被打回登录页，"
                "自动刷新救不回，请在 Chrome 手动重新登录 podcaster.xiaoyuzhoufm.com。",
                file=sys.stderr,
            )
        else:
            print("[token-refresh] Chrome 刷新失败，继续使用现有 token。", file=sys.stderr)
        return False

    fresh.setdefault("buildTime", DEFAULT_BUILD_TIME)
    auth.clear()
    auth.update(fresh)
    try:
        persist_auth_cache(auth)
        print("[token-refresh] Token 已刷新并缓存。", file=sys.stderr)
    except Exception as exc:
        print(f"[token-refresh] 缓存写入失败 (non-fatal): {exc}", file=sys.stderr)
    return True


def load_auth_with_refresh(raw_auth_json: str | None) -> dict:
    """Load auth and force a fresh pull from Chrome before publishing.

    小宇宙 access token 的 JWT 不带 exp，只能按 iat 猜寿命；实测其真实有效期远短于一天
    （20260704E1 拿到一枚 15h 的旧 token 直接被 401 code:21 拒），任何本地 TTL 估算都会
    放行已被服务端作废的 token。因此发布前一律 require_fresh 强刷一次：打开后台 tab 让
    SPA 解冻续期，token 已新则秒返回，「tab 睡死但登录仍有效」的情况能自愈。刷新失败
    （Chrome 未登录/会话真过期）时保留已加载 token，交给下游 401 重试与失败通知兜底。
    """
    auth = load_auth(raw_auth_json)
    refresh_auth_from_chrome(auth, reason="发布前强制刷新登录态")
    return auth


def request_json_via_browser_session(
    method: str, url: str, payload: dict | None = None, *, max_attempts: int = 3
) -> dict:
    # 兜底走 Chrome Apple Events 执行 fetch。tab.execute 偶发抛 errAEEventNotHandled
    # （中文报错"信息无法识别。"，AppleScript -1708），多见于 tab 正在导航/未就绪时；
    # 浏览器内 fetch 也可能瞬时报 status=0 的网络错。这两类都是抖动，退避重试即可自愈。
    # 真实 HTTP 状态（401 等）按原样抛出，交给调用方的刷新逻辑处理，不在这里重试。
    ensure_podcaster_tab()
    cmd = [
        "osascript",
        "-l",
        "JavaScript",
        str(JXA_SCRIPT),
        "call",
        PODCAST_PAGE_URL,
        method,
        url,
    ]
    if payload is not None:
        cmd.append(json.dumps(payload, ensure_ascii=False))

    last_transient = ""
    for attempt in range(max_attempts):
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "未知错误"
            last_transient = f"浏览器会话请求失败: {method} {url} error={detail}"
            if attempt < max_attempts - 1:
                wait = (3, 8, 15)[min(attempt, 2)]
                print(f"[browser] JXA 进程级失败，{wait}s 后重试 ({attempt+1}/{max_attempts}): {detail}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(last_transient)

        raw = proc.stdout.strip() or proc.stderr.strip()
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"浏览器会话返回不是合法 JSON: {raw}") from exc

        if not response.get("ok"):
            status = response.get("status")
            body = response.get("text") or response.get("error") or ""
            # status=0 表示浏览器内 fetch 在拿到 HTTP 响应前就抛了（网络抖动），可重试；
            # 有真实状态码（401 等）则交还调用方。
            if status in (0, None) and attempt < max_attempts - 1:
                wait = (3, 8, 15)[min(attempt, 2)]
                last_transient = f"浏览器会话 fetch 瞬时失败 status={status} body={body}"
                print(f"[browser] {last_transient}，{wait}s 后重试 ({attempt+1}/{max_attempts})", file=sys.stderr)
                time.sleep(wait)
                continue
            raise BrowserSessionRequestError(method, url, status, body)

        text = response.get("text") or "{}"
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"浏览器会话返回的 body 不是合法 JSON: {text}") from exc

    raise RuntimeError(last_transient or f"浏览器会话请求失败: {method} {url}")


def request_json(method: str, url: str, auth: dict, payload: dict | None = None) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    refreshed = False
    while True:
        headers = {
            "accept": "application/json, text/plain, */*",
            "x-app-build-time": auth["buildTime"],
            "x-jike-access-token": auth["accessToken"],
            "x-jike-refresh-token": auth["refreshToken"],
            "x-pid": PODCAST_PID,
        }
        if payload is not None:
            headers["content-type"] = "application/json;charset=UTF-8"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and not refreshed and refresh_auth_from_chrome(auth, reason=f"{method} {url} 返回 401"):
                refreshed = True
                continue
            if exc.code == 401:
                try:
                    return request_json_via_browser_session(method, url, payload)
                except BrowserSessionRequestError as browser_exc:
                    if (
                        browser_exc.status == 401
                        and not refreshed
                        and refresh_auth_from_chrome(auth, reason=f"浏览器会话 {method} {url} 返回 401")
                    ):
                        refreshed = True
                        continue
                    raise
            raise RuntimeError(f"API 请求失败: {method} {url} status={exc.code} body={body}") from exc
        except urllib.error.URLError:
            try:
                return request_json_via_browser_session(method, url, payload)
            except BrowserSessionRequestError as browser_exc:
                if (
                    browser_exc.status == 401
                    and not refreshed
                    and refresh_auth_from_chrome(auth, reason=f"浏览器会话 {method} {url} 返回 401")
                ):
                    refreshed = True
                    continue
                raise

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"API 返回不是合法 JSON: {raw}") from exc


def provision_resource(auth: dict, resource_type: str) -> str:
    query = urllib.parse.urlencode(
        {
            "type": resource_type,
            "midway-project-id": "v6worU4NnWyL",
        }
    )
    response = request_json("GET", f"{BALLOON_BASE}/qiniu/provision?{query}", auth)
    token = response.get("token", "")
    if not token:
        raise RuntimeError(f"未拿到 {resource_type} 上传 token")
    return token


def upload_with_qiniu(file_path: Path, token: str, *, max_retries: int = 3) -> dict:
    cmd = [
        "curl",
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--noproxy", "*",
        "--connect-timeout", "30",
        "--max-time", "300",
        "https://upload.qiniup.com/",
        "-F",
        f"token={token}",
        "-F",
        f"file=@{str(file_path)}",
    ]
    last_err = ""
    for attempt in range(max_retries):
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
        last_err = proc.stderr.strip() or "七牛上传失败（空响应）"
        if attempt < max_retries - 1:
            wait = (5, 15, 30)[min(attempt, 2)]
            print(f"[qiniu] 上传失败，{wait}s 后重试 ({attempt+1}/{max_retries}): {last_err}")
            import time
            time.sleep(wait)
    raise RuntimeError(last_err)


def create_hosted_resource(auth: dict, title: str, file_payload: dict) -> dict:
    payload = {
        "pid": PODCAST_PID,
        "title": title,
        "file": file_payload["file"],
    }
    response = request_json("POST", f"{API_BASE}/hosted-resource/create", auth, payload)
    return response["data"]


def list_hosted_resources(auth: dict, title: str, limit: int = 20, skip: int = 0) -> dict:
    payload = {
        "pid": PODCAST_PID,
        "limit": limit,
        "skip": skip,
        "type": "AUDIO",
        "title": title,
    }
    return request_json("POST", f"{API_BASE}/hosted-resource/list", auth, payload)


def find_existing_resource(auth: dict, title: str) -> dict | None:
    response = list_hosted_resources(auth, title)
    items = response.get("data", [])
    for item in items:
        if item.get("title") == title:
            return item
    return None


def upload_cover_file(auth: dict, cover_path: Path) -> dict:
    image_token = provision_resource(auth, "IMAGE")
    image_upload = upload_with_qiniu(cover_path, image_token)
    return image_upload["file"]


def create_episode(auth: dict, payload: dict) -> dict:
    response = request_json("POST", f"{API_BASE}/episode/hosted/create-free", auth, payload)
    return response["data"]


def get_episode(auth: dict, eid: str) -> dict:
    query = urllib.parse.urlencode({"eid": eid})
    response = request_json("GET", f"{API_BASE}/episode/get?{query}", auth)
    return response.get("data", {})


def update_episode(auth: dict, payload: dict) -> dict:
    response = request_json("POST", f"{API_BASE}/episode/hosted/update", auth, payload)
    return response["data"]


def normalize_existing_eid(existing: dict) -> str | None:
    return existing.get("eid") or existing.get("episode", {}).get("eid")


def normalize_existing_image(existing: dict) -> dict | None:
    image = existing.get("image")
    if isinstance(image, dict):
        return image
    episode_image = existing.get("episode", {}).get("image")
    if isinstance(episode_image, dict):
        return episode_image
    return None


def episode_manage_url(eid: str | None) -> str | None:
    return PODCAST_PAGE_URL


def episode_edit_url(eid: str | None) -> str | None:
    return PODCAST_PAGE_URL


def build_result(
    title: str,
    publish_at: str,
    api_pub_date: str,
    resource: dict | None,
    image: dict | None,
    episode: dict | None,
    mode: str,
    warnings: list[str] | None = None,
) -> dict:
    eid = episode.get("eid") if episode else None
    audio_url = None
    if resource and isinstance(resource.get("audio"), dict):
        audio_url = resource["audio"].get("url")

    result = {
        "mode": mode,
        "title": title,
        "pub_date": publish_at,
        "api_pub_date": api_pub_date,
        "resource_id": resource.get("id") if resource else None,
        "audio_url": audio_url,
        "eid": eid,
        "episode_url": episode_manage_url(eid),
        "episode_edit_url": episode_edit_url(eid),
        "resource": resource,
        "image": image,
        "episode": episode,
        "warnings": warnings or [],
        "updated_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
    }
    return compact_dict(result)


def safe_update_episode(auth: dict, payload: dict, warnings: list[str]) -> dict:
    try:
        return update_episode(auth, payload)
    except RuntimeError as exc:
        if payload.get("image") is None:
            raise
        warnings.append(f"带封面更新失败，已回退为不带封面更新: {exc}")
        retry_payload = dict(payload)
        retry_payload.pop("image", None)
        return update_episode(auth, retry_payload)


def resolve_existing_eid(auth: dict, existing_eid: str | None, warnings: list[str]) -> str | None:
    if not existing_eid:
        return None

    try:
        episode = get_episode(auth, existing_eid)
    except RuntimeError as exc:
        warnings.append(f"旧单集 {existing_eid} 无法预读，继续按已有 eid 更新，禁止降级新建: {exc}")
        return existing_eid

    status = str(episode.get("status", "")).upper()
    if not episode.get("eid") or status in {"DELETED", "REMOVED"}:
        raise RuntimeError(f"旧单集 {existing_eid} 已不存在或已删除，按当前任务限制禁止新建")

    return existing_eid


def main() -> int:
    try:
        return _main_impl()
    finally:
        # 发布结束（无论成败）关闭本流程自动打开的小宇宙后台 tab
        close_podcaster_tab_if_created()


def _main_impl() -> int:
    parser = argparse.ArgumentParser(
        description="Upload audio/cover to XiaoYuZhou and create or update one hosted episode"
    )
    parser.add_argument("episode_dir", help="Episode folder, e.g. ./episodes/20260326E1")
    parser.add_argument("--auth-json", help="JSON string from xiaoyuzhou_api_session.jxa auth")
    parser.add_argument("--publish-at", help="Override publish time, e.g. 2026-03-27T09:30:00+08:00")
    parser.add_argument("--dry-run", action="store_true", help="Only compute inputs and planned actions")
    parser.add_argument(
        "--force-audio-upload",
        action="store_true",
        help="Upload current local audio again instead of reusing an existing hosted resource with the same title",
    )
    parser.add_argument(
        "--force-cover-upload",
        action="store_true",
        help="Upload current local cover again even if an episode image already exists",
    )
    args = parser.parse_args()

    episode_dir = Path(args.episode_dir).expanduser()
    current_step = "读取输入"
    inputs: dict = {}
    publish_at = ""

    try:
        if not episode_dir.is_dir():
            raise RuntimeError(f"目录不存在: {episode_dir}")

        inputs = read_episode_inputs(episode_dir)
        existing = load_existing_result(episode_dir)
        publish_at, schedule_warning = resolve_publish_at(existing, episode_dir, args.publish_at)
        api_pub_date = to_api_pub_date(publish_at)
        existing_eid = normalize_existing_eid(existing)
        warnings: list[str] = []
        if schedule_warning:
            warnings.append(schedule_warning)

        current_step = "发布前检查"
        ensure_publishable_cover(episode_dir, inputs)

        if args.dry_run:
            payload_plain_text = re.sub(r"<[^>]+>", " ", inputs["shownotes"])
            dry_run_result = {
                "mode": "dry_run",
                "title": inputs["title"],
                "mp3_path": str(inputs["mp3_path"]),
                "cover_path": str(inputs["cover_path"]) if inputs["cover_path"] else None,
                "shownotes_length": len(inputs["shownotes"]),
                "shownotes_source": inputs["shownotes_meta"].get("shownotes_source"),
                "shownotes_source_path": inputs["shownotes_meta"].get("source_path"),
                "shownotes_file_mtime": inputs["shownotes_meta"].get("shownotes_mtime"),
                "shownotes_timestamp_count": inputs["shownotes_meta"].get("shownotes_timestamp_count"),
                "shownotes_last_timestamp": inputs["shownotes_meta"].get("shownotes_last_timestamp"),
                "audio_duration_seconds": inputs["shownotes_meta"].get("audio_duration_seconds"),
                "shownotes_source_format": inputs["shownotes_meta"].get("source_format"),
                "shownotes_payload_format": inputs["shownotes_meta"].get("payload_format"),
                "shownotes_payload_has_bold": inputs["shownotes_meta"].get("payload_has_bold"),
                "shownotes_payload_has_bullet": inputs["shownotes_meta"].get("payload_has_bullet"),
                "shownotes_payload_block_count": inputs["shownotes_meta"].get("payload_block_count"),
                "shownotes_sample": inputs["shownotes_meta"].get("shownotes_sample"),
                "payload_shownotes_head_300": _brief_text(payload_plain_text, 300),
                "payload_shownotes_tail_300": _brief_text(payload_plain_text[-300:], 300),
                "planned_pub_date": publish_at,
                "planned_api_pub_date": api_pub_date,
                "planned_episode_action": "update" if existing_eid else "create",
                "force_audio_upload": bool(args.force_audio_upload),
                "force_cover_upload": bool(args.force_cover_upload),
                "existing_eid": existing_eid,
                "existing_resource_id": existing.get("resource_id") or existing.get("resource", {}).get("id"),
                "warnings": warnings,
            }
            print(json.dumps(compact_dict(dry_run_result), ensure_ascii=False, indent=2))
            return 0

        current_step = "获取小宇宙登录态"
        raw_auth_json = args.auth_json or os.environ.get("XIAOYUZHOU_AUTH_JSON")
        auth = load_auth_with_refresh(raw_auth_json)

        current_step = "读取已有单集状态"
        existing_eid = resolve_existing_eid(auth, existing_eid, warnings)

        current_step = "查询音频资源"
        existing_resource = existing.get("resource") if isinstance(existing.get("resource"), dict) else None
        resource = None
        if not args.force_audio_upload:
            resource = existing_resource if existing_eid and existing_resource and existing_resource.get("audioFile") else None
            if resource is None:
                resource = find_existing_resource(auth, inputs["title"])
        if resource is None:
            if existing_eid:
                raise RuntimeError("已有单集更新缺少可复用音频资源，按当前任务限制禁止重新上传音频。")
            current_step = "上传音频文件"
            media_token = provision_resource(auth, "MEDIA")
            media_upload = upload_with_qiniu(inputs["mp3_path"], media_token)
            current_step = "创建音频资源"
            resource = create_hosted_resource(auth, inputs["title"], media_upload)
            if args.force_audio_upload:
                warnings.append(
                    f"已强制上传当前目录音频 {inputs['mp3_path'].name}，未复用同标题历史资源。"
                )

        existing_image = existing.get("image") or normalize_existing_image(existing)
        image_file = existing_image
        should_upload_cover = bool(inputs["cover_path"]) and (
            args.force_cover_upload
            or image_file is None
            or (not existing_eid and (episode_dir / "cover_generation.json").exists())
        )
        if should_upload_cover:
            current_step = "上传封面"
            image_file = upload_cover_file(auth, inputs["cover_path"])

        base_payload = compact_dict(
            {
                "pid": PODCAST_PID,
                "title": inputs["title"],
                "shownotes": inputs["shownotes"],
                "audioFile": resource.get("audioFile"),
                "pubDate": api_pub_date,
                "allTrial": False,
            }
        )
        payload_plain = re.sub(r"<[^>]+>", " ", base_payload.get("shownotes", ""))
        print("[xiaoyuzhou preflight] payload shownotes summary:")
        print(
            json.dumps(
                {
                    "shownotes_source": inputs["shownotes_meta"].get("shownotes_source"),
                    "shownotes_source_path": inputs["shownotes_meta"].get("source_path"),
                    "shownotes_length": len(base_payload.get("shownotes", "")),
                    "shownotes_head_300": _brief_text(payload_plain, 300),
                    "shownotes_tail_300": _brief_text(payload_plain[-300:], 300),
                    "shownotes_timestamp_count": inputs["shownotes_meta"].get("shownotes_timestamp_count"),
                    "shownotes_last_timestamp": inputs["shownotes_meta"].get("shownotes_last_timestamp"),
                    "audio_duration_seconds": inputs["shownotes_meta"].get("audio_duration_seconds"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

        if not base_payload.get("audioFile"):
            raise RuntimeError("未拿到 audioFile，无法创建或更新单集")

        if existing_eid:
            current_step = "更新小宇宙单集"
            update_payload = dict(base_payload)
            update_payload["eid"] = existing_eid
            if not should_include_pub_date_on_update(existing, publish_at):
                update_payload.pop("pubDate", None)
                warnings.append("当前是已发布单集的文案修正，更新请求中已省略 pubDate，避免触发小宇宙的预约发布时间校验。")
            if image_file is not None and (args.force_cover_upload or image_file is not existing_image):
                update_payload["image"] = image_file
            episode = safe_update_episode(auth, update_payload, warnings)
            mode = "updated"
        else:
            current_step = "创建小宇宙单集"
            episode = create_episode(auth, base_payload)
            if image_file is not None:
                try:
                    current_step = "回填封面"
                    episode = safe_update_episode(
                        auth,
                        {**base_payload, "eid": episode["eid"], "image": image_file},
                        warnings,
                    )
                except RuntimeError as exc:
                    warnings.append(f"单集已创建，但封面补写失败: {exc}")
            mode = "created"

        result = build_result(
            title=inputs["title"],
            publish_at=publish_at,
            api_pub_date=api_pub_date,
            resource=resource,
            image=image_file,
            episode=episode,
            mode=mode,
            warnings=warnings,
        )
        output_path = episode_dir / "xiaoyuzhou_publish_result.json"
        write_json(output_path, result)
        sync_publish_metadata(episode_dir, inputs, result)
        notify_publish_success(episode_dir, inputs, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        notify_publish_failure(
            episode_dir,
            step=current_step,
            error=str(exc),
            title=inputs.get("title", "") if isinstance(inputs, dict) else "",
            publish_at=publish_at,
        )
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
