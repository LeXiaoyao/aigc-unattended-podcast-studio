"""Admin schedule management routes."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(ROOT_DIR))
from fastapi.templating import Jinja2Templates
templates = Jinja2Templates(directory=str(ROOT_DIR / "webapp" / "templates"))

router = APIRouter()


def _is_admin(podcast_admin: str | None = None) -> bool:
    from webapp.deps import verify_admin_cookie
    return verify_admin_cookie(podcast_admin)


@router.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return RedirectResponse("/admin/login")
    return RedirectResponse("/admin/dashboard")


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", context={"error": None})


@router.post("/admin/login")
async def login(request: Request):
    from fastapi.responses import Response
    from webapp.deps import ADMIN_PASSWORD, make_auth_cookie
    form = await request.form()
    password = form.get("password", "")
    if password == ADMIN_PASSWORD:
        resp = RedirectResponse("/admin/dashboard", status_code=303)
        resp.set_cookie(
            "podcast_admin",
            make_auth_cookie(password),
            httponly=True,
            max_age=60 * 60 * 24 * 30,  # 30 days
            samesite="lax",
        )
        return resp
    return templates.TemplateResponse(request, "login.html", context={"error": "密码错误"})


@router.get("/admin/logout")
async def logout():
    resp = RedirectResponse("/episodes")
    resp.delete_cookie("podcast_admin")
    return resp


@router.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return RedirectResponse("/admin/login")
    from webapp.deps import load_schedule_config
    config = load_schedule_config()
    return templates.TemplateResponse(request, "dashboard.html", context={
        "config": config,
        "day_names": ["一", "二", "三", "四", "五", "六", "日"],
    })


@router.get("/admin/schedule")
async def get_schedule(podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    from webapp.deps import load_schedule_config
    return load_schedule_config()


@router.post("/admin/schedule")
async def update_schedule(request: Request, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    from webapp.deps import load_schedule_config, save_schedule_config
    config = load_schedule_config()

    ALLOWED_KEYS = {"enabled", "days_of_week", "hour", "minute", "publish_hour", "publish_minute", "publish_day_offset", "daily_runs", "date_overrides"}
    for flow_type in ("dual_host", "solo"):
        if flow_type in body:
            update = body[flow_type]
            if flow_type not in config:
                config[flow_type] = {}
            config[flow_type].update({
                k: v for k, v in update.items()
                if k in ALLOWED_KEYS
            })

    save_schedule_config(config)

    # Rewrite launchd plists
    try:
        from webapp.schedule_manager import update_dual_host_schedule, update_solo_schedule
        dh = config.get("dual_host", {})
        solo = config.get("solo", {})
        update_dual_host_schedule(
            enabled=dh.get("enabled", True),
            days_of_week=dh.get("days_of_week", list(range(7))),
            hour=dh.get("hour", 7),
            minute=dh.get("minute", 0),
        )
        update_solo_schedule(
            enabled=solo.get("enabled", True),
            days_of_week=solo.get("days_of_week", list(range(7))),
            hour=solo.get("hour", 4),
            minute=solo.get("minute", 0),
            daily_runs=solo.get("daily_runs"),
        )
    except Exception as exc:
        return JSONResponse({"ok": True, "warning": f"plist 更新失败: {exc}"})

    return JSONResponse({"ok": True, "config": config})


@router.post("/admin/schedule/date-override")
async def set_date_override(request: Request, podcast_admin: str | None = Cookie(default=None)):
    """Set or remove a schedule override for a specific date.

    Body: {"date": "2026-04-05", "skip": true}
    or:   {"date": "2026-04-10", "runs": [{"hour": 4, "minute": 0}, {"hour": 12, "minute": 0}]}
    or:   {"date": "2026-04-05", "remove": true}  — removes the override
    """
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    date_str = body.get("date", "").strip()
    if not date_str:
        return JSONResponse({"error": "date is required"}, status_code=400)

    from webapp.deps import load_schedule_config, save_schedule_config
    config = load_schedule_config()
    solo = config.setdefault("solo", {})
    overrides = solo.setdefault("date_overrides", {})

    if body.get("remove"):
        overrides.pop(date_str, None)
    elif body.get("skip"):
        overrides[date_str] = {"skip": True}
    elif "runs" in body:
        overrides[date_str] = {"runs": body["runs"]}
    else:
        return JSONResponse({"error": "must provide skip, runs, or remove"}, status_code=400)

    save_schedule_config(config)
    return JSONResponse({"ok": True, "date": date_str, "overrides": overrides})
