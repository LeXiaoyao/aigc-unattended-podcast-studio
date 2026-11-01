#!/usr/bin/env python3
"""Manage XiaoYuZhou auth cache using tokens read from the browser session."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from xiaoyuzhou_publish_pipeline import (
    _BROWSER_TAB_STATE,
    AUTH_CACHE_FILE,
    decode_jwt_payload,
    fetch_auth_from_chrome,
    load_auth,
    login_block_active,
)
from mobile_notify import notify

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

def read_cached_auth() -> dict | None:
    if not AUTH_CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(AUTH_CACHE_FILE.read_text(encoding="utf-8"))
        return load_auth(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return None


def token_expiry_iso(token: str) -> str | None:
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if not exp:
        return None
    return datetime.fromtimestamp(exp, tz=BEIJING_TZ).isoformat(timespec="seconds")


def token_remaining_seconds(token: str) -> int | None:
    payload = decode_jwt_payload(token)
    exp = payload.get("exp")
    if not exp:
        return None
    return int(exp - datetime.now(tz=BEIJING_TZ).timestamp())


def save_auth(auth: dict, *, source: str) -> dict:
    enriched = {
        **auth,
        "source": source,
        "cachedAt": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "accessTokenExp": token_expiry_iso(auth.get("accessToken", "")),
        "refreshTokenExp": token_expiry_iso(auth.get("refreshToken", "")),
    }
    AUTH_CACHE_FILE.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    AUTH_CACHE_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return enriched


def cache_is_usable(auth: dict, *, min_ttl_seconds: int) -> bool:
    remaining = token_remaining_seconds(auth.get("accessToken", ""))
    if remaining is not None:
        return remaining > min_ttl_seconds
    # Token has no exp (encrypted format). Fall back to cachedAt-based TTL.
    cached_at_str = auth.get("cachedAt")
    if cached_at_str:
        try:
            cached_at = datetime.fromisoformat(cached_at_str)
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=BEIJING_TZ)
            age_seconds = (datetime.now(BEIJING_TZ) - cached_at).total_seconds()
            return age_seconds < 7200  # treat as stale after 2 hours
        except Exception:
            pass
    return True


def login_required_detected() -> bool:
    return bool(_BROWSER_TAB_STATE.get("login_required")) or login_block_active()


def ensure_auth(*, min_ttl_seconds: int, force_refresh: bool, notify_on_failure: bool) -> dict:
    warn_if_session_expiring(notify_on_warn=notify_on_failure)
    cached = read_cached_auth()

    if cached and not force_refresh and cache_is_usable(cached, min_ttl_seconds=min_ttl_seconds):
        return cached

    fresh = fetch_auth_from_chrome(require_fresh=True)
    if fresh:
        validated = load_auth(json.dumps(fresh, ensure_ascii=False))
        save_auth(validated, source="chrome")
        return validated

    # 会话失效和"刷新抖动"要分开报：前者缓存 token 也已被服务端作废（发布必 401），
    # 只能人工重登；后者回退缓存还能继续跑。之前两种都报"刷新异常"，看不出要不要动手。
    if login_required_detected():
        message = (
            "小宇宙登录态已失效：Chrome 后台页被重定向到登录页，缓存 token 也会被 401 拒绝。"
            "请在 Chrome 重新登录 podcaster.xiaoyuzhoufm.com，之后自动流程即恢复。"
        )
        print(message, file=sys.stderr)
        if notify_on_failure:
            notify("小宇宙登录态已失效 · 需人工重登", message)
        raise RuntimeError(message)

    if cached:
        remaining = token_remaining_seconds(cached.get("accessToken", ""))
        if remaining is None or remaining > 0:
            warning = "Chrome token 刷新失败，已回退到当前仍有效的缓存 token。"
            print(warning, file=sys.stderr)
            if notify_on_failure:
                notify("小宇宙 Token 刷新异常", warning)
            return cached

    message = "未能从 Chrome 会话刷新小宇宙 token，且本地缓存已不可用。"
    if notify_on_failure:
        notify("小宇宙 Token 已失效", message)
    raise RuntimeError(message)


CHROME_COOKIE_DB = (
    Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
)
SESSION_WARN_FILE = AUTH_CACHE_FILE.parent / ".xiaoyuzhou_session_warned.json"
SESSION_WARN_DAYS = 7


def chrome_session_deadline() -> datetime | None:
    """读 Chrome cookie 里 x-jike-refresh-token 的到期时间。

    小宇宙的会话有硬上限（实测 60 天，续 token 不会延长），到期当天所有自动流程一起挂，
    且只能人工重登。提前预警才不会又是"发布挂了才发现"。只读到期时间，不碰 cookie 值。
    """
    if not CHROME_COOKIE_DB.exists():
        return None
    import shutil
    import sqlite3
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "Cookies"
            shutil.copy2(CHROME_COOKIE_DB, snapshot)  # Chrome 持锁，必须先快照
            conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT MAX(expires_utc) FROM cookies "
                    "WHERE host_key LIKE '%xiaoyuzhoufm.com' AND name = 'x-jike-refresh-token'"
                ).fetchone()
            finally:
                conn.close()
        if not row or not row[0]:
            return None
        # Chrome 用 1601-01-01 起算的微秒
        return datetime.fromtimestamp(row[0] / 1_000_000 - 11_644_473_600, tz=BEIJING_TZ)
    except Exception:
        return None


def warn_if_session_expiring(*, notify_on_warn: bool = True) -> int | None:
    deadline = chrome_session_deadline()
    if deadline is None:
        return None
    days_left = (deadline - datetime.now(BEIJING_TZ)).days
    if days_left > SESSION_WARN_DAYS:
        SESSION_WARN_FILE.unlink(missing_ok=True)
        return days_left

    today = datetime.now(BEIJING_TZ).date().isoformat()
    try:
        warned_on = json.loads(SESSION_WARN_FILE.read_text(encoding="utf-8")).get("date")
    except Exception:
        warned_on = None
    if notify_on_warn and warned_on != today:  # 一天刷 6 次，提醒只发一次
        notify(
            "小宇宙登录态即将到期",
            f"Chrome 会话还剩 {days_left} 天（{deadline.date()} 到期），"
            "到期后 E1/E2 会一起发布失败。请找时间在 Chrome 重新登录一次 podcaster 后台。",
        )
        SESSION_WARN_FILE.write_text(
            json.dumps({"date": today, "deadline": deadline.isoformat()}, ensure_ascii=False),
            encoding="utf-8",
        )
    return days_left


def status_payload() -> dict:
    cached = read_cached_auth()
    if not cached:
        return {"ok": False, "cache_exists": False}
    return {
        "ok": True,
        "cache_exists": True,
        "login_required": login_block_active(),
        "chrome_session_days_left": warn_if_session_expiring(notify_on_warn=False),
        "cached_at": cached.get("cachedAt"),
        "source": cached.get("source", "cache"),
        "access_token_exp": token_expiry_iso(cached.get("accessToken", "")),
        "refresh_token_exp": token_expiry_iso(cached.get("refreshToken", "")),
        "access_token_remaining_seconds": token_remaining_seconds(cached.get("accessToken", "")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh cached XiaoYuZhou auth tokens from Chrome")
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="Return usable auth JSON, refreshing if needed")
    get_parser.add_argument("--min-ttl-seconds", type=int, default=3600)
    get_parser.add_argument("--force-refresh", action="store_true")
    get_parser.add_argument("--notify-on-failure", action="store_true")

    refresh_parser = subparsers.add_parser("refresh", help="Force refresh auth cache from Chrome")
    refresh_parser.add_argument("--notify-on-failure", action="store_true")

    subparsers.add_parser("status", help="Show cached auth expiry status")
    subparsers.add_parser("healthcheck", help="Read hosted resources to verify current authentication without publishing")

    args = parser.parse_args()

    if args.command == "healthcheck":
        from xiaoyuzhou_publish_pipeline import list_hosted_resources

        auth = ensure_auth(min_ttl_seconds=3600, force_refresh=False, notify_on_failure=False)
        response = list_hosted_resources(auth, "", limit=1)
        if response.get("success") is False or not isinstance(response.get("data"), list):
            raise RuntimeError("小宇宙资料库读取未通过，请检查登录态或接口返回")
        print(json.dumps({"ok": True, "authenticated_read": True}))
        return 0

    if args.command == "status":
        print(json.dumps(status_payload(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "refresh":
        auth = ensure_auth(
            min_ttl_seconds=24 * 60 * 60,
            force_refresh=True,
            notify_on_failure=args.notify_on_failure,
        )
        warn_if_session_expiring()
        print(json.dumps(auth, ensure_ascii=False))
        return 0

    auth = ensure_auth(
        min_ttl_seconds=args.min_ttl_seconds,
        force_refresh=args.force_refresh,
        notify_on_failure=args.notify_on_failure,
    )
    print(json.dumps(auth, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise
    finally:
        # token 刷新若自动开过小宇宙后台 tab，结束后关闭
        from xiaoyuzhou_publish_pipeline import close_podcaster_tab_if_created

        close_podcaster_tab_if_created()
