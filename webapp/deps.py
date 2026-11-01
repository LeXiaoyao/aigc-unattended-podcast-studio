"""Shared dependencies for the webapp."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
SCHEDULE_CONFIG_FILE = ROOT_DIR / "schedule_config.json"
LOG_DIR = ROOT_DIR / "logs"

PODCAST_NAME = os.environ.get("PODCAST_NAME", "AI观测站")
PODCAST_DESCRIPTION = os.environ.get("PODCAST_DESCRIPTION", "每日AI产业全链路观察，从芯片到应用，从趋势到判断")
PODCAST_PID = os.environ.get("XIAOYUZHOU_PODCAST_PID", "").strip()

ADMIN_PASSWORD = os.environ.get("WEBAPP_ADMIN_PASSWORD", "admin123")
COOKIE_SECRET = "podcast_local_secret_2026"
COOKIE_NAME = "podcast_admin"


def make_auth_cookie(password: str) -> str:
    return hmac.new(COOKIE_SECRET.encode(), password.encode(), hashlib.sha256).hexdigest()


def verify_admin_cookie(cookie_value: str | None) -> bool:
    if not cookie_value:
        return False
    return cookie_value == make_auth_cookie(ADMIN_PASSWORD)


def load_schedule_config() -> dict:
    default = {
        "dual_host": {
            "enabled": True,
            "days_of_week": [0, 1, 2, 3, 4, 5, 6],
            "hour": 7,
            "minute": 0,
            "publish_hour": 7,
            "publish_minute": 0,
            "publish_day_offset": 1,
            "description": "双人口播，每日生成，次日 07:00 发布，通过ListenHub合成",
        },
        "solo": {
            "enabled": True,
            "days_of_week": [0, 1, 2, 3, 4, 5, 6],
            "hour": 4,
            "minute": 0,
            "publish_hour": 9,
            "publish_minute": 30,
            "description": "单人口播，每日生成待确认，MiniMax普通话新闻女主播TTS合成",
            "daily_runs": [{"hour": 4, "minute": 0}],
        },
    }
    if SCHEDULE_CONFIG_FILE.exists():
        try:
            return json.loads(SCHEDULE_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_schedule_config(config: dict) -> None:
    SCHEDULE_CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
