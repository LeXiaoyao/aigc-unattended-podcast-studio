"""Helpers for episode publish confirmation and state transitions."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT_DIR))

STATUS_PENDING = "pending_confirmation"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_DRAFT = "draft_ready"

EPISODE_DIR_PREFIX = "20"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_path(episode_dir: Path) -> Path:
    return episode_dir / "record.json"


def _episode_date_value(episode_id: str, record: dict[str, Any], existing: dict[str, Any]) -> str:
    date_value = str(existing.get("date") or record.get("日期") or "").strip()
    if date_value:
        return date_value
    if len(episode_id) >= 8 and episode_id[:8].isdigit():
        return f"{episode_id[:4]}-{episode_id[4:6]}-{episode_id[6:8]}"
    return datetime.now().strftime("%Y-%m-%d")


def load_record(episode_dir: Path) -> dict[str, Any]:
    record_path = _record_path(episode_dir)
    if not record_path.exists():
        return {}
    try:
        return _load_json(record_path)
    except Exception:
        return {}


def _canonical_status(value: str) -> str:
    status = value.strip().lower()
    mapping = {
        "draft_assets_ready": STATUS_DRAFT,
        "draft_ready": STATUS_DRAFT,
        "pending_confirmation": STATUS_PENDING,
        "publishing": STATUS_PUBLISHING,
        "submitted_to_xiaoyuzhou": STATUS_PUBLISHED,
        "published": STATUS_PUBLISHED,
    }
    return mapping.get(status, status)


def episode_flow_type(episode_dir: Path, record: dict[str, Any] | None = None, existing: dict[str, Any] | None = None) -> str:
    record = record if record is not None else load_record(episode_dir)
    existing = existing if existing is not None else get_episode_row(episode_dir.name)

    flow_type_path = episode_dir / "flow_type.txt"
    if flow_type_path.exists():
        value = flow_type_path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = str(record.get("flow_type") or existing.get("flow_type") or "").strip()
    return value or "solo"


def get_episode_row(episode_id: str) -> dict[str, Any]:
    try:
        from db.schema import get_episode

        return get_episode(episode_id) or {}
    except Exception:
        return {}


def current_episode_status(episode_dir: Path, record: dict[str, Any] | None = None, existing: dict[str, Any] | None = None) -> str:
    record = record if record is not None else load_record(episode_dir)
    existing = existing if existing is not None else get_episode_row(episode_dir.name)
    flow_type = episode_flow_type(episode_dir, record, existing)

    record_status = _canonical_status(str(record.get("发布状态", "")))
    if record_status:
        return record_status

    db_status = _canonical_status(str(existing.get("status", "")))
    if db_status:
        if db_status == "audio_ready" and flow_type == "solo":
            return STATUS_PENDING
        return db_status

    if (episode_dir / "xiaoyuzhou_publish_result.json").exists():
        return STATUS_PUBLISHED

    if flow_type == "solo":
        audio_path = str(record.get("音频路径") or "").strip()
        if audio_path:
            return STATUS_PENDING

    return ""


def update_episode_state(
    episode_dir: Path,
    state: str,
    *,
    extra_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = load_record(episode_dir)
    existing = get_episode_row(episode_dir.name)

    state = _canonical_status(state)
    record["发布状态"] = state
    if extra_record:
        record.update(extra_record)
    _write_json(_record_path(episode_dir), record)

    try:
        from db.schema import upsert_episode

        row: dict[str, Any] = {
            "id": episode_dir.name,
            "date": _episode_date_value(episode_dir.name, record, existing),
            "flow_type": existing.get("flow_type") or record.get("flow_type") or "solo",
            "title": record.get("标题") or existing.get("title") or "",
            "trends": existing.get("trends"),
            "products": existing.get("products"),
            "script_path": existing.get("script_path") or record.get("口播稿路径", ""),
            "audio_path": record.get("音频路径") or existing.get("audio_path", ""),
            "cover_path": record.get("封面路径") or existing.get("cover_path", ""),
            "listenhub_url": existing.get("listenhub_url", ""),
            "listenhub_title": existing.get("listenhub_title", ""),
            "xiaoyuzhou_eid": existing.get("xiaoyuzhou_eid", ""),
            "xiaoyuzhou_pub_date": existing.get("xiaoyuzhou_pub_date", ""),
            "xiaoyuzhou_url": existing.get("xiaoyuzhou_url", ""),
            "status": state,
        }
        if existing.get("created_at"):
            row["created_at"] = existing["created_at"]
        upsert_episode(row)
    except Exception:
        pass

    return record


def get_episode_dir(episode_id: str) -> Path:
    episode_dir = ROOT_DIR / "episodes" / episode_id
    if not episode_dir.is_dir():
        raise RuntimeError(f"未找到对应单集目录: {episode_id}")
    return episode_dir


def iter_episode_dirs() -> list[Path]:
    episodes_root = ROOT_DIR / "episodes"
    if not episodes_root.is_dir():
        return []
    candidates = [
        path
        for path in episodes_root.iterdir()
        if path.is_dir() and path.name.startswith(EPISODE_DIR_PREFIX)
    ]
    return sorted(candidates, key=lambda path: path.name, reverse=True)


def pick_pending_episode(episode_id: str | None = None) -> Path:
    if episode_id:
        episode_dir = get_episode_dir(episode_id)
        if episode_flow_type(episode_dir) != "solo":
            raise RuntimeError(f"{episode_id} 不是单人口播单集")
        status = current_episode_status(episode_dir)
        if status != STATUS_PENDING:
            raise RuntimeError(f"{episode_id} 当前不是待确认状态，而是 {status or 'unknown'}")
        return episode_dir

    for episode_dir in iter_episode_dirs():
        if episode_flow_type(episode_dir) != "solo":
            continue
        if current_episode_status(episode_dir) == STATUS_PENDING:
            return episode_dir
    raise RuntimeError("没有找到待确认发布的单集")


def _assert_solo_flow_type_file(episode_dir: Path) -> None:
    flow_path = episode_dir / "flow_type.txt"
    if not flow_path.exists():
        raise RuntimeError(f"{episode_dir.name} 缺少 flow_type.txt，拒绝提升 Codex 封面")
    flow_type = flow_path.read_text(encoding="utf-8").strip()
    if flow_type != "solo":
        raise RuntimeError(f"{episode_dir.name} flow_type.txt={flow_type or '<empty>'}，拒绝提升 Codex 封面")


_PLACEHOLDER_TITLE_PATTERN = re.compile(r"^AI产业观察\s*\d{4}-\d{2}-\d{2}\s*$")


def _title_is_placeholder(title: str) -> bool:
    """build-episode 阶段写入的占位标题，形如 'AI产业观察 2026-05-26'。"""
    return bool(_PLACEHOLDER_TITLE_PATTERN.match((title or "").strip()))


def _promote_codex_cover(episode_dir: Path) -> None:
    """如果 Codex 生成了候选封面且质量评估存在，将其转为 cover.jpeg 作为正式封面。"""
    codex_cover = episode_dir / "cover_codex_candidate_1.png"
    if not codex_cover.exists():
        return
    _assert_solo_flow_type_file(episode_dir)
    import subprocess
    cover_jpeg = episode_dir / "cover.jpeg"
    backup = episode_dir / "cover_before_codex.jpeg"
    if cover_jpeg.exists() and not backup.exists():
        cover_jpeg.rename(backup)
    subprocess.run(
        ["sips", "-s", "format", "jpeg", str(codex_cover), "--out", str(cover_jpeg)],
        check=True, capture_output=True,
    )
    # 只在 title.txt 缺失或仍是 build-episode 占位时，才用本地 LLM 链补一次标题。
    # codex 06:50 的 prompt 已经按规则直接产出 title.txt（含冲突动词约束），
    # 不应在发布前用本地 LLM 重写覆盖。
    title_path = episode_dir / "title.txt"
    existing_title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
    if not existing_title or _title_is_placeholder(existing_title):
        from solo_pipeline_modular.solo_title import generate_episode_title_after_cover
        generate_episode_title_after_cover(episode_dir)


def ensure_episode_publishable(episode_dir: Path) -> dict[str, Any]:
    from xiaoyuzhou_publish_pipeline import ensure_publishable_cover, read_episode_inputs

    _promote_codex_cover(episode_dir)
    inputs = read_episode_inputs(episode_dir)
    ensure_publishable_cover(episode_dir, inputs)
    return inputs


def claim_episode_for_publish(
    episode_dir: Path,
    *,
    confirmer: str,
    command_text: str,
) -> dict[str, Any]:
    status = current_episode_status(episode_dir)
    if status != STATUS_PENDING:
        raise RuntimeError(f"{episode_dir.name} 当前不能发布，状态为 {status or 'unknown'}")

    inputs = ensure_episode_publishable(episode_dir)
    update_episode_state(
        episode_dir,
        STATUS_PUBLISHING,
        extra_record={
            "发布确认时间": datetime.now().isoformat(timespec="seconds"),
            "发布确认用户": confirmer,
            "发布确认命令": command_text,
            "发布失败原因": "",
        },
    )
    return inputs


def revert_episode_to_pending(episode_dir: Path, reason: str) -> None:
    update_episode_state(
        episode_dir,
        STATUS_PENDING,
        extra_record={
            "发布失败时间": datetime.now().isoformat(timespec="seconds"),
            "发布失败原因": reason.strip(),
        },
    )


def _notify_publish_started(episode_dir: Path, title: str, confirmer: str) -> None:
    try:
        from mobile_notify import notify_rich
    except Exception:
        return

    dry_run = _publish_dry_run_enabled()
    is_auto = confirmer in ("自动发布", "auto-publish")
    status_text = (
        "内容就绪，自动发布已启动，开始提交小宇宙" if is_auto else "已收到飞书确认，开始提交小宇宙"
    )
    notify_rich(
        f"AI观测站 · {episode_dir.name} 开始发布",
        [
            {"label": "发布状态", "text": status_text + ("（dry run）" if dry_run else "")},
            {"label": "标题", "text": title},
            {"label": "发布方式", "text": "自动发布（发布后会推送封面/shownotes/正文验收包）"} if is_auto else {"label": "确认人", "text": confirmer},
        ],
        desktop=False,
    )


def _notify_publish_failed(episode_dir: Path, reason: str) -> None:
    try:
        from mobile_notify import notify_rich
    except Exception:
        return

    notify_rich(
        f"AI观测站 · {episode_dir.name} 发布失败",
        [
            {"label": "发布状态", "text": "已回退到待确认，可修复后再次发送确认指令"},
            {"label": "失败原因", "text": reason[:400]},
        ],
        desktop=False,
    )


def _notify_publish_dry_run_success(episode_dir: Path) -> None:
    try:
        from mobile_notify import notify_rich
    except Exception:
        return

    notify_rich(
        f"AI观测站 · {episode_dir.name} 发布链路 dry run 通过",
        [
            {"label": "发布状态", "text": "已完整跑通到小宇宙提交前检查，未实际提交平台"},
            {"label": "下一步", "text": f"如需真实发布，请在飞书发送：确认发布小宇宙 {episode_dir.name}"},
        ],
        desktop=False,
    )


def _publish_dry_run_enabled() -> bool:
    value = (
        os.environ.get("FEISHU_PUBLISH_DRY_RUN")
        or os.environ.get("XIAOYUZHOU_PUBLISH_DRY_RUN")
        or ""
    ).strip()
    return value.lower() in {"1", "true", "yes", "on"}


async def run_publish_job(episode_dir: Path, *, confirmer: str) -> bool:
    record = load_record(episode_dir)
    title = str(record.get("标题") or episode_dir.name).strip()
    _notify_publish_started(episode_dir, title, confirmer)
    log_path = ROOT_DIR / "logs" / f"publish_{episode_dir.name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(
            f"\n\n{'='*60}\n[webapp] publish {episode_dir.name} started "
            f"{datetime.now().isoformat(timespec='seconds')}\n{'='*60}\n"
        )

    script_path = ROOT_DIR / "xiaoyuzhou_prepare_episode.sh"
    if not script_path.exists():
        reason = f"未找到发布脚本: {script_path}"
        revert_episode_to_pending(episode_dir, reason)
        _notify_publish_failed(episode_dir, reason)
        return False

    env = dict(os.environ)
    dry_run = _publish_dry_run_enabled()
    if dry_run:
        env["DRY_RUN"] = "true"

    process = await asyncio.create_subprocess_exec(
        "zsh",
        str(script_path),
        str(episode_dir),
        cwd=str(ROOT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    output, _ = await process.communicate()
    output_text = output.decode("utf-8", errors="replace").strip()
    with log_path.open("a", encoding="utf-8") as log_file:
        if output_text:
            log_file.write(output_text)
            log_file.write("\n")
        log_file.write(f"[webapp] publish returncode={process.returncode}\n")

    if process.returncode == 0:
        if dry_run:
            update_episode_state(
                episode_dir,
                STATUS_PENDING,
                extra_record={
                    "发布演练时间": datetime.now().isoformat(timespec="seconds"),
                    "发布演练结果": "ok",
                },
            )
            _notify_publish_dry_run_success(episode_dir)
        return True

    lines = [line.strip() for line in output_text.splitlines() if line.strip()]
    reason = lines[-1] if lines else f"发布脚本退出码 {process.returncode}"
    revert_episode_to_pending(episode_dir, reason)
    _notify_publish_failed(episode_dir, reason)
    return False
