#!/usr/bin/env python3
"""SQLite database schema and access layer for AI podcast system."""
from __future__ import annotations

import json
import os
import sqlite3
import datetime
from contextlib import closing
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
DB_PATH = ROOT_DIR / "db" / "podcast.db"

_initialized = False


def get_db() -> sqlite3.Connection:
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    global _initialized
    if _initialized:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            flow_type TEXT NOT NULL DEFAULT 'dual_host',
            title TEXT,
            trends TEXT,
            products TEXT,
            script_path TEXT,
            audio_path TEXT,
            cover_path TEXT,
            listenhub_url TEXT,
            listenhub_title TEXT,
            xiaoyuzhou_eid TEXT,
            xiaoyuzhou_pub_date TEXT,
            xiaoyuzhou_url TEXT,
            status TEXT DEFAULT 'draft',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS schedule_config (
            flow_type TEXT PRIMARY KEY,
            enabled INTEGER DEFAULT 1,
            days_of_week TEXT DEFAULT '[0,1,2,3,4,5,6]',
            hour INTEGER DEFAULT 5,
            minute INTEGER DEFAULT 5,
            publish_hour INTEGER DEFAULT 9,
            publish_minute INTEGER DEFAULT 30,
            last_triggered TEXT,
            updated_at TEXT
        );
    """)
    # Insert defaults if missing
    for flow_type, days, hour, minute in [
        ("dual_host", "[4]", 5, 5),
        ("solo", "[0,1,2,3,4,5,6]", 4, 0),
    ]:
        conn.execute(
            "INSERT OR IGNORE INTO schedule_config (flow_type, days_of_week, hour, minute) VALUES (?,?,?,?)",
            (flow_type, days, hour, minute),
        )
    conn.commit()
    conn.close()
    _initialized = True


def upsert_episode(row: dict) -> None:
    init_db()
    now = datetime.datetime.now().isoformat()
    row.setdefault("created_at", now)
    row["updated_at"] = now
    cols = [
        "id", "date", "flow_type", "title", "trends", "products",
        "script_path", "audio_path", "cover_path", "listenhub_url",
        "listenhub_title", "xiaoyuzhou_eid", "xiaoyuzhou_pub_date",
        "xiaoyuzhou_url", "status", "created_at", "updated_at",
    ]
    keys = [c for c in cols if c in row]
    placeholders = ", ".join("?" for _ in keys)
    values = [row[k] for k in keys]
    sql = f"INSERT OR REPLACE INTO episodes ({', '.join(keys)}) VALUES ({placeholders})"
    with closing(get_db()) as conn:
        conn.execute(sql, values)
        conn.commit()


def get_episode(episode_id: str) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        return dict(row) if row else None


def list_episodes(limit: int = 50, offset: int = 0, flow_type: str | None = None) -> list[dict]:
    with closing(get_db()) as conn:
        if flow_type:
            rows = conn.execute(
                "SELECT * FROM episodes WHERE flow_type = ? ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
                (flow_type, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM episodes ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]


def count_episodes(flow_type: str | None = None) -> int:
    with closing(get_db()) as conn:
        if flow_type:
            return conn.execute("SELECT COUNT(*) FROM episodes WHERE flow_type = ?", (flow_type,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]


def get_schedule_config(flow_type: str) -> dict | None:
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM schedule_config WHERE flow_type = ?", (flow_type,)).fetchone()
        return dict(row) if row else None


def update_schedule_config(flow_type: str, **kwargs) -> None:
    init_db()
    kwargs["updated_at"] = datetime.datetime.now().isoformat()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [flow_type]
    with closing(get_db()) as conn:
        conn.execute(f"UPDATE schedule_config SET {sets} WHERE flow_type = ?", values)
        conn.commit()


def should_run_today(flow_type: str) -> bool:
    """Check if flow_type is scheduled for today (reads from JSON config first, falls back to DB).

    Supports date_overrides: per-date skip or custom run times.
    """
    import os
    # Prefer JSON config file
    config_file = ROOT_DIR / "schedule_config.json"
    if config_file.exists():
        try:
            cfg = json.loads(config_file.read_text(encoding="utf-8"))
            flow_cfg = cfg.get(flow_type, {})
            if not flow_cfg.get("enabled", True):
                return False
            # Check date overrides first
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            overrides = flow_cfg.get("date_overrides", {})
            if today_str in overrides:
                override = overrides[today_str]
                if override.get("skip"):
                    return False
                # Custom runs for this date — should run
                if override.get("runs"):
                    return True
            today = datetime.datetime.now().weekday()  # 0=Mon, 6=Sun
            days = flow_cfg.get("days_of_week", list(range(7)))
            return today in days
        except Exception:
            pass
    # Fall back to DB
    config = get_schedule_config(flow_type)
    if not config:
        return True
    if not config["enabled"]:
        return False
    today = datetime.datetime.now().weekday()
    try:
        days = json.loads(config["days_of_week"])
        return today in days
    except Exception:
        return True
