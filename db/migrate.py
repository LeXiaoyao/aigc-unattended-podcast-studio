#!/usr/bin/env python3
"""One-time migration: import all existing episode directories into SQLite."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT_DIR))
from db.schema import init_db, upsert_episode


def migrate() -> None:
    init_db()
    pattern = re.compile(r"^\d{8}E\d+$")
    dirs = sorted(
        [d for d in ROOT_DIR.iterdir() if d.is_dir() and pattern.match(d.name)],
        key=lambda d: d.name,
    )
    ok = 0
    skipped = 0
    for episode_dir in dirs:
        record_path = episode_dir / "record.json"
        publish_path = episode_dir / "xiaoyuzhou_publish_result.json"

        record = {}
        if record_path.exists():
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        publish = {}
        if publish_path.exists():
            try:
                publish = json.loads(publish_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        episode_id = record.get("期数") or episode_dir.name
        date = record.get("日期") or episode_dir.name[:4] + "-" + episode_dir.name[4:6] + "-" + episode_dir.name[6:8]
        title = record.get("标题", "")
        audio_path = record.get("音频路径", "")
        cover_path = record.get("封面路径", "")

        trends_raw = record.get("三大趋势", "")
        trends = json.dumps([t.strip() for t in trends_raw.split("\n") if t.strip()], ensure_ascii=False)

        products_raw = record.get("产品洞察", "")
        products = json.dumps([p.strip() for p in products_raw.split("\n") if p.strip()], ensure_ascii=False)

        xiaoyuzhou_eid = publish.get("eid", "")
        xiaoyuzhou_pub_date = publish.get("pub_date", "")
        xiaoyuzhou_url = publish.get("episode_url", "")

        if xiaoyuzhou_eid:
            status = "published"
        elif audio_path:
            status = "audio_ready"
        else:
            status = "draft"

        # Detect flow type from flow_type.txt if exists
        flow_type_file = episode_dir / "flow_type.txt"
        flow_type = "dual_host"
        if flow_type_file.exists():
            flow_type = flow_type_file.read_text().strip()

        row = {
            "id": episode_id,
            "date": date,
            "flow_type": flow_type,
            "title": title,
            "trends": trends,
            "products": products,
            "script_path": record.get("口播稿路径", ""),
            "audio_path": audio_path,
            "cover_path": cover_path,
            "listenhub_url": record.get("ListenHub作品链接", ""),
            "listenhub_title": record.get("ListenHub标题", ""),
            "xiaoyuzhou_eid": xiaoyuzhou_eid,
            "xiaoyuzhou_pub_date": xiaoyuzhou_pub_date,
            "xiaoyuzhou_url": xiaoyuzhou_url,
            "status": status,
        }
        try:
            upsert_episode(row)
            print(f"[OK] {episode_id} - {title or '(no title)'}")
            ok += 1
        except Exception as exc:
            print(f"[ERR] {episode_id}: {exc}")
            skipped += 1

    print(f"\n迁移完成: {ok} 条成功, {skipped} 条失败")


if __name__ == "__main__":
    migrate()
