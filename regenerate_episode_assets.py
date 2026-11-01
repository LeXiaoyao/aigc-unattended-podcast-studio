#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from ai_news_pipeline import ROOT_DIR, now_local, read_json, write_json, regenerate_cover_for_episode
from audio_mixer import generate_highlight_clip, mix_podcast_from_chunks
from minimax_tts import minimax_tts_chunks
from solo_pipeline import _concat_wavs_to_mp3, _render_solo_shownotes, _split_script_into_chunks

ENV_FILE = ROOT_DIR / ".env.listenhub"


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and value:
            import os

            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def strip_markdown(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_bullet_text(text: str) -> str:
    cleaned = strip_markdown(text)
    cleaned = re.sub(r"^[•\-]\s*", "", cleaned)
    cleaned = re.sub(r"^\d+[.)、]\s*", "", cleaned)
    return cleaned.strip()


def extract_shownote_bullets(shownotes_text: str, limit: int = 5) -> list[str]:
    bullets: list[str] = []
    for raw_line in shownotes_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in {"本期要点", "时间线", "参考资料与延伸阅读"}:
            continue
        if re.match(r"^\d{2}:\d{2}\s+", line):
            continue
        if line.startswith("- "):
            continue
        cleaned = clean_bullet_text(line)
        if not cleaned:
            continue
        bullets.append(cleaned)
        if len(bullets) >= limit:
            break
    return bullets


def extract_script_paragraphs(script_text: str, limit: int = 4) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", script_text) if part.strip()]
    selected: list[str] = []
    for paragraph in paragraphs:
        if paragraph.startswith("早上好") or paragraph.startswith("嗨，早上好"):
            continue
        if "以上就是今天的AI产业观察" in paragraph:
            continue
        selected.append(strip_markdown(paragraph))
        if len(selected) >= limit:
            break
    return selected


def build_research_markdown(title: str, script_text: str, shownotes_text: str, episode_dir: Path) -> str:
    raw_date = episode_dir.name[:8]
    if re.match(r"^\d{8}$", raw_date):
        date_label = f"{raw_date[:4]} 年 {raw_date[4:6]} 月 {raw_date[6:8]} 日"
    else:
        date_label = now_local().strftime("%Y 年 %m 月 %d 日")

    bullets = extract_shownote_bullets(shownotes_text, limit=5)
    paragraphs = extract_script_paragraphs(script_text, limit=4)

    lines = [f"# {title}", "", f"日期：{date_label}", "", "## 今日结论", ""]
    if bullets:
        for idx, bullet in enumerate(bullets[:3], start=1):
            lines.append(f"{idx}. {bullet}")
    else:
        lines.extend(
            [
                "1. AI 产业热点继续围绕模型、融资与开发框架展开。",
                "2. 企业级智能体和开源模型仍是当日内容的主线。",
                "3. 开发者社区的新项目正在成为补充选题的重要来源。",
            ]
        )

    lines.extend(["", "## 三大趋势", ""])
    trend_items = bullets[:3] if bullets else paragraphs[:3]
    for idx, item in enumerate(trend_items, start=1):
        lines.extend(
            [
                f"### 趋势{idx}：{item}",
                "",
                "发生了什么：",
                item,
                "",
                "为什么重要：",
                item,
                "",
            ]
        )

    if paragraphs:
        lines.extend(["## 深度研究", ""])
        for paragraph in paragraphs:
            lines.append(paragraph)
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def sync_record_metadata(episode_dir: Path, *, title: str, voice_id: str | None = None) -> dict:
    record_path = episode_dir / "record.json"
    record = read_json(record_path) if record_path.exists() else {}
    record["标题"] = title
    record["口播稿路径"] = str(episode_dir / "podcast_script.txt")
    record["Shownotes路径"] = str(episode_dir / "shownotes.txt")
    record["研究文档路径"] = str(episode_dir / "research.md")
    if voice_id:
        record["TTS音色"] = voice_id
    write_json(record_path, record)
    return record


def backup_if_missing(source: Path, backup_name: str) -> None:
    if not source.exists():
        return
    backup_path = source.with_name(backup_name)
    if not backup_path.exists():
        shutil.copy2(source, backup_path)


def main() -> int:
    load_env()

    parser = argparse.ArgumentParser(description="根据现有 title/script/shownotes 重新生成单集封面与音频资产")
    parser.add_argument("--episode", required=True, help="例如 20260406E1")
    parser.add_argument("--cover-provider", default="gemini", choices=["gemini"], help="封面提供方")
    parser.add_argument("--voice-id", help="MiniMax 声音 ID")
    args = parser.parse_args()

    episode_dir = ROOT_DIR / args.episode
    if not episode_dir.is_dir():
        raise RuntimeError(f"目录不存在: {episode_dir}")

    title_path = episode_dir / "title.txt"
    script_path = episode_dir / "podcast_script.txt"
    shownotes_path = episode_dir / "shownotes.txt"
    if not title_path.exists() or not script_path.exists() or not shownotes_path.exists():
        raise RuntimeError("缺少 title.txt / podcast_script.txt / shownotes.txt，无法重生成资产")

    title = title_path.read_text(encoding="utf-8").strip()
    script_text = script_path.read_text(encoding="utf-8").strip()
    shownotes_text = shownotes_path.read_text(encoding="utf-8").strip()
    shownote_bullets = extract_shownote_bullets(shownotes_text)
    if not title or not script_text:
        raise RuntimeError("标题或口播稿为空，无法重生成资产")

    research_path = episode_dir / "research.md"
    backup_if_missing(research_path, "research_before_asset_regen.md")
    research_path.write_text(
        build_research_markdown(title, script_text, shownotes_text, episode_dir),
        encoding="utf-8",
    )

    sync_record_metadata(episode_dir, title=title)

    cover_meta = regenerate_cover_for_episode(episode_dir, provider=args.cover_provider)
    if not cover_meta.get("cover_path"):
        raise RuntimeError(f"封面生成失败: {cover_meta.get('error', 'unknown error')}")

    voice_raw_path = episode_dir / "podcast_voice_raw.mp3"
    podcast_path = episode_dir / "podcast_solo.mp3"
    highlight_path = episode_dir / "highlight_15s.mp3"
    chunks_dir = episode_dir / "tts_chunks"
    backup_if_missing(voice_raw_path, "podcast_voice_raw_before_regen.mp3")
    backup_if_missing(podcast_path, "podcast_solo_before_regen.mp3")
    backup_if_missing(highlight_path, "highlight_15s_before_regen.mp3")
    backup_if_missing(shownotes_path, "shownotes_before_regen.txt")

    script_chunks = _split_script_into_chunks(script_text)
    chunk_texts = [chunk["text"] for chunk in script_chunks]
    chunk_labels = [chunk["label"] for chunk in script_chunks]

    chunk_wav_paths, selected_voice_id = minimax_tts_chunks(
        chunk_texts,
        chunks_dir,
        voice_id=args.voice_id or None,
        rotation_key=args.episode,
    )
    _concat_wavs_to_mp3(chunk_wav_paths, voice_raw_path)
    podcast_path, timeline = mix_podcast_from_chunks(
        chunk_wav_paths,
        chunk_labels,
        podcast_path,
    )
    generate_highlight_clip(podcast_path, highlight_path)
    if shownote_bullets:
        shownotes_path.write_text(
            _render_solo_shownotes(shownote_bullets, timeline),
            encoding="utf-8",
        )

    record = sync_record_metadata(episode_dir, title=title, voice_id=selected_voice_id)
    record["音频路径"] = str(podcast_path)
    record["封面路径"] = str(episode_dir / "cover.jpeg")
    record["发布状态"] = "pending_republish"
    write_json(episode_dir / "record.json", record)

    print(
        json.dumps(
            {
                "episode": args.episode,
                "title": title,
                "cover_path": str(episode_dir / "cover.jpeg"),
                "voice_raw_path": str(voice_raw_path),
                "audio_path": str(podcast_path),
                "highlight_path": str(highlight_path),
                "chunks_dir": str(chunks_dir),
                "timeline_count": len(timeline),
                "voice_id": selected_voice_id,
                "status": "assets_regenerated",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
