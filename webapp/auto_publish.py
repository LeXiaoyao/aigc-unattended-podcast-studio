"""每日 E1/E2 自动发布编排器。

设计原则：
- 复用 webapp 现有的 op 函数与状态机（start_synthesize_tts_op / start_dual_download_op /
  claim_episode_for_publish / run_publish_job），不新增发布路径。
- 任何一步自动化失败 → 单集回到 pending_confirmation，飞书手动指令（合成TTS /
  下载单集 / 确认发布小宇宙）全部照常可用，作为人工兜底。
- 自动重试有上限（每步 2 次），超限或过截止时间只推一条飞书提示人工接管，绝不无限重试。

时间线（默认值，可在 schedule_config.json 各 flow 下覆盖）：
- solo (E1)：05:15 起等 codex 稿（script_quality_report.md）→ 合成 TTS →
  音频就绪即发布。发布前优先等 codex 封面候选，06:00 还没有就走生图降级链。
  实际上线时间受小宇宙"当前时间+4小时05分"最短预约窗口约束。
- dual_host (E2)：07:15 起轮询 ListenHub 成品并下载 → 下载完成即发布，
  预约发布时间仍由 schedule_config 的 publish_day_offset=1 + 07:00 决定（次日 7 点）。

开关：schedule_config.json 各 flow 的 "auto_publish": true/false，每个 tick 动态读取，
改配置立即生效、无需重启 webapp。

手动测试：AUTO_PUBLISH_DRY_RUN=1 python3 -m webapp.auto_publish  （只打印决策，不执行）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT_DIR))

TICK_INTERVAL_SECONDS = 300  # 5 分钟一个 tick，与 ListenHub 生成耗时/TTS 耗时匹配

# 各 flow 默认时间参数（HH:MM），均可在 schedule_config.json 对应 flow 下同名覆盖
SOLO_DEFAULTS = {
    "auto_publish_earliest": "05:15",     # codex 稿 04:30 重写完之后再开始
    "auto_publish_cover_wait": "06:00",   # 优先等 05:00 的 codex 封面候选，过点就走生图降级链
    "auto_publish_deadline": "09:30",     # 到点还没就绪 → 推飞书提示人工接管
}
DUAL_DEFAULTS = {
    "auto_publish_earliest": "07:15",     # 07:00 提交 ListenHub，生成需要几分钟
    "auto_publish_deadline": "11:00",
}

MAX_TTS_ATTEMPTS = 2
MAX_PUBLISH_ATTEMPTS = 2

STATE_FILENAME = "auto_publish_state.json"


def _dry_run() -> bool:
    return (os.environ.get("AUTO_PUBLISH_DRY_RUN") or "").strip().lower() in {"1", "true", "yes", "on"}


def _log(message: str) -> None:
    print(f"[auto-publish] {message}", file=sys.stderr)


def _load_schedule_config() -> dict[str, Any]:
    try:
        return json.loads((ROOT_DIR / "schedule_config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _flow_config(flow_type: str) -> dict[str, Any]:
    config = _load_schedule_config().get(flow_type)
    return config if isinstance(config, dict) else {}


def _parse_hhmm(value: Any, fallback: str) -> dtime:
    for candidate in (value, fallback):
        try:
            hour, minute = str(candidate).strip().split(":")
            return dtime(int(hour), int(minute))
        except Exception:
            continue
    return dtime(0, 0)


def _flow_time(config: dict[str, Any], defaults: dict[str, str], key: str) -> dtime:
    return _parse_hhmm(config.get(key), defaults[key])


# ── 每单集的自动化状态（重试计数/一次性提醒去重），存在单集目录里 ──────────────

def _load_state(episode_dir: Path) -> dict[str, Any]:
    path = episode_dir / STATE_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(episode_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    (episode_dir / STATE_FILENAME).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _notify_once(episode_dir: Path, state: dict[str, Any], flag: str, title: str, sections: list[dict[str, str]]) -> None:
    """同一提醒每单集只推一次飞书，避免每个 tick 重复轰炸。"""
    if state.get(flag):
        return
    if _dry_run():
        _log(f"[dry-run] notify: {title}")
        return
    state[flag] = True
    _save_state(episode_dir, state)
    try:
        from mobile_notify import notify_rich

        notify_rich(title, sections, desktop=False)
    except Exception as exc:
        _log(f"飞书提醒失败（忽略）: {exc}")


# ── 就绪条件判断 ──────────────────────────────────────────────────────────────

def _pid_running(pid_file: Path) -> bool:
    if not pid_file.exists():
        return False
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError):
        return False


def _episode_has_audio(episode_dir: Path) -> bool:
    from webapp.publish_service import load_record

    audio_path = str(load_record(episode_dir).get("音频路径") or "").strip()
    if audio_path and Path(audio_path).exists():
        return True
    return bool(list(episode_dir.glob("*.mp3")))


def _today_episode_dirs() -> list[Path]:
    today = datetime.now().strftime("%Y%m%d")
    episodes_root = ROOT_DIR / "episodes"
    if not episodes_root.is_dir():
        return []
    return sorted(p for p in episodes_root.iterdir() if p.is_dir() and p.name.startswith(f"{today}E"))


# ── 发布（solo/dual 共用）────────────────────────────────────────────────────

def _parse_local_dt(raw: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def _login_recovered_at() -> tuple[str, datetime | None]:
    """读取登录熔断解除时间戳（由 xiaoyuzhou_publish_pipeline.clear_login_block 落盘）。"""
    path = ROOT_DIR / ".xiaoyuzhou_login_recovered.json"
    if not path.exists():
        return "", None
    try:
        raw = str(json.loads(path.read_text(encoding="utf-8")).get("recovered_at") or "")
    except Exception:
        return "", None
    return raw, _parse_local_dt(raw)


def _grant_login_recovery_retry(episode_dir: Path, state: dict[str, Any]) -> bool:
    """登录态恢复后，给"因登录态失效而放弃"的单集补一次重试额度。

    只在三个条件同时成立时放行，避免把真正的内容/接口故障也无限重试：
      1) 本集上次发布失败原因确实是登录态；
      2) 那次失败发生在登录态恢复之前（恢复之后再失败的属于新问题）；
      3) 这次恢复还没给过本集额度（用 recovered_at 去重，每次恢复每集只给一次）。
    """
    recovered_raw, recovered_at = _login_recovered_at()
    if not recovered_raw or recovered_at is None:
        return False
    if state.get("login_recovery_reset_at") == recovered_raw:
        return False

    from webapp.publish_service import load_record

    record = load_record(episode_dir)
    reason = str(record.get("发布失败原因") or "")
    if "登录态" not in reason:
        return False
    failed_at = _parse_local_dt(str(record.get("发布失败时间") or ""))
    if failed_at is not None and failed_at > recovered_at:
        return False

    state["publish_attempts"] = 0
    state["login_recovery_reset_at"] = recovered_raw
    state.pop("notified_publish_given_up", None)
    _save_state(episode_dir, state)
    _log(f"{episode_dir.name} 登录态已于 {recovered_raw} 恢复，补一次自动发布额度")
    return True


async def _auto_publish_episode(episode_dir: Path, state: dict[str, Any]) -> None:
    from webapp.publish_service import claim_episode_for_publish, load_record, run_publish_job

    record = load_record(episode_dir)
    cover_paths = [
        episode_dir / name for name in (
            "cover.jpeg", "cover.jpg", "cover.png", "image.png",
            "封面.jpg", "封面.jpeg", "封面.png", "cover_codex_candidate_1.png",
        )
    ]
    if record.get("封面路径"):
        cover_paths.append(Path(record["封面路径"]))
    if not any(path.is_file() and path.stat().st_size > 0 for path in cover_paths):
        _notify_once(
            episode_dir, state, "notified_cover_missing",
            f"AI观测站 · {episode_dir.name} 等待封面",
            [{"label": "状态", "text": "封面尚未就绪，保留发布重试额度；封面生成后自动继续。"}],
        )
        return

    attempts = int(state.get("publish_attempts", 0))
    if attempts >= MAX_PUBLISH_ATTEMPTS and _grant_login_recovery_retry(episode_dir, state):
        attempts = 0
    if attempts >= MAX_PUBLISH_ATTEMPTS:
        _notify_once(
            episode_dir, state, "notified_publish_given_up",
            f"AI观测站 · {episode_dir.name} 自动发布已停止重试",
            [
                {"label": "状态", "text": f"自动发布连续失败 {attempts} 次，已交回人工"},
                {"label": "人工兜底", "text": f"排查后在飞书发送：确认发布小宇宙 {episode_dir.name}"},
            ],
        )
        return

    if _dry_run():
        _log(f"[dry-run] {episode_dir.name} 满足发布条件，将执行自动发布（第 {attempts + 1} 次）")
        return

    state["publish_attempts"] = attempts + 1
    _save_state(episode_dir, state)
    _log(f"{episode_dir.name} 开始自动发布（第 {attempts + 1} 次）")
    try:
        # 封面转换和素材预检放线程，避免阻塞编排器循环
        await asyncio.to_thread(
            claim_episode_for_publish,
            episode_dir,
            confirmer="auto-publish",
            command_text=f"自动发布 {episode_dir.name}",
        )
    except Exception as exc:
        _log(f"{episode_dir.name} claim 失败: {exc}")
        _notify_once(
            episode_dir, state, f"notified_claim_failed_{attempts + 1}",
            f"AI观测站 · {episode_dir.name} 自动发布预检失败",
            [
                {"label": "原因", "text": str(exc)[:400]},
                {"label": "人工兜底", "text": f"修复后在飞书发送：确认发布小宇宙 {episode_dir.name}"},
            ],
        )
        return

    # run_publish_job 自带开始/成功/失败通知；失败会把状态回退到待确认，
    # 下个 tick 会再试一次，直到 MAX_PUBLISH_ATTEMPTS 后交回人工。
    await run_publish_job(episode_dir, confirmer="自动发布")


# ── solo (E1) ────────────────────────────────────────────────────────────────

async def _handle_solo(episode_dir: Path, now: datetime) -> None:
    from webapp.publish_service import (
        STATUS_PENDING,
        STATUS_PUBLISHED,
        STATUS_PUBLISHING,
        current_episode_status,
    )
    from webapp.routers.pipeline import start_synthesize_tts_op

    config = _flow_config("solo")
    status = current_episode_status(episode_dir)
    if status in (STATUS_PUBLISHING, STATUS_PUBLISHED):
        return
    if now.time() < _flow_time(config, SOLO_DEFAULTS, "auto_publish_earliest"):
        return

    state = _load_state(episode_dir)
    deadline = _flow_time(config, SOLO_DEFAULTS, "auto_publish_deadline")

    # 1. 稿件就绪信号：要求 podcast_script.txt 与 script_quality_report.md 均存在且未标记阻塞
    report_path = episode_dir / "script_quality_report.md"
    script_path = episode_dir / "podcast_script.txt"
    report_text = report_path.read_text(encoding="utf-8", errors="ignore") if report_path.exists() else ""
    report_blocked = (
        not report_path.exists()
        or not script_path.exists()
        or "候选不足，未出稿" in report_text
        or "阻塞原因" in report_text
    )
    if report_blocked:
        if now.time() >= deadline:
            _notify_once(
                episode_dir, state, "notified_script_missing",
                f"AI观测站 · {episode_dir.name} 自动发布未启动",
                [
                    {"label": "原因", "text": "稿件未就绪或质检报告标记阻塞（缺 script_quality_report.md / 候选不足），自动发布跳过"},
                    {"label": "人工兜底", "text": f"稿件确认后在飞书发送：合成TTS {episode_dir.name}"},
                ],
            )
        return

    # 2. 音频就绪 → 发布（优先等 codex 封面候选，过点就交给 claim 的生图降级链）
    if status == STATUS_PENDING and _episode_has_audio(episode_dir):
        cover_ready = (episode_dir / "cover_codex_candidate_1.png").exists()
        if not cover_ready and now.time() < _flow_time(config, SOLO_DEFAULTS, "auto_publish_cover_wait"):
            _log(f"{episode_dir.name} 音频已就绪，等待 codex 封面候选")
            return
        await _auto_publish_episode(episode_dir, state)
        return

    # 3. 音频已在但状态不是待确认（如 cover_retry_pending）→ 等对应流程把状态翻过来
    if _episode_has_audio(episode_dir):
        _log(f"{episode_dir.name} 音频已就绪但状态为 {status or 'unknown'}，等待状态推进")
        return

    # 4. 还没有音频 → 自动合成 TTS
    if _pid_running(Path(f"/tmp/podcast_synthesize_tts_{episode_dir.name}.pid")):
        return
    attempts = int(state.get("tts_attempts", 0))
    if attempts >= MAX_TTS_ATTEMPTS:
        _notify_once(
            episode_dir, state, "notified_tts_given_up",
            f"AI观测站 · {episode_dir.name} 自动 TTS 已停止重试",
            [
                {"label": "状态", "text": f"TTS 连续失败 {attempts} 次，已交回人工"},
                {"label": "人工兜底", "text": f"排查后在飞书发送：合成TTS {episode_dir.name}"},
            ],
        )
        return

    if _dry_run():
        _log(f"[dry-run] {episode_dir.name} 将启动自动 TTS（第 {attempts + 1} 次）")
        return

    ok, message = await start_synthesize_tts_op(episode_dir.name)
    if ok:
        state["tts_attempts"] = attempts + 1
        _save_state(episode_dir, state)
        _log(f"{episode_dir.name} 自动 TTS 已启动（第 {attempts + 1} 次）: {message}")
    else:
        # "already running" 之类的并发拒绝不计入失败次数，等下个 tick
        _log(f"{episode_dir.name} 自动 TTS 未启动: {message}")


# ── dual_host (E2) ───────────────────────────────────────────────────────────

async def _handle_dual(episode_dir: Path, now: datetime) -> None:
    from webapp.publish_service import (
        STATUS_PENDING,
        STATUS_PUBLISHED,
        STATUS_PUBLISHING,
        current_episode_status,
        update_episode_state,
    )
    from webapp.routers.pipeline import start_dual_download_op

    config = _flow_config("dual_host")
    status = current_episode_status(episode_dir)
    if status in (STATUS_PUBLISHING, STATUS_PUBLISHED):
        return
    if now.time() < _flow_time(config, DUAL_DEFAULTS, "auto_publish_earliest"):
        return

    state = _load_state(episode_dir)
    deadline = _flow_time(config, DUAL_DEFAULTS, "auto_publish_deadline")

    # 1. 音频未下载 → 轮询 ListenHub（download op 在成品未生成时会自行退出，属正常轮询）
    if not _episode_has_audio(episode_dir):
        if now.time() >= deadline:
            _notify_once(
                episode_dir, state, "notified_download_given_up",
                f"AI观测站 · {episode_dir.name} 自动下载已停止轮询",
                [
                    {"label": "状态", "text": "ListenHub 成品迟迟未就绪，已交回人工"},
                    {"label": "人工兜底", "text": f"稍后在飞书发送：下载单集 {episode_dir.name}"},
                ],
            )
            return
        if _pid_running(Path(f"/tmp/podcast_dual_download_{episode_dir.name}.pid")):
            return
        if _dry_run():
            _log(f"[dry-run] {episode_dir.name} 将轮询 ListenHub 下载")
            return
        ok, message = await start_dual_download_op(episode_dir.name)
        _log(f"{episode_dir.name} 自动下载轮询: {message if ok else '未启动 - ' + message}")
        return

    # 2. 音频已就绪 → 提升到待确认后发布（复刻飞书确认对 dual 的状态提升逻辑）
    PUBLISHABLE = (STATUS_PENDING, "audio_ready", "draft_ready", "")
    if status not in PUBLISHABLE:
        _log(f"{episode_dir.name} 状态 {status} 不可自动发布，跳过")
        return
    if status != STATUS_PENDING:
        if _dry_run():
            _log(f"[dry-run] {episode_dir.name} 将从 {status or 'unknown'} 提升到待确认")
        else:
            update_episode_state(episode_dir, STATUS_PENDING)
    await _auto_publish_episode(episode_dir, state)


# ── 主循环 ───────────────────────────────────────────────────────────────────

async def _notify_recovered_backlog(now: datetime) -> None:
    from webapp.publish_service import STATUS_PENDING, current_episode_status, episode_flow_type, load_record

    recovered_raw, recovered_at = _login_recovered_at()
    episodes_root = ROOT_DIR / "episodes"
    if recovered_at is None or not episodes_root.is_dir():
        return
    for episode_dir in sorted(episodes_root.iterdir()):
        try:
            day = datetime.strptime(episode_dir.name[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if not episode_dir.is_dir() or not now.date() - timedelta(days=7) <= day < now.date():
            continue
        if current_episode_status(episode_dir) != STATUS_PENDING:
            continue
        if (episode_dir / "xiaoyuzhou_publish_result.json").exists():
            continue
        if not _flow_config(episode_flow_type(episode_dir)).get("auto_publish"):
            continue
        record = load_record(episode_dir)
        failed_at = _parse_local_dt(record.get("发布失败时间", ""))
        if "登录态" not in str(record.get("发布失败原因", "")) or failed_at is None or failed_at > recovered_at:
            continue
        # 跨日新闻的原排期已失效，提醒核对后补发，不能静默套用今天的排期。
        _notify_once(
            episode_dir, _load_state(episode_dir), "notified_login_recovered_backlog",
            f"AI观测站 · {episode_dir.name} 登录已恢复，待补发",
            [{"label": "状态", "text": f"登录已于 {recovered_raw} 恢复；本期跨日未发布，请核对平台已有节目及新排期后，在飞书确认发布。"}],
        )


async def run_tick() -> None:
    from webapp.publish_service import episode_flow_type

    now = datetime.now()
    await _notify_recovered_backlog(now)
    for episode_dir in _today_episode_dirs():
        try:
            flow_type = episode_flow_type(episode_dir)
            if flow_type == "solo" and _flow_config("solo").get("auto_publish"):
                await _handle_solo(episode_dir, now)
            elif flow_type == "dual_host" and _flow_config("dual_host").get("auto_publish"):
                await _handle_dual(episode_dir, now)
        except Exception as exc:
            _log(f"{episode_dir.name} tick 异常（跳过，下轮重试）: {exc}")


async def auto_publish_loop() -> None:
    _log(f"自动发布编排器已启动，每 {TICK_INTERVAL_SECONDS}s 一个 tick")
    while True:
        try:
            await run_tick()
        except Exception as exc:
            _log(f"tick 顶层异常（忽略，下轮重试）: {exc}")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_tick())
