"""FastAPI app entry point — AI podcast website + admin dashboard."""
from __future__ import annotations

import mimetypes
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT_DIR))

from webapp.deps import PODCAST_NAME, PODCAST_DESCRIPTION
from webapp.routers import episodes, feishu, schedule, pipeline
from fastapi.templating import Jinja2Templates
templates = Jinja2Templates(directory=str(ROOT_DIR / "webapp" / "templates"))

app = FastAPI(title=PODCAST_NAME, description=PODCAST_DESCRIPTION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8765", "http://127.0.0.1:8765"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "webapp" / "static")), name="static")

# Include routers
app.include_router(episodes.router)
app.include_router(schedule.router)
app.include_router(pipeline.router)
app.include_router(feishu.router)


@app.on_event("startup")
async def _start_auto_publish_loop():
    """E1/E2 每日自动发布编排器；开关在 schedule_config.json 各 flow 的 auto_publish。"""
    import asyncio

    from webapp.auto_publish import auto_publish_loop

    asyncio.create_task(auto_publish_loop())


@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/episodes")


def _episode_media_path(episode_id: str, field: str) -> Path | None:
    try:
        from db.schema import get_episode
        ep = get_episode(episode_id)
    except Exception:
        ep = None
    raw_path = (ep or {}).get(field) or ""
    if raw_path:
        path = Path(raw_path)
        if path.exists() and path.is_file():
            return path
    return None


@app.get("/cover/{episode_id}")
async def serve_cover(episode_id: str):
    """Serve episode cover image."""
    db_cover = _episode_media_path(episode_id, "cover_path")
    if db_cover:
        media_type = mimetypes.guess_type(str(db_cover))[0] or "image/jpeg"
        return FileResponse(str(db_cover), media_type=media_type)

    for episode_dir in (ROOT_DIR / "episodes" / episode_id, ROOT_DIR / episode_id):
        for name in ("cover.jpeg", "cover.jpg", "cover.png", "cover_background.png"):
            p = episode_dir / name
            if p.exists():
                media_type = "image/jpeg" if name.endswith((".jpg", ".jpeg")) else "image/png"
                return FileResponse(str(p), media_type=media_type)
    # Return placeholder
    placeholder = ROOT_DIR / "webapp" / "static" / "cover_placeholder.png"
    if placeholder.exists():
        return FileResponse(str(placeholder), media_type="image/png")
    return HTMLResponse("<svg width='400' height='400' xmlns='http://www.w3.org/2000/svg'><rect width='400' height='400' fill='#1a1a2e'/><text x='200' y='210' text-anchor='middle' fill='#6366f1' font-size='48'>🎙</text></svg>", media_type="image/svg+xml")


@app.get("/audio/{episode_id}")
async def serve_audio(episode_id: str):
    """Serve episode audio file."""
    db_audio = _episode_media_path(episode_id, "audio_path")
    if db_audio:
        media_type = mimetypes.guess_type(str(db_audio))[0] or "audio/mpeg"
        return FileResponse(str(db_audio), media_type=media_type)

    for episode_dir in (ROOT_DIR / "episodes" / episode_id, ROOT_DIR / episode_id):
        if not episode_dir.is_dir():
            continue
        for name in ("podcast_solo.mp3", "podcast.mp3"):
            p = episode_dir / name
            if p.exists():
                return FileResponse(str(p), media_type="audio/mpeg")
        for p in sorted(episode_dir.glob("*.mp3")):
            if not p.name.startswith("highlight_") and not p.name.endswith("_raw.mp3"):
                return FileResponse(str(p), media_type="audio/mpeg")
    return HTMLResponse("Audio not found", status_code=404)


@app.get("/rss.xml")
@app.get("/feed.xml")
async def serve_rss_feed(request: Request, flow: str | None = None):
    """Serve standard Podcast RSS 2.0 XML feed."""
    from rss_publisher import build_rss_xml

    base_url = os.environ.get("PODCAST_BASE_URL") or str(request.base_url).rstrip("/")
    xml_content = build_rss_xml(base_url=base_url, flow_type=flow)
    return Response(content=xml_content, media_type="application/rss+xml; charset=utf-8")


@app.get("/rss/{flow_type}.xml")
async def serve_flow_rss_feed(request: Request, flow_type: str):
    """Serve flow-specific Podcast RSS 2.0 XML feed (solo or dual_host)."""
    from rss_publisher import build_rss_xml

    normalized = "solo" if flow_type == "solo" else ("dual_host" if flow_type in ("dual", "dual_host") else None)
    base_url = os.environ.get("PODCAST_BASE_URL") or str(request.base_url).rstrip("/")
    xml_content = build_rss_xml(base_url=base_url, flow_type=normalized)
    return Response(content=xml_content, media_type="application/rss+xml; charset=utf-8")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return templates.TemplateResponse(request, "base.html", context={"error": "页面不存在"}, status_code=404)
