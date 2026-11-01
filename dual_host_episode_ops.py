#!/usr/bin/env python3
"""CLI helper for dual_host episode manual operations triggered from the webapp.

Usage:
  python dual_host_episode_ops.py download-listenhub --episode 20260424E3
  python dual_host_episode_ops.py gen-cover-title    --episode 20260424E3 [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(ROOT_DIR))

ENV_FILE = ROOT_DIR / ".env.listenhub"
SHARED_ENV_FILE = ROOT_DIR / "shared" / "config" / ".env.listenhub"


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_env() -> None:
    """Load env vars from shared config first, then root .env.listenhub (root wins on conflict)."""
    for env_path in (SHARED_ENV_FILE, ENV_FILE):
        if not env_path.exists():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key:
                os.environ.setdefault(key.strip(), value.strip().strip('"'))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_episode_dir(episode_id: str) -> Path:
    ep_dir = ROOT_DIR / "episodes" / episode_id
    if not ep_dir.is_dir():
        print(f"[ERROR] 未找到单集目录: {ep_dir}", file=sys.stderr)
        sys.exit(1)
    return ep_dir


def _find_lh_episode_id(episode_dir: Path) -> str:
    """Extract ListenHub episodeId from local result files."""
    for fname in ("listenhub_episode_result.json", "listenhub_create_result.json"):
        fpath = episode_dir / fname
        if not fpath.exists():
            continue
        data = _read_json(fpath)
        # Direct field or nested under "data"
        ep_id = (
            data.get("episodeId")
            or data.get("data", {}).get("episodeId")
            or ""
        )
        if ep_id:
            return str(ep_id).strip()
    return ""


def _find_existing_mp3(episode_dir: Path) -> Path | None:
    """Return the largest/newest .mp3 in the episode dir, if > 500 KB."""
    candidates = sorted(
        (f for f in episode_dir.glob("*.mp3") if f.is_file()),
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    for f in candidates:
        if f.stat().st_size > 500_000:
            return f
    return None


# ── command: download-listenhub ────────────────────────────────────────────────

def cmd_download_listenhub(episode_id: str) -> None:
    """Re-fetch audio, outline, title from ListenHub API for a dual_host episode."""
    _load_env()

    from ai_news_pipeline import listenhub_get, read_json, write_json  # type: ignore

    episode_dir = _get_episode_dir(episode_id)
    lh_ep_id = _find_lh_episode_id(episode_dir)

    if not lh_ep_id:
        print("[ERROR] 未找到 ListenHub episodeId（需要 listenhub_episode_result.json 或 listenhub_create_result.json）", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] 查询 ListenHub 单集: {lh_ep_id}")
    resp = listenhub_get(f"/podcast/episodes/{lh_ep_id}")
    data: dict = resp.get("data", resp)

    process_status = data.get("processStatus", "")
    print(f"[INFO] processStatus: {process_status}")

    if process_status != "success":
        print(f"[WARN] 单集尚未生成完成，当前状态: {process_status or 'unknown'}", file=sys.stderr)
        sys.exit(1)

    title: str = (data.get("title") or "").strip()
    audio_url: str = (data.get("audioUrl") or "").strip()
    outline: str = (data.get("outline") or "").strip()
    cover_url: str = (data.get("cover") or "").strip()

    # ── 1. Save / update outline + script ─────────────────────────────────────
    if outline:
        outline_path = episode_dir / "listenhub_outline.md"
        outline_path.write_text(outline, encoding="utf-8")
        print(f"[INFO] 大纲已保存: {outline_path.name}（{len(outline)} 字符）")
    else:
        print("[WARN] API 返回的 outline 为空，跳过大纲保存")

    script_text = ""
    scripts = data.get("scripts") or []
    if isinstance(scripts, list) and scripts:
        lines = []
        for seg in scripts:
            speaker = str(seg.get("speakerName") or "").strip()
            content = str(seg.get("content") or "").strip()
            if content:
                lines.append(f"{speaker}：{content}" if speaker else content)
        script_text = "\n\n".join(lines)
        if script_text:
            script_path = episode_dir / "listenhub_script.txt"
            script_path.write_text(script_text, encoding="utf-8")
            print(f"[INFO] 对话脚本已保存: {script_path.name}（{len(script_text)} 字符，{len(lines)} 段）")
    else:
        print("[WARN] API 未返回 scripts，跳过脚本保存")

    # ── 2. Save title ─────────────────────────────────────────────────────────
    if title:
        (episode_dir / "title.txt").write_text(title, encoding="utf-8")
        print(f"[INFO] 标题: {title}")

    # ── 3. Download audio if missing ──────────────────────────────────────────
    existing_mp3 = _find_existing_mp3(episode_dir)
    if existing_mp3:
        print(f"[INFO] 音频已存在，跳过下载: {existing_mp3.name}（{existing_mp3.stat().st_size // 1024} KB）")
    elif audio_url:
        safe_title = title[:60] if title else "ListenHub AI播客"
        # Remove chars not safe for filenames
        import re
        safe_title = re.sub(r'[\\/:*?"<>|]', " ", safe_title).strip()
        final_mp3 = episode_dir / f"{safe_title}.mp3"
        tmp_mp3 = episode_dir / ".audio_download_tmp"
        print(f"[INFO] 下载音频 → {final_mp3.name} ...")
        result = subprocess.run(
            [
                "curl", "-L", "--fail", "--silent", "--show-error",
                "-H", "Referer: https://listenhub.ai/",
                "-H", "Origin: https://listenhub.ai",
                audio_url, "-o", str(tmp_mp3),
            ],
            check=False,
        )
        if result.returncode != 0 or not tmp_mp3.exists():
            print("[ERROR] 音频下载失败", file=sys.stderr)
            tmp_mp3.unlink(missing_ok=True)
            sys.exit(1)
        tmp_mp3.rename(final_mp3)
        print(f"[INFO] 音频已保存: {final_mp3.name}（{final_mp3.stat().st_size // 1024} KB）")
        existing_mp3 = final_mp3
    else:
        print("[WARN] API 未返回 audioUrl，无法下载音频")

    # ── 4. Update record.json ─────────────────────────────────────────────────
    record_path = episode_dir / "record.json"
    record: dict = _read_json(record_path)
    changed = False

    if title and not record.get("ListenHub标题"):
        record["ListenHub标题"] = title
        changed = True
    if title and not record.get("标题"):
        record["标题"] = title
        changed = True
    if existing_mp3 and not record.get("音频路径"):
        record["音频路径"] = str(existing_mp3)
        changed = True
    # Ensure audio_ready status if we now have audio
    if existing_mp3 and not record.get("发布状态"):
        record["发布状态"] = "pending_confirmation"
        changed = True
    if record.get("flow_type") != "dual_host":
        record["flow_type"] = "dual_host"
        changed = True

    if changed:
        _write_json(record_path, record)
        print("[INFO] record.json 已更新")

    # ── 5. Refresh listenhub_episode_result.json ──────────────────────────────
    ep_result_path = episode_dir / "listenhub_episode_result.json"
    existing_result: dict = _read_json(ep_result_path)
    # Merge fresh API data on top; preserve local_* fields
    merged = {**data}
    merged["local_audio_path"] = str(existing_mp3) if existing_mp3 else existing_result.get("local_audio_path", "")
    cover_jpeg = episode_dir / "cover.jpeg"
    merged["local_cover_path"] = str(cover_jpeg) if cover_jpeg.exists() else existing_result.get("local_cover_path", "")
    _write_json(ep_result_path, merged)
    print("[INFO] listenhub_episode_result.json 已刷新")

    # ── 6. Update DB ──────────────────────────────────────────────────────────
    try:
        from db.schema import upsert_episode, get_episode  # type: ignore
        row = get_episode(episode_id) or {}
        row.update({
            "id": episode_id,
            "flow_type": "dual_host",
            "title": record.get("标题") or title or row.get("title", ""),
            "audio_path": record.get("音频路径") or row.get("audio_path", ""),
            "listenhub_title": title or row.get("listenhub_title", ""),
        })
        upsert_episode(row)
        print("[INFO] DB 记录已更新")
    except Exception as exc:
        print(f"[WARN] DB 更新失败（可忽略）: {exc}")

    print(f"[OK] ListenHub 下载/同步完成: {episode_id}")

    # ── 7. 飞书回执：手机端据此审核标题/封面后直接回复发布指令 ────────────────
    try:
        from mobile_notify import notify_feishu_image, notify_rich

        audio_desc = (
            f"{existing_mp3.name}（{existing_mp3.stat().st_size // 1024 // 1024} MB）"
            if existing_mp3
            else "缺失，请检查"
        )
        notify_rich(
            "AI观测站 · 双人单集已下载",
            [
                {"label": "期号", "text": episode_id},
                {"label": "标题", "text": title or "(未返回标题)"},
                {"label": "音频", "text": audio_desc},
                {"label": "封面", "text": "见下图" if cover_jpeg.exists() else "尚未生成，后台跑「封面+标题」后再发布"},
                {"label": "下一步", "text": f"标题封面没问题就回复 确认发布小宇宙 {episode_id}（默认次日 7:00 上线）"},
            ],
            desktop=False,
        )
        if cover_jpeg.exists():
            notify_feishu_image(str(cover_jpeg), f"AI观测站 · {episode_id} 封面")
        # 脚本与大纲全文推送（手机审稿不接受截断预览）
        from mobile_notify import notify_feishu_long_text

        if script_text:
            notify_feishu_long_text(f"AI观测站 · {episode_id} 双人对话脚本全文", script_text)
        if outline:
            notify_feishu_long_text(f"AI观测站 · {episode_id} 大纲全文", outline)
    except Exception as exc:
        print(f"[WARN] 下载回执推送失败（可忽略）: {exc}", file=sys.stderr)


# ── command: gen-cover-title ───────────────────────────────────────────────────

def cmd_gen_cover_title(episode_id: str, force: bool = False) -> None:
    """Regenerate cover (Gemini→MiniMax) + render title for a dual_host episode."""
    _load_env()

    from ai_news_pipeline import regenerate_cover_for_episode, read_json, write_json  # type: ignore

    episode_dir = _get_episode_dir(episode_id)
    record_path = episode_dir / "record.json"
    record: dict = _read_json(record_path)

    # ── Ensure title is set ───────────────────────────────────────────────────
    if not record.get("标题"):
        # Try ListenHub episode result first
        lh_result_path = episode_dir / "listenhub_episode_result.json"
        lh_data = _read_json(lh_result_path)
        lh_title = (lh_data.get("title") or "").strip()

        # Also try title.txt
        title_txt = episode_dir / "title.txt"
        txt_title = title_txt.read_text(encoding="utf-8").strip() if title_txt.exists() else ""

        title = lh_title or txt_title
        if title:
            record["标题"] = title
            record.setdefault("ListenHub标题", title)
            _write_json(record_path, record)
            print(f"[INFO] 标题已从 ListenHub 取得: {title}")
        else:
            print("[WARN] 未找到标题，将使用默认标题渲染封面")

    # ── Check if cover already exists ────────────────────────────────────────
    existing_cover = record.get("封面路径", "")
    if not force and existing_cover and Path(existing_cover).exists():
        print(f"[INFO] 封面已存在: {existing_cover}（传入 --force 可强制覆盖）")
        # Still continue to regenerate

    print(f"[INFO] 开始生成封面（Gemini → MiniMax 降级链）...")
    result = regenerate_cover_for_episode(episode_dir)

    if result.get("status") == "success":
        print(f"[OK] 封面生成成功")
        print(f"      背景图: {result.get('background_path', '')}")
        print(f"      封面: {result.get('cover_path', '')}")
        print(f"      标题: {result.get('overlay_title', '')}")
        print(f"      生成方式: {result.get('method', 'unknown')}")
    else:
        err = result.get("error", "unknown error")
        print(f"[ERROR] 封面生成失败: {err}", file=sys.stderr)
        sys.exit(1)

    # ── Update DB ─────────────────────────────────────────────────────────────
    try:
        from db.schema import upsert_episode, get_episode  # type: ignore
        updated_record = _read_json(record_path)
        row = get_episode(episode_id) or {}
        row.update({
            "id": episode_id,
            "flow_type": "dual_host",
            "title": updated_record.get("标题") or row.get("title", ""),
            "cover_path": updated_record.get("封面路径") or row.get("cover_path", ""),
        })
        upsert_episode(row)
        print("[INFO] DB cover/title 已更新")
    except Exception as exc:
        print(f"[WARN] DB 更新失败（可忽略）: {exc}")


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Dual host episode manual operations")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dl = sub.add_parser("download-listenhub", help="Re-download audio/outline/title from ListenHub")
    p_dl.add_argument("--episode", required=True, metavar="EPISODE_ID")

    p_cv = sub.add_parser("gen-cover-title", help="Regenerate cover image and set title")
    p_cv.add_argument("--episode", required=True, metavar="EPISODE_ID")
    p_cv.add_argument("--force", action="store_true", help="Overwrite existing cover")

    args = parser.parse_args()

    if args.cmd == "download-listenhub":
        cmd_download_listenhub(args.episode)
    elif args.cmd == "gen-cover-title":
        cmd_gen_cover_title(args.episode, force=getattr(args, "force", False))


if __name__ == "__main__":
    main()
