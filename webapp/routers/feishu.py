"""Feishu event callback for publish confirmation."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, str(ROOT_DIR))

from webapp.publish_service import claim_episode_for_publish, pick_pending_episode, run_publish_job

router = APIRouter()

DEFAULT_CONFIRM_KEYWORDS = ("确认发布小宇宙",)
EPISODE_ID_RE = re.compile(r"(\d{8}E\d+(?:_[A-Za-z0-9_-]+)?)")

# ── 可选外部静态站发布：复用同一桥接/白名单，独立于小宇宙 E 期 ──
FR_EPISODE_RE = re.compile(r"(\d{8}F\d+)")
FR_PIPELINE = os.environ.get("FOREIGN_REPORT_PIPELINE", "")
FR_COVER_INBOX = Path(os.environ.get("FOREIGN_REPORT_COVER_INBOX") or (ROOT_DIR / "outputs" / "_cover_inbox"))
# 关键词 → 编排脚本子命令
FR_OP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "tts": ("合成外资重磅",),
    "publish": ("确认发布外资重磅",),
    "bind-cover": ("绑定封面", "绑定外资重磅封面"),
}


# ── 事件去重 ──────────────────────────────────────────────────────────
# 飞书 webhook 是 at-least-once 投递：回调没在约 1 秒内返回 200，同一条事件会
# 带【同一个 header.event_id】重投多次（实测会隔几分钟再投）。pid 锁只能挡住
# 还在并发跑的重投，挡不住上一次已跑完、锁已释放后的延迟重投，于是一条指令被
# 执行多遍。这里在入口按 event_id 幂等去重，重复事件直接短路返回。
_EVENT_DEDUP_TTL = 600.0   # 秒；飞书重试集中在几分钟内，10 分钟足够覆盖
_EVENT_DEDUP_MAX = 512
_seen_event_ids: "OrderedDict[str, float]" = OrderedDict()


def _is_duplicate_event(event_id: str) -> bool:
    """同步 check-and-mark：首见登记并返回 False，重复返回 True。

    FastAPI 异步处理跑在单一事件循环线程，且本函数内部不 await，因此 check 到
    insert 之间不会被其他请求穿插，天然无竞态。没有 event_id 时按新事件处理。
    """
    if not event_id:
        return False
    now = time.monotonic()
    # 从最早插入的一端清理过期项（OrderedDict 按插入顺序，front 即最旧）
    while _seen_event_ids:
        oldest_id = next(iter(_seen_event_ids))
        if now - _seen_event_ids[oldest_id] > _EVENT_DEDUP_TTL:
            _seen_event_ids.pop(oldest_id)
        else:
            break
    if event_id in _seen_event_ids:
        return True
    _seen_event_ids[event_id] = now
    while len(_seen_event_ids) > _EVENT_DEDUP_MAX:
        _seen_event_ids.popitem(last=False)
    return False


def _csv_env(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _configured_confirm_keywords() -> tuple[str, ...]:
    keywords = tuple(_csv_env("FEISHU_PUBLISH_CONFIRM_KEYWORDS"))
    return keywords or DEFAULT_CONFIRM_KEYWORDS


def _expected_event_token() -> str:
    return (
        os.environ.get("FEISHU_EVENT_TOKEN")
        or os.environ.get("LARK_EVENT_TOKEN")
        or os.environ.get("FEISHU_VERIFICATION_TOKEN")
        or ""
    ).strip()


def _incoming_event_token(payload: dict) -> str:
    header = payload.get("header") or {}
    return str(header.get("token") or payload.get("token") or "").strip()


def _authorized_sender_ids() -> dict[str, set[str]]:
    return {
        "open_id": _csv_env("FEISHU_ALLOWED_OPEN_IDS"),
        "user_id": _csv_env("FEISHU_ALLOWED_USER_IDS"),
        "union_id": _csv_env("FEISHU_ALLOWED_UNION_IDS"),
    }


def _sender_summary(event: dict) -> tuple[bool, str]:
    sender = event.get("sender") or {}
    sender_ids = sender.get("sender_id") or {}
    allowed = _authorized_sender_ids()
    configured = any(values for values in allowed.values())
    display = (
        str(sender.get("sender_type") or "user"),
        str(sender_ids.get("user_id") or ""),
        str(sender_ids.get("open_id") or ""),
        str(sender_ids.get("union_id") or ""),
    )
    display_text = " / ".join(part for part in display if part)
    if not configured:
        return False, display_text or "unknown"

    for key, allowed_values in allowed.items():
        incoming = str(sender_ids.get(key) or "").strip()
        if incoming and incoming in allowed_values:
            return True, display_text or incoming
    return False, display_text or "unknown"


def _extract_message_text(event: dict) -> str:
    message = event.get("message") or {}
    if str(message.get("message_type") or "").strip() != "text":
        return ""
    raw_content = message.get("content")
    if isinstance(raw_content, dict):
        text = str(raw_content.get("text") or "")
    else:
        try:
            content = json.loads(str(raw_content or ""))
        except Exception:
            return ""
        text = str(content.get("text") or "")
    text = re.sub(r"<at[^>]*>.*?</at>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:@[\w.-]+\s+)+", "", text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _notify_invalid_confirm_command(text: str) -> None:
    try:
        from mobile_notify import notify_rich
    except Exception:
        return

    notify_rich(
        "AI观测站 · 发布确认命令格式不正确",
        [
            {"label": "收到命令", "text": text or "(空文本)"},
            {"label": "正确格式", "text": "确认发布小宇宙 20260401E2"},
            {"label": "说明", "text": "发布确认必须明确指定期数，避免误发到其他待确认单集。"},
        ],
        desktop=False,
    )


def _resolve_publishable_episode(episode_id: str | None) -> Path:
    """solo 沿用 pick_pending_episode；dual_host 复刻后台「一键发布」的状态提升逻辑。"""
    from webapp.publish_service import (
        STATUS_PENDING,
        current_episode_status,
        episode_flow_type,
        get_episode_dir,
        load_record,
        update_episode_state,
    )

    if not episode_id:
        return pick_pending_episode(None)

    episode_dir = get_episode_dir(episode_id)
    if episode_flow_type(episode_dir) != "dual_host":
        return pick_pending_episode(episode_id)

    status = current_episode_status(episode_dir)
    PUBLISHABLE = (STATUS_PENDING, "audio_ready", "draft_ready", "")
    if status not in PUBLISHABLE:
        raise RuntimeError(f"单集状态不可发布: {status or 'unknown'}")
    if status != STATUS_PENDING:
        record = load_record(episode_dir)
        audio_path = str(record.get("音频路径") or "").strip()
        if (not audio_path or not Path(audio_path).exists()) and not list(episode_dir.glob("*.mp3")):
            raise RuntimeError(f"音频文件不存在，请先发 下载单集 {episode_id}")
        update_episode_state(episode_dir, STATUS_PENDING)
    return episode_dir


def _episode_already_finalized(episode_id: str | None) -> bool:
    """显式指定期号的单集是否已 publishing/published（重复确认应静默忽略）。"""
    if not episode_id:
        return False
    try:
        from webapp.publish_service import (
            STATUS_PUBLISHED,
            STATUS_PUBLISHING,
            current_episode_status,
            get_episode_dir,
        )

        status = current_episode_status(get_episode_dir(episode_id))
    except Exception:
        return False
    return status in (STATUS_PUBLISHING, STATUS_PUBLISHED)


def _parse_confirm_command(text: str) -> tuple[str, str | None]:
    if not text:
        return "none", None
    for keyword in _configured_confirm_keywords():
        bare_pattern = re.compile(rf"^{re.escape(keyword)}$", re.IGNORECASE)
        if bare_pattern.match(text):
            return "missing_episode", None

        # 分隔符可选：关键词以中文/字母结尾、期号以数字开头，"下载单集20260614E2" 与
        # "下载单集 20260614E2" 都应识别（手机端常漏打空格）。
        pattern = re.compile(rf"^{re.escape(keyword)}[\s:：]*(?P<episode>{EPISODE_ID_RE.pattern})$", re.IGNORECASE)
        match = pattern.match(text)
        if match:
            return "ok", match.group("episode")
    return "none", None


# 单口三步指令：合成TTS → 更新shownotes → 确认发布小宇宙（最后一步走 _parse_confirm_command）
# 双人指令：下载单集 → 确认发布小宇宙
OP_COMMAND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "synthesize_tts": ("合成TTS", "合成tts"),
    "regen_shownotes": ("更新shownotes", "生成shownotes", "更新Shownotes"),
    "dual_download": ("下载单集", "下载双人"),
}


def _parse_op_command(text: str) -> tuple[str, str, str | None]:
    """识别 TTS/shownotes 操作指令。返回 (status, op_name, episode_id)。"""
    if not text:
        return "none", "", None
    for op_name, keywords in OP_COMMAND_KEYWORDS.items():
        for keyword in keywords:
            if re.match(rf"^{re.escape(keyword)}$", text, re.IGNORECASE):
                return "missing_episode", op_name, None
            match = re.match(
                rf"^{re.escape(keyword)}[\s:：]*(?P<episode>{EPISODE_ID_RE.pattern})$",
                text,
                re.IGNORECASE,
            )
            if match:
                return "ok", op_name, match.group("episode")
    return "none", "", None


def _notify_op_receipt(title: str, sections: list[dict[str, str]]) -> None:
    try:
        from mobile_notify import notify_rich
    except Exception:
        return
    notify_rich(title, sections, desktop=False)


async def _handle_op_command(op_name: str, episode_id: str, sender_text: str) -> JSONResponse:
    """执行合成TTS / 更新shownotes / 下载单集 指令并回执到飞书。"""
    from webapp.routers.pipeline import (
        start_dual_download_op,
        start_regen_shownotes_op,
        start_synthesize_tts_op,
    )

    op_labels = {
        "synthesize_tts": "合成TTS",
        "regen_shownotes": "更新Shownotes",
        "dual_download": "下载单集",
    }
    runners = {
        "synthesize_tts": start_synthesize_tts_op,
        "regen_shownotes": start_regen_shownotes_op,
        "dual_download": start_dual_download_op,
    }
    op_label = op_labels[op_name]
    runner = runners[op_name]
    try:
        ok, message = await runner(episode_id)
    except Exception as exc:
        ok, message = False, str(exc)

    print(f"[feishu] {op_label} {episode_id} 启动{'成功' if ok else '失败'}: {message}", file=sys.stderr)
    if ok:
        next_hints = {
            "synthesize_tts": "完成后会推送『TTS 已完成』和终稿全文，之后可回复 确认发布小宇宙 " + episode_id,
            "regen_shownotes": "完成后会推送『Shownotes 已更新』和内容预览。",
            "dual_download": "完成后会推送『双人单集已下载』含标题和封面图，确认后回复 确认发布小宇宙 " + episode_id,
        }
        next_hint = next_hints[op_name]
        _notify_op_receipt(
            f"AI观测站 · {op_label} 已启动",
            [
                {"label": "期号", "text": episode_id},
                {"label": "状态", "text": message},
                {"label": "下一步", "text": next_hint},
            ],
        )
    else:
        _notify_op_receipt(
            f"AI观测站 · {op_label} 未执行",
            [
                {"label": "期号", "text": episode_id},
                {"label": "原因", "text": message},
            ],
        )
    return JSONResponse({"ok": True, "accepted": ok, "op": op_name, "episode_id": episode_id, "message": message})


# ─────────────────────────── 外资重磅分支 ───────────────────────────
def _extract_image_and_text(event: dict) -> tuple[str, str, str]:
    """从 image / post 消息取 (message_id, image_key, caption_text)。

    群里 @机器人 + 图片会以 post（富文本）下发，需从 content.content 里解析 img/text/at；
    纯图片则是 image 类型。非图片返回 ('','','')。
    """
    message = event.get("message") or {}
    mtype = str(message.get("message_type") or "").strip()
    message_id = str(message.get("message_id") or "").strip()
    raw = message.get("content")
    try:
        content = raw if isinstance(raw, dict) else json.loads(str(raw or ""))
    except Exception:
        return "", "", ""
    if mtype == "image":
        return message_id, str(content.get("image_key") or "").strip(), ""
    if mtype == "post":
        image_key, texts = "", []
        for line in (content.get("content") or []):
            if not isinstance(line, list):
                continue
            for el in line:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("img", "image") and not image_key:
                    image_key = str(el.get("image_key") or el.get("file_key") or "").strip()
                elif tag == "text":
                    texts.append(str(el.get("text") or ""))
        caption = re.sub(r"\s+", " ", " ".join(texts)).strip()
        return message_id, image_key, caption
    return "", "", ""


def _download_feishu_image(message_id: str, image_key: str) -> Path | None:
    """用 tenant token 下载消息里的图片资源到封面收件箱 latest.png。"""
    import urllib.request

    if not message_id or not image_key:
        return None
    try:
        from mobile_notify import _feishu_tenant_access_token
        token = _feishu_tenant_access_token()
    except Exception as exc:
        print(f"[feishu] 取 tenant token 失败: {exc}", file=sys.stderr)
        return None
    url = (
        f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
        f"/resources/{image_key}?type=image"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            data = resp.read()
    except Exception as exc:
        print(f"[feishu] 下载图片失败: {exc}", file=sys.stderr)
        return None
    FR_COVER_INBOX.mkdir(parents=True, exist_ok=True)
    dest = FR_COVER_INBOX / "latest.png"
    dest.write_bytes(data)
    print(f"[feishu] 封面已暂存 {dest} ({len(data)} bytes)", file=sys.stderr)
    return dest


def _parse_fr_command(text: str) -> tuple[str, str, str | None]:
    """识别外资重磅指令。返回 (status, op, episode_id)。op 为编排脚本子命令名。"""
    if not text:
        return "none", "", None
    for op, keywords in FR_OP_KEYWORDS.items():
        for kw in keywords:
            if re.match(rf"^{re.escape(kw)}$", text, re.IGNORECASE):
                return "missing_episode", op, None
            m = re.match(rf"^{re.escape(kw)}[\s:：]*(?P<ep>{FR_EPISODE_RE.pattern})$", text, re.IGNORECASE)
            if m:
                return "ok", op, m.group("ep")
    return "none", "", None


async def _run_fr_pipeline(op: str, episode_id: str) -> None:
    """后台跑外资重磅编排脚本并把结果回执到飞书。"""
    try:
        from mobile_notify import notify_rich
    except Exception:
        notify_rich = None  # type: ignore

    op_label = {"tts": "合成外资重磅", "publish": "确认发布外资重磅", "bind-cover": "绑定封面"}[op]
    args = [_python_cmd_fr(), FR_PIPELINE, "-e", episode_id, op]
    if op == "bind-cover":
        src = FR_COVER_INBOX / "latest.png"
        if not src.exists():
            if notify_rich:
                notify_rich(f"外资重磅 · {op_label} 未执行",
                            [{"label": "期号", "text": episode_id},
                             {"label": "原因", "text": "封面收件箱为空，请先把封面图发到群里再绑定"}], desktop=False)
            return
        args += ["--src", str(src)]

    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out_b, _ = await proc.communicate()
    out = (out_b or b"").decode("utf-8", errors="replace").strip()
    tail = "\n".join(out.splitlines()[-12:])
    ok = proc.returncode == 0
    print(f"[feishu] FR {op} {episode_id} rc={proc.returncode}", file=sys.stderr)
    if notify_rich:
        next_hint = {
            "tts": f"完成后可发 确认发布外资重磅 {episode_id}（需封面已绑定）",
            "publish": "已 push 到生产仓库，几分钟后 GitHub Pages 生效",
            "bind-cover": f"封面已入库，可发 确认发布外资重磅 {episode_id}",
        }[op]
        notify_rich(
            f"外资重磅 · {op_label} {'已完成' if ok else '失败'}",
            [{"label": "期号", "text": episode_id},
             {"label": "结果", "text": ("成功" if ok else "失败") + f"（rc={proc.returncode}）"},
             {"label": "日志", "text": tail or "(无输出)"},
             {"label": "下一步", "text": next_hint if ok else "查看日志后重试"}],
            desktop=False,
        )


def _python_cmd_fr() -> str:
    return sys.executable or "python3"


async def _handle_fr_command(op: str, episode_id: str) -> JSONResponse:
    op_label = {"tts": "合成外资重磅", "publish": "确认发布外资重磅", "bind-cover": "绑定封面"}[op]
    try:
        from mobile_notify import notify_rich
        notify_rich(f"外资重磅 · {op_label} 已启动",
                    [{"label": "期号", "text": episode_id},
                     {"label": "状态", "text": "后台执行中，完成后回执"}], desktop=False)
    except Exception:
        pass
    asyncio.create_task(_run_fr_pipeline(op, episode_id))
    return JSONResponse({"ok": True, "accepted": True, "op": f"fr_{op}", "episode_id": episode_id})


@router.post("/feishu/events")
async def feishu_events(request: Request):
    payload = await request.json()
    expected_token = _expected_event_token()
    if not expected_token:
        print("[feishu] event token not configured", file=sys.stderr)
        return JSONResponse({"error": "event token not configured"}, status_code=503)

    if payload.get("type") == "url_verification":
        incoming_token = _incoming_event_token(payload)
        if expected_token and incoming_token and incoming_token != expected_token:
            print("[feishu] url_verification invalid token", file=sys.stderr)
            return JSONResponse({"error": "invalid token"}, status_code=403)
        return JSONResponse({"challenge": payload.get("challenge", "")})

    incoming_token = _incoming_event_token(payload)
    if expected_token and incoming_token != expected_token:
        print("[feishu] invalid event token", file=sys.stderr)
        return JSONResponse({"error": "invalid token"}, status_code=403)

    header = payload.get("header") or {}
    event_type = str(header.get("event_type") or "")
    if event_type != "im.message.receive_v1":
        print(f"[feishu] ignored unsupported event: {event_type or 'unknown'}", file=sys.stderr)
        return JSONResponse({"ok": True, "ignored": "unsupported_event"})

    # 幂等去重：同一条指令被飞书重投时，只有首次真正执行，重复的直接短路。
    event_id = str(header.get("event_id") or "")
    if _is_duplicate_event(event_id):
        print(f"[feishu] 跳过重复事件 event_id={event_id}", file=sys.stderr)
        return JSONResponse({"ok": True, "ignored": "duplicate_event", "event_id": event_id})

    event = payload.get("event") or {}
    is_allowed, sender_text = _sender_summary(event)
    if not is_allowed:
        print(f"[feishu] 忽略未授权用户的发布指令: {sender_text}", file=sys.stderr)
        return JSONResponse({"ok": True, "ignored": "unauthorized"})

    # 外资重磅：@机器人 + 封面图（image 或 post）→ 下载暂存；带期号则自动入库改名+裁切
    fr_msg_id, fr_image_key, fr_caption = _extract_image_and_text(event)
    if fr_image_key:
        saved = _download_feishu_image(fr_msg_id, fr_image_key)
        ep_m = FR_EPISODE_RE.search(fr_caption or "")
        try:
            from mobile_notify import notify_rich
            if saved and ep_m:
                ep = ep_m.group(1)
                notify_rich("外资重磅 · 封面已收到，自动入库中",
                            [{"label": "期号", "text": ep},
                             {"label": "状态", "text": f"正在改名为 {ep}.png 并裁成 3000×3000"}], desktop=False)
                asyncio.create_task(_run_fr_pipeline("bind-cover", ep))
            elif saved:
                notify_rich("外资重磅 · 封面已暂存",
                            [{"label": "状态", "text": "已收到封面图（消息里没带期号）"},
                             {"label": "下一步", "text": "发『绑定封面 20260626F28』入库；或下次 @机器人 时把期号写进同一条消息即自动入库"}], desktop=False)
            else:
                notify_rich("外资重磅 · 封面接收失败",
                            [{"label": "原因", "text": "下载图片失败，检查 im:resource 权限/网络"}], desktop=False)
        except Exception:
            pass
        return JSONResponse({"ok": True, "accepted": bool(saved), "op": "fr_cover", "episode_id": ep_m.group(1) if ep_m else ""})

    text = _extract_message_text(event)
    print(f"[feishu] 收到消息 sender={sender_text} text={text or '(empty)'}", file=sys.stderr)

    # 外资重磅指令优先于小宇宙 E 期解析（F 期 ID，互不干扰）
    fr_status, fr_op, fr_episode = _parse_fr_command(text)
    if fr_status == "missing_episode":
        try:
            from mobile_notify import notify_rich
            notify_rich("外资重磅 · 指令缺少期号",
                        [{"label": "收到命令", "text": text},
                         {"label": "正确格式", "text": "合成外资重磅 20260626F28 / 确认发布外资重磅 20260626F28 / 绑定封面 20260626F28"}], desktop=False)
        except Exception:
            pass
        return JSONResponse({"ok": True, "accepted": False, "reason": "fr_missing_episode_id"})
    if fr_status == "ok":
        return await _handle_fr_command(fr_op, fr_episode or "")

    op_status, op_name, op_episode = _parse_op_command(text)
    if op_status == "missing_episode":
        _notify_op_receipt(
            "AI观测站 · 指令缺少期号",
            [
                {"label": "收到命令", "text": text},
                {"label": "正确格式", "text": "合成TTS 20260612E1 / 更新shownotes 20260612E1 / 下载单集 20260612E2"},
            ],
        )
        return JSONResponse({"ok": True, "accepted": False, "reason": "missing_episode_id"})
    if op_status == "ok":
        return await _handle_op_command(op_name, op_episode or "", sender_text)

    parse_status, episode_id = _parse_confirm_command(text)
    print(
        f"[feishu] 解析命令结果 status={parse_status} episode={episode_id or '-'}",
        file=sys.stderr,
    )
    if parse_status == "missing_episode":
        _notify_invalid_confirm_command(text)
        return JSONResponse({"ok": True, "accepted": False, "reason": "missing_episode_id"})

    if parse_status != "ok":
        print("[feishu] 忽略未命中确认关键词的消息", file=sys.stderr)
        return JSONResponse({"ok": True, "ignored": "keyword_not_matched"})

    try:
        episode_dir = _resolve_publishable_episode(episode_id)
        record = claim_episode_for_publish(
            episode_dir,
            confirmer=sender_text,
            command_text=text,
        )
    except Exception as exc:
        # 已发布/正在发布的单集重复收到确认指令时，属于幂等无操作（飞书事件重投或盲发重发），
        # 静默忽略，不要往群里推「发布确认未执行」制造噪音。
        if _episode_already_finalized(episode_id):
            print(
                f"[feishu] 忽略已发布单集的重复确认 episode={episode_id or '-'}",
                file=sys.stderr,
            )
            return JSONResponse({"ok": True, "ignored": "already_published"})
        print(f"[feishu] 发布确认失败: {exc}", file=sys.stderr)
        # 手机端盲发指令需要回执，失败原因推回飞书群
        try:
            from mobile_notify import notify_rich

            notify_rich(
                "AI观测站 · 发布确认未执行",
                [
                    {"label": "收到命令", "text": text},
                    {"label": "原因", "text": str(exc)},
                ],
                desktop=False,
            )
        except Exception:
            pass
        return JSONResponse({"ok": True, "accepted": False, "reason": str(exc)})

    print(f"[feishu] 已接受发布确认: {episode_dir.name}", file=sys.stderr)
    asyncio.create_task(run_publish_job(episode_dir, confirmer=sender_text))
    return JSONResponse(
        {
            "ok": True,
            "accepted": True,
            "episode_id": episode_dir.name,
            "title": record.get("title") or "",
            "status": "publishing",
        }
    )
