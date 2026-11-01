"""Admin pipeline trigger and live log streaming."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from fastapi import APIRouter, Cookie
from fastapi.responses import JSONResponse, StreamingResponse

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(ROOT_DIR))

router = APIRouter()

LOG_FILES = {
    "dual_host": ROOT_DIR / "logs" / "ai-podcast-generator.out.log",
    "solo": ROOT_DIR / "logs" / "solo-podcast-generator.out.log",
}

PIPELINE_SCRIPTS = {
    "dual_host": str(ROOT_DIR / "run_ai_podcast_weekly.sh"),
    "solo": str(ROOT_DIR / "run_solo_podcast_daily.sh"),
}


def _is_admin(cookie: str | None) -> bool:
    from webapp.deps import verify_admin_cookie
    return verify_admin_cookie(cookie)


@router.post("/admin/pipeline/trigger")
async def trigger_pipeline(
    request: dict | None = None,
    podcast_admin: str | None = Cookie(default=None),
):
    # Accept raw JSON body
    from fastapi import Request as FastRequest
    pass


# Re-define to accept body properly
from fastapi import Request as FastRequest


@router.post("/admin/pipeline/run")
async def run_pipeline(request: FastRequest, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    flow = body.get("flow", "solo")
    if flow not in PIPELINE_SCRIPTS:
        return JSONResponse({"error": f"Unknown flow: {flow}"}, status_code=400)

    script = PIPELINE_SCRIPTS[flow]
    # Source env file first, add --force to bypass schedule check for manual triggers
    env_file = ROOT_DIR / "shared" / "config" / ".env.listenhub"
    force_env = "SOLO_FORCE=1"  # Used by run_solo_podcast_daily.sh

    # Write output directly to log file so SSE streaming picks it up in real-time
    log_file = LOG_FILES.get(flow)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # Append a separator for new run
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n\n{'='*60}\n[手动触发] {flow} pipeline started\n{'='*60}\n")

    cmd = f"source {env_file} && {force_env} zsh {script}"
    if log_file:
        cmd = f"({cmd}) >> {log_file} 2>&1"

    pid_file = Path(f"/tmp/podcast_pipeline_{flow}.pid")

    # Check if already running
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            return JSONResponse({"error": f"{flow} pipeline already running (pid={pid})"}, status_code=409)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=str(ROOT_DIR),
    )
    pid_file.write_text(str(proc.pid))

    async def cleanup():
        await proc.wait()
        pid_file.unlink(missing_ok=True)

    asyncio.create_task(cleanup())

    return JSONResponse({"ok": True, "flow": flow, "pid": proc.pid})


@router.post("/admin/episodes/{episode_id}/publish")
async def publish_episode(episode_id: str, request: FastRequest, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from webapp.publish_service import (
        get_episode_dir,
        current_episode_status,
        claim_episode_for_publish,
        run_publish_job,
        STATUS_PENDING,
    )

    try:
        episode_dir = get_episode_dir(episode_id)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    status = current_episode_status(episode_dir)
    if status not in (STATUS_PENDING, "audio_ready"):
        return JSONResponse({"error": f"单集状态不可发布: {status}"}, status_code=409)

    # 禁止在 regen_shownotes 还在运行时发布（防止 shownotes.txt 被并发覆盖导致内容回退）
    regen_pid_file = Path(f"/tmp/podcast_regen_shownotes_{episode_id}.pid")
    if regen_pid_file.exists():
        try:
            pid = int(regen_pid_file.read_text().strip())
            os.kill(pid, 0)
            return JSONResponse(
                {"error": f"「更新Shownotes」正在运行中 (pid={pid})，请等待完成后再发布"},
                status_code=409,
            )
        except (ProcessLookupError, ValueError):
            regen_pid_file.unlink(missing_ok=True)

    try:
        claim_episode_for_publish(episode_dir, confirmer="webapp", command_text=f"一键发布 {episode_id}")
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)

    asyncio.create_task(run_publish_job(episode_dir, confirmer="webapp"))
    return JSONResponse({"ok": True, "episode_id": episode_id, "status": "publishing"})


async def start_synthesize_tts_op(episode_id: str) -> tuple[bool, str]:
    """合成 TTS 的核心逻辑，webapp 按钮和飞书指令共用。返回 (是否已启动, 说明)。"""
    from webapp.publish_service import get_episode_dir, current_episode_status

    try:
        episode_dir = get_episode_dir(episode_id)
    except RuntimeError as e:
        return False, str(e)

    status = current_episode_status(episode_dir)
    SYNTHESIZE_ALLOWED = ("draft_ready", "cover_retry_pending", "pending_confirmation", "audio_ready", "")
    if status not in SYNTHESIZE_ALLOWED:
        return False, f"当前状态不可合成 TTS: {status}"

    pid_file = Path(f"/tmp/podcast_synthesize_tts_{episode_id}.pid")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return False, f"TTS already running (pid={pid})"
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    log_file = ROOT_DIR / "logs" / f"synthesize_tts_{episode_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n\n{'='*60}\n[webapp] synthesize-tts {episode_id} started\n{'='*60}\n")

    stdout = open(log_file, "ab")
    proc = await asyncio.create_subprocess_exec(
        _python_cmd(),
        str(ROOT_DIR / "solo_pipeline.py"),
        "synthesize-tts",
        "--episode",
        episode_id,
        cwd=str(ROOT_DIR),
        stdout=stdout,
        stderr=asyncio.subprocess.STDOUT,
    )
    pid_file.write_text(str(proc.pid))

    async def cleanup():
        try:
            await proc.wait()
        finally:
            stdout.close()
            pid_file.unlink(missing_ok=True)

    asyncio.create_task(cleanup())
    return True, f"TTS 合成已启动 (pid={proc.pid})，日志 logs/synthesize_tts_{episode_id}.log"


@router.post("/admin/episodes/{episode_id}/synthesize-tts")
async def synthesize_episode_tts(episode_id: str, request: FastRequest, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    ok, message = await start_synthesize_tts_op(episode_id)
    if not ok:
        status_code = 404 if "未找到" in message else 409
        return JSONResponse({"error": message}, status_code=status_code)
    return JSONResponse({"ok": True, "episode_id": episode_id, "status": "synthesizing"})


def _python_cmd() -> str:
    p = Path("/opt/homebrew/bin/python3")
    return str(p) if p.exists() else "python3"


async def _spawn_episode_op(episode_id: str, op_name: str, extra_args: list[str]) -> JSONResponse:
    """Shared helper: spawn solo_pipeline.py sub-command for a given episode operation."""
    pid_file = Path(f"/tmp/podcast_{op_name}_{episode_id}.pid")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return JSONResponse({"error": f"已在运行 (pid={pid})"}, status_code=409)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    log_file = ROOT_DIR / "logs" / f"{op_name}_{episode_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n\n{'='*60}\n[webapp] {op_name} {episode_id} started\n{'='*60}\n")

    stdout = open(log_file, "ab")
    proc = await asyncio.create_subprocess_exec(
        _python_cmd(), str(ROOT_DIR / "solo_pipeline.py"), *extra_args,
        cwd=str(ROOT_DIR),
        stdout=stdout,
        stderr=asyncio.subprocess.STDOUT,
    )
    pid_file.write_text(str(proc.pid))

    async def cleanup():
        returncode = None
        try:
            returncode = await proc.wait()
        finally:
            stdout.close()
            with open(log_file, "a", encoding="utf-8") as f:
                status = "ok" if returncode == 0 else "failed"
                f.write(f"[webapp] {op_name} {episode_id} finished rc={returncode} status={status}\n")
            pid_file.unlink(missing_ok=True)

    asyncio.create_task(cleanup())
    return JSONResponse({"ok": True, "episode_id": episode_id, "op": op_name, "pid": proc.pid})


@router.post("/admin/episodes/{episode_id}/generate-cover-title")
async def generate_cover_title(episode_id: str, request: FastRequest, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from webapp.publish_service import get_episode_dir, load_record

    try:
        episode_dir = get_episode_dir(episode_id)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    force = bool(body.get("force", False))

    if not force:
        record = load_record(episode_dir)
        existing_title = record.get("标题", "").strip()
        existing_cover = record.get("封面路径", "").strip()
        cover_exists = bool(existing_cover and Path(existing_cover).exists())
        if existing_title or cover_exists:
            return JSONResponse({
                "exists": True,
                "title": existing_title,
                "cover": existing_cover if cover_exists else "",
            })

    extra_args = ["gen-cover-title", "--episode", episode_id]
    if force:
        extra_args.append("--force")
    return await _spawn_episode_op(episode_id, "gen_cover_title", extra_args)


async def start_regen_shownotes_op(episode_id: str) -> tuple[bool, str]:
    """更新 shownotes 的核心逻辑，webapp 按钮和飞书指令共用。返回 (是否已启动, 说明)。"""
    from webapp.publish_service import get_episode_dir

    try:
        episode_dir = get_episode_dir(episode_id)
    except RuntimeError as e:
        return False, str(e)

    audio_path = episode_dir / "podcast_solo.mp3"
    if not audio_path.exists():
        # also check generic name
        alt = episode_dir / "podcast.mp3"
        if not alt.exists():
            return False, "音频文件不存在，请先合成 TTS"

    # 有精准 chunk 时间线时直接使用；否则退回 heuristic（faster_whisper 未必安装）
    chunk_timeline = episode_dir / "tts_chunk_timeline.json"
    if chunk_timeline.exists():
        extra_args = ["regen-shownotes", "--episode", episode_id]
    else:
        extra_args = ["regen-shownotes", "--episode", episode_id,
                      "--timecode-mode", "heuristic", "--force-recompute-timecodes"]

    response = await _spawn_episode_op(episode_id, "regen_shownotes", extra_args)
    if response.status_code >= 400:
        try:
            detail = json.loads(bytes(response.body)).get("error", "启动失败")
        except Exception:
            detail = "启动失败"
        return False, detail
    return True, f"Shownotes 重建已启动，日志 logs/regen_shownotes_{episode_id}.log"


@router.post("/admin/episodes/{episode_id}/generate-shownotes")
async def generate_shownotes(episode_id: str, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    ok, message = await start_regen_shownotes_op(episode_id)
    if not ok:
        status_code = 404 if "未找到" in message else 409
        return JSONResponse({"error": message}, status_code=status_code)
    return JSONResponse({"ok": True, "episode_id": episode_id, "status": "regenerating"})


@router.get("/admin/pipeline/logs/episode/{episode_id}/{op_name}")
async def stream_episode_logs(episode_id: str, op_name: str, podcast_admin: str | None = Cookie(default=None)):
    """Stream per-episode operation logs: op_name in {synthesize_tts, gen_cover_title, regen_shownotes}."""
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    allowed_ops = {"synthesize_tts", "gen_cover_title", "regen_shownotes", "dual_download", "dual_cover_title"}
    if op_name not in allowed_ops:
        return JSONResponse({"error": "Unknown operation"}, status_code=400)

    log_path = ROOT_DIR / "logs" / f"{op_name}_{episode_id}.log"

    async def generate():
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-100:]:
                yield f"data: {line}\n\n"
        else:
            yield "data: (日志文件尚不存在)\n\n"

        last_size = log_path.stat().st_size if log_path.exists() else 0
        for _ in range(300):
            await asyncio.sleep(1)
            if not log_path.exists():
                continue
            current_size = log_path.stat().st_size
            if current_size > last_size:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(last_size)
                    new_content = f.read()
                for line in new_content.splitlines():
                    if line.strip():
                        yield f"data: {line}\n\n"
                last_size = current_size
        yield "data: [log stream ended]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _spawn_dual_op(episode_id: str, op_name: str, extra_args: list[str]) -> JSONResponse:
    """Shared helper: spawn dual_host_episode_ops.py sub-command for a given operation."""
    pid_file = Path(f"/tmp/podcast_{op_name}_{episode_id}.pid")
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return JSONResponse({"error": f"已在运行 (pid={pid})"}, status_code=409)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    log_file = ROOT_DIR / "logs" / f"{op_name}_{episode_id}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n\n{'='*60}\n[webapp] {op_name} {episode_id} started\n{'='*60}\n")

    stdout = open(log_file, "ab")
    proc = await asyncio.create_subprocess_exec(
        _python_cmd(), str(ROOT_DIR / "dual_host_episode_ops.py"), *extra_args,
        cwd=str(ROOT_DIR),
        stdout=stdout,
        stderr=asyncio.subprocess.STDOUT,
    )
    pid_file.write_text(str(proc.pid))

    async def cleanup():
        try:
            await proc.wait()
        finally:
            stdout.close()
            pid_file.unlink(missing_ok=True)

    asyncio.create_task(cleanup())
    return JSONResponse({"ok": True, "episode_id": episode_id, "op": op_name, "pid": proc.pid})


async def start_dual_download_op(episode_id: str) -> tuple[bool, str]:
    """双人下载单集的核心逻辑，webapp 按钮和飞书指令共用。返回 (是否已启动, 说明)。"""
    from webapp.publish_service import get_episode_dir
    try:
        get_episode_dir(episode_id)
    except RuntimeError as e:
        return False, str(e)

    response = await _spawn_dual_op(
        episode_id, "dual_download",
        ["download-listenhub", "--episode", episode_id],
    )
    if response.status_code >= 400:
        try:
            detail = json.loads(bytes(response.body)).get("error", "启动失败")
        except Exception:
            detail = "启动失败"
        return False, detail
    return True, f"下载已启动，日志 logs/dual_download_{episode_id}.log"


@router.post("/admin/dual-episodes/{episode_id}/download-listenhub")
async def dual_download_listenhub(episode_id: str, podcast_admin: str | None = Cookie(default=None)):
    """手动从 ListenHub 重新下载音频 / 大纲 / 标题（自动化下载失败时的人工兜底）。"""
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    ok, message = await start_dual_download_op(episode_id)
    if not ok:
        status_code = 404 if "未找到" in message else 409
        return JSONResponse({"error": message}, status_code=status_code)
    return JSONResponse({"ok": True, "episode_id": episode_id, "op": "dual_download"})


@router.post("/admin/dual-episodes/{episode_id}/publish")
async def publish_dual_episode(episode_id: str, podcast_admin: str | None = Cookie(default=None)):
    """立即发布双人口播到小宇宙（次日 07:00 或按 schedule 配置）。"""
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from webapp.publish_service import (
        get_episode_dir,
        current_episode_status,
        episode_flow_type,
        claim_episode_for_publish,
        run_publish_job,
        update_episode_state,
        STATUS_PENDING,
    )

    try:
        episode_dir = get_episode_dir(episode_id)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    flow_type = episode_flow_type(episode_dir)
    if flow_type != "dual_host":
        return JSONResponse({"error": f"不是双人口播节目: {episode_id}（flow_type={flow_type}）"}, status_code=400)

    status = current_episode_status(episode_dir)
    # dual_host: draft_ready/draft_assets_ready with audio present → treat as publishable
    PUBLISHABLE = (STATUS_PENDING, "audio_ready", "pending_confirmation", "draft_ready", "")
    if status not in PUBLISHABLE:
        return JSONResponse({"error": f"单集状态不可发布: {status or 'unknown'}"}, status_code=409)

    # 非 pending_confirmation 状态先提升，让 claim 通过
    if status != STATUS_PENDING:
        # Verify audio exists before promoting
        from webapp.publish_service import load_record
        record = load_record(episode_dir)
        audio_path = str(record.get("音频路径") or "").strip()
        if not audio_path or not Path(audio_path).exists():
            # Also check for any mp3 in dir
            mp3s = list(episode_dir.glob("*.mp3"))
            if not mp3s:
                return JSONResponse({"error": "音频文件不存在，无法发布"}, status_code=409)
        update_episode_state(episode_dir, STATUS_PENDING)

    try:
        claim_episode_for_publish(
            episode_dir,
            confirmer="admin-webapp",
            command_text=f"后台双人口播一键发布 {episode_id}",
        )
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)

    asyncio.create_task(run_publish_job(episode_dir, confirmer="admin-webapp"))
    return JSONResponse({"ok": True, "episode_id": episode_id, "status": "publishing"})


@router.post("/admin/dual-episodes/{episode_id}/generate-cover-title")
async def dual_generate_cover_title(episode_id: str, podcast_admin: str | None = Cookie(default=None)):
    """生成封面（Gemini→MiniMax 降级）并渲染标题到底图。"""
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    from webapp.publish_service import get_episode_dir
    try:
        episode_dir = get_episode_dir(episode_id)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    return await _spawn_dual_op(
        episode_id, "dual_cover_title",
        ["gen-cover-title", "--episode", episode_id],
    )


@router.get("/admin/pipeline/logs/{flow}")
async def stream_logs(flow: str, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if flow not in LOG_FILES:
        return JSONResponse({"error": "Unknown flow"}, status_code=400)

    log_path = LOG_FILES[flow]

    async def generate():
        # Send last 80 lines first
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-80:]:
                yield f"data: {line}\n\n"
        else:
            yield "data: (日志文件尚不存在)\n\n"

        # Tail new lines
        last_size = log_path.stat().st_size if log_path.exists() else 0
        for _ in range(300):  # ~5 minutes of tailing
            await asyncio.sleep(1)
            if not log_path.exists():
                continue
            current_size = log_path.stat().st_size
            if current_size > last_size:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    f.seek(last_size)
                    new_content = f.read()
                for line in new_content.splitlines():
                    if line.strip():
                        yield f"data: {line}\n\n"
                last_size = current_size
        yield "data: [log stream ended]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/admin/pipeline/status/{flow}")
async def pipeline_status(flow: str, podcast_admin: str | None = Cookie(default=None)):
    if not _is_admin(podcast_admin):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    pid_file = Path(f"/tmp/podcast_pipeline_{flow}.pid")
    running = False
    pid = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            running = True
        except Exception:
            pid_file.unlink(missing_ok=True)
            pid = None

    # Last run time from log
    log_path = LOG_FILES.get(flow)
    last_run = ""
    if log_path and log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if "开始执行" in line:
                last_run = line[:20]
                break

    return JSONResponse({"flow": flow, "running": running, "pid": pid, "last_run": last_run})
