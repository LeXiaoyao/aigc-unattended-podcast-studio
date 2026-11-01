"""Public episode listing and detail routes."""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(ROOT_DIR))
from fastapi.templating import Jinja2Templates
templates = Jinja2Templates(directory=str(ROOT_DIR / "webapp" / "templates"))

router = APIRouter()


def _load_episodes_from_db(limit: int, offset: int, flow_type: str | None) -> tuple[list[dict], int]:
    try:
        from db.schema import list_episodes, count_episodes
        return list_episodes(limit=limit, offset=offset, flow_type=flow_type), count_episodes(flow_type)
    except Exception:
        return [], 0


def _load_episodes_from_csv() -> list[dict]:
    """Fallback: load episodes from CSV."""
    csv_path = ROOT_DIR / "content_records.csv"
    if not csv_path.exists():
        return []
    episodes = []
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                episode_id = row.get("期数", "")
                if not episode_id:
                    continue
                episodes.append({
                    "id": episode_id,
                    "date": row.get("日期", ""),
                    "flow_type": "dual_host",
                    "title": row.get("标题", ""),
                    "trends": json.dumps(
                        [t.strip() for t in row.get("三大趋势", "").split("\n") if t.strip()],
                        ensure_ascii=False,
                    ),
                    "audio_path": row.get("音频路径", ""),
                    "cover_path": row.get("封面路径", ""),
                    "listenhub_url": row.get("ListenHub作品链接", ""),
                    "status": "audio_ready" if row.get("音频路径") else "draft",
                    "xiaoyuzhou_eid": "",
                    "xiaoyuzhou_url": "",
                })
    except Exception:
        pass
    return list(reversed(episodes))


def _get_episodes_page(page: int, per_page: int, flow_type: str | None) -> tuple[list[dict], int]:
    offset = (page - 1) * per_page
    episodes, total = _load_episodes_from_db(per_page, offset, flow_type)
    if total == 0:
        # Fallback to CSV
        all_eps = _load_episodes_from_csv()
        if flow_type:
            all_eps = [e for e in all_eps if e.get("flow_type") == flow_type]
        total = len(all_eps)
        episodes = all_eps[offset: offset + per_page]
    return episodes, total


def _enrich_episode(ep: dict) -> dict:
    """Add computed fields for template rendering."""
    ep = dict(ep)
    # Parse trends from JSON string
    try:
        ep["trends_list"] = json.loads(ep.get("trends") or "[]")
    except Exception:
        ep["trends_list"] = []
    try:
        ep["products_list"] = json.loads(ep.get("products") or "[]")
    except Exception:
        ep["products_list"] = []
    # Cover URL
    ep["cover_url"] = f"/cover/{ep['id']}" if ep.get("cover_path") else "/static/cover_placeholder.png"
    # Audio URL
    ep["audio_url"] = f"/audio/{ep['id']}" if ep.get("audio_path") else ""
    # 小宇宙 link
    if ep.get("xiaoyuzhou_eid"):
        ep["xiaoyuzhou_link"] = f"https://www.xiaoyuzhoufm.com/episode/{ep['xiaoyuzhou_eid']}"
    else:
        ep["xiaoyuzhou_link"] = ep.get("xiaoyuzhou_url", "")
    return ep


@router.get("/episodes", response_class=HTMLResponse)
async def episode_list(
    request: Request,
    page: int = 1,
    flow_type: str | None = None,
):
    per_page = 12
    episodes, total = _get_episodes_page(page, per_page, flow_type)
    episodes = [_enrich_episode(e) for e in episodes]
    total_pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(request, "index.html", context={
        "episodes": episodes,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "current_flow": flow_type or "all",
    })


@router.get("/episodes/{episode_id}", response_class=HTMLResponse)
async def episode_detail(request: Request, episode_id: str):
    try:
        from db.schema import get_episode
        ep = get_episode(episode_id)
    except Exception:
        ep = None

    if not ep:
        # Try reading record.json directly
        record_path = ROOT_DIR / "episodes" / episode_id / "record.json"
        if record_path.exists():
            record = json.loads(record_path.read_text(encoding="utf-8"))
            publish_path = ROOT_DIR / "episodes" / episode_id / "xiaoyuzhou_publish_result.json"
            publish = {}
            if publish_path.exists():
                publish = json.loads(publish_path.read_text(encoding="utf-8"))
            ep = {
                "id": episode_id,
                "date": record.get("日期", ""),
                "flow_type": record.get("flow_type", "dual_host"),
                "title": record.get("标题", ""),
                "trends": json.dumps(
                    [t.strip() for t in record.get("三大趋势", "").split("\n") if t.strip()],
                    ensure_ascii=False,
                ),
                "products": json.dumps(
                    [p.strip() for p in record.get("产品洞察", "").split("\n") if p.strip()],
                    ensure_ascii=False,
                ),
                "audio_path": record.get("音频路径", ""),
                "cover_path": record.get("封面路径", ""),
                "listenhub_url": record.get("ListenHub作品链接", ""),
                "xiaoyuzhou_eid": publish.get("eid", ""),
                "xiaoyuzhou_url": publish.get("episode_url", ""),
                "status": "published" if publish.get("eid") else "audio_ready",
            }
        else:
            from fastapi.responses import Response
            return Response(content="Episode not found", status_code=404)

    ep = _enrich_episode(ep)

    # Load script for show notes
    script_text = ""
    script_path = ep.get("script_path", "")
    if script_path and Path(script_path).exists():
        script_text = Path(script_path).read_text(encoding="utf-8")[:2000]

    return templates.TemplateResponse(request, "episode.html", context={
        "episode": ep,
        "script_preview": script_text,
    })


@router.get("/api/episodes")
async def api_episodes(
    page: int = 1,
    per_page: int = 50,
    flow_type: str | None = None,
):
    episodes, total = _get_episodes_page(page, per_page, flow_type)
    episodes = [_enrich_episode(e) for e in episodes]
    return {"episodes": episodes, "total": total, "page": page, "per_page": per_page}
