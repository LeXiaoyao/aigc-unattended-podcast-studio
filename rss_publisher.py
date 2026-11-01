#!/usr/bin/env python3
"""Standard Podcast RSS 2.0 (iTunes / Podcasting 2.0 compliant) feed generator.

Supports both:
1. Standalone CLI generation of static RSS XML (`rss.xml` / `episodes/feed.xml`) for static hosting / CDN / S3 / R2.
2. Dynamic FastAPI serving at `/rss.xml`, `/feed.xml`, `/rss/solo.xml`, and `/rss/dual_host.xml` via `webapp/main.py`.

Usage:
  python3 rss_publisher.py --output rss.xml --base-url https://podcast.example.com
  python3 rss_publisher.py --flow solo --output rss_solo.xml
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent)
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
EPISODE_ID_RE = re.compile(r"^(\d{8})E(\d+)$")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_episode_mp3(episode_dir: Path, record: dict[str, Any]) -> Path | None:
    preferred = str(record.get("音频路径") or "").strip()
    if preferred:
        p = Path(preferred).expanduser()
        if p.is_file():
            return p
    mp3s = sorted(
        (
            p
            for p in episode_dir.glob("*.mp3")
            if p.is_file()
            and not p.name.startswith("highlight_")
            and not p.name.endswith("_raw.mp3")
        ),
        key=lambda p: (p.name != "podcast_solo.mp3", -p.stat().st_mtime),
    )
    return mp3s[0] if mp3s else None


def _detect_flow_type(episode_dir: Path, record: dict[str, Any]) -> str:
    ft_file = episode_dir / "flow_type.txt"
    if ft_file.is_file():
        val = ft_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
        if val in ("solo", "dual_host"):
            return val
    rec_ft = str(record.get("flow_type") or record.get("类型") or "").strip().lower()
    if "solo" in rec_ft or "单人" in rec_ft:
        return "solo"
    if "dual" in rec_ft or "双人" in rec_ft or (episode_dir / "listenhub_create_result.json").exists():
        return "dual_host"
    if (episode_dir / "podcast_solo.mp3").exists():
        return "solo"
    return "dual_host"


def _probe_duration_seconds(mp3_path: Path, record: dict[str, Any]) -> int:
    raw_dur = record.get("时长秒数") or record.get("duration_seconds")
    if isinstance(raw_dur, (int, float)) and raw_dur > 0:
        return int(raw_dur)
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(mp3_path),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return max(1, int(float(proc.stdout.strip())))
    except Exception:
        pass
    # Rough estimate from file size (128kbps ~ 16KB/s)
    try:
        return max(1, int(mp3_path.stat().st_size / 16000))
    except Exception:
        return 600


def _format_itunes_duration(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _resolve_pub_datetime(episode_dir: Path, record: dict[str, Any], mp3_path: Path) -> dt.datetime:
    for key in ("发布时间", "published_at", "创建时间", "created_at"):
        val = str(record.get(key) or "").strip()
        if val:
            try:
                parsed = dt.datetime.fromisoformat(val.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=BEIJING_TZ)
                return parsed
            except Exception:
                pass
    m = EPISODE_ID_RE.match(episode_dir.name)
    if m:
        try:
            day = dt.datetime.strptime(m.group(1), "%Y%m%d")
            seq = int(m.group(2))
            hour = 7 if seq == 1 else 9
            return day.replace(hour=hour, minute=0, second=0, tzinfo=BEIJING_TZ)
        except Exception:
            pass
    return dt.datetime.fromtimestamp(mp3_path.stat().st_mtime, tz=BEIJING_TZ)


def collect_episodes(
    root_dir: Path = ROOT_DIR,
    flow_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    candidates: dict[str, Path] = {}
    for search_root in (root_dir, root_dir / "episodes"):
        if not search_root.is_dir():
            continue
        for d in search_root.iterdir():
            if d.is_dir() and EPISODE_ID_RE.match(d.name):
                candidates[d.name] = d

    items: list[dict[str, Any]] = []
    for ep_id in sorted(candidates.keys(), reverse=True):
        ep_dir = candidates[ep_id]
        record = _load_json(ep_dir / "record.json")
        mp3_path = _find_episode_mp3(ep_dir, record)
        if not mp3_path:
            continue
        ep_flow = _detect_flow_type(ep_dir, record)
        if flow_type and flow_type not in ("all", "") and ep_flow != flow_type:
            continue

        title_path = ep_dir / "title.txt"
        title = (
            (title_path.read_text(encoding="utf-8", errors="ignore").strip() if title_path.is_file() else "")
            or str(record.get("标题") or record.get("ListenHub标题") or "").strip()
            or f"AI观测站 {ep_id}"
        )

        shownotes_path = ep_dir / "shownotes.txt"
        description = (
            (shownotes_path.read_text(encoding="utf-8", errors="ignore").strip() if shownotes_path.is_file() else "")
            or str(record.get("shownotes") or record.get("摘要") or "").strip()
            or title
        )

        pub_dt = _resolve_pub_datetime(ep_dir, record, mp3_path)
        size_bytes = mp3_path.stat().st_size
        duration_sec = _probe_duration_seconds(mp3_path, record)

        items.append(
            {
                "episode_id": ep_id,
                "flow_type": ep_flow,
                "title": title,
                "description": description,
                "pub_datetime": pub_dt,
                "pub_rfc2822": email.utils.format_datetime(pub_dt),
                "audio_size": size_bytes,
                "duration_seconds": duration_sec,
                "duration_itunes": _format_itunes_duration(duration_sec),
                "mp3_path": mp3_path,
            }
        )
        if len(items) >= limit:
            break

    return items


def build_rss_xml(
    base_url: str | None = None,
    flow_type: str | None = None,
    limit: int = 50,
    root_dir: Path = ROOT_DIR,
) -> str:
    resolved_base = (
        base_url
        or os.environ.get("PODCAST_BASE_URL")
        or "http://localhost:8765"
    ).rstrip("/")
    podcast_title = os.environ.get("PODCAST_NAME", "AI观测站")
    if flow_type == "solo":
        podcast_title = f"{podcast_title} · 单口日报"
    elif flow_type == "dual_host":
        podcast_title = f"{podcast_title} · 双人深聊"

    podcast_desc = os.environ.get(
        "PODCAST_DESCRIPTION",
        "每日AI产业全链路观察，从芯片到应用，从趋势到判断（AIGC 无人值守播客工作流生成）",
    )
    podcast_author = os.environ.get("PODCAST_AUTHOR", podcast_title)
    podcast_language = os.environ.get("PODCAST_LANGUAGE", "zh-cn")
    podcast_category = os.environ.get("PODCAST_CATEGORY", "Technology")

    episodes = collect_episodes(root_dir=root_dir, flow_type=flow_type, limit=limit)
    now_rfc2822 = email.utils.format_datetime(dt.datetime.now(tz=BEIJING_TZ))
    feed_path = f"/rss/{flow_type}.xml" if flow_type in ("solo", "dual_host") else "/rss.xml"
    channel_image = f"{resolved_base}/static/cover_placeholder.svg"
    if episodes:
        channel_image = f"{resolved_base}/cover/{episodes[0]['episode_id']}"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{html.escape(podcast_title)}</title>",
        f"    <link>{html.escape(resolved_base)}</link>",
        f'    <atom:link href="{html.escape(resolved_base + feed_path)}" rel="self" type="application/rss+xml" />',
        f"    <description>{html.escape(podcast_desc)}</description>",
        f"    <language>{html.escape(podcast_language)}</language>",
        f"    <lastBuildDate>{now_rfc2822}</lastBuildDate>",
        f"    <itunes:author>{html.escape(podcast_author)}</itunes:author>",
        f"    <itunes:summary>{html.escape(podcast_desc)}</itunes:summary>",
        "    <itunes:explicit>false</itunes:explicit>",
        f'    <itunes:image href="{html.escape(channel_image)}" />',
        f'    <itunes:category text="{html.escape(podcast_category)}" />',
    ]

    for ep in episodes:
        ep_id = ep["episode_id"]
        audio_url = f"{resolved_base}/audio/{ep_id}"
        cover_url = f"{resolved_base}/cover/{ep_id}"
        ep_link = f"{resolved_base}/episodes/{ep_id}"
        desc_text = ep["description"]
        desc_html = "<br/>".join(html.escape(line) for line in desc_text.splitlines())

        lines.extend(
            [
                "    <item>",
                f"      <title>{html.escape(ep['title'])}</title>",
                f"      <link>{html.escape(ep_link)}</link>",
                f'      <guid isPermaLink="false">{html.escape(ep_id)}</guid>',
                f"      <pubDate>{ep['pub_rfc2822']}</pubDate>",
                f"      <description>{html.escape(desc_text)}</description>",
                f"      <content:encoded><![CDATA[{desc_html}]]></content:encoded>",
                f'      <enclosure url="{html.escape(audio_url)}" length="{ep["audio_size"]}" type="audio/mpeg" />',
                f'      <itunes:image href="{html.escape(cover_url)}" />',
                f"      <itunes:duration>{ep['duration_itunes']}</itunes:duration>",
                f"      <itunes:episodeType>full</itunes:episodeType>",
                "    </item>",
            ]
        )

    lines.extend(["  </channel>", "</rss>", ""])
    return "\n".join(lines)


def write_rss_file(
    output_path: Path,
    base_url: str | None = None,
    flow_type: str | None = None,
    limit: int = 50,
) -> Path:
    xml_text = build_rss_xml(base_url=base_url, flow_type=flow_type, limit=limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(xml_text, encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Podcast RSS 2.0 XML feed from local episodes.")
    parser.add_argument("--base-url", default=None, help="Public base URL (default: PODCAST_BASE_URL or http://localhost:8765)")
    parser.add_argument("--flow", choices=["all", "solo", "dual_host"], default="all", help="Filter by flow type")
    parser.add_argument("--limit", type=int, default=50, help="Max episodes in feed")
    parser.add_argument("--output", default=str(ROOT_DIR / "rss.xml"), help="Output RSS XML file path")
    args = parser.parse_args()

    out = write_rss_file(
        output_path=Path(args.output).expanduser(),
        base_url=args.base_url,
        flow_type=None if args.flow == "all" else args.flow,
        limit=args.limit,
    )
    episodes = collect_episodes(flow_type=None if args.flow == "all" else args.flow, limit=args.limit)
    print(f"[rss_publisher] Wrote {len(episodes)} episode(s) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
