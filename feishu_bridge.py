#!/usr/bin/env python3
"""飞书长连接桥：让手机端飞书消息能到达本地 Mac。

本机没有公网 IP，飞书云端无法回调 webapp 的 /feishu/events。
这里用飞书官方 lark-oapi SDK 的 WebSocket 长连接模式接收事件，
再原样转发给本地 webapp（端口 8765），复用已有的发布确认逻辑：

    手机飞书发"确认发布小宇宙 20260611E1"
      → 飞书云端 → 长连接 → 本进程 → POST localhost:8765/feishu/events
      → webapp 校验白名单/解析期号 → run_publish_job 发布

依赖环境变量（shared/config/.env.listenhub）：
    FEISHU_APP_ID / FEISHU_APP_SECRET  自建应用凭证（长连接必需）
    FEISHU_EVENT_TOKEN                 webapp 校验事件用的 token（已有）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

import lark_oapi as lark

WEBAPP_EVENTS_URL = os.environ.get(
    "FEISHU_BRIDGE_FORWARD_URL", "http://127.0.0.1:8765/feishu/events"
)


# 本机转发绝不走系统代理（Clash 等会接管 127.0.0.1 导致连接被掐断）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _forward_to_webapp(payload: dict) -> None:
    req = urllib.request.Request(
        WEBAPP_EVENTS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _DIRECT_OPENER.open(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        print(f"[bridge] webapp 响应: {body[:300]}", flush=True)


def _on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    """把 SDK 事件对象还原成 webapp /feishu/events 期望的 payload 结构。"""
    try:
        event_dict = json.loads(lark.JSON.marshal(data.event))
    except Exception as exc:
        print(f"[bridge] 事件序列化失败: {exc}", file=sys.stderr, flush=True)
        return

    payload = {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "token": os.environ.get("FEISHU_EVENT_TOKEN", ""),
            "event_id": getattr(data.header, "event_id", "") or "",
        },
        "event": event_dict,
    }
    msg = (event_dict.get("message") or {}).get("content", "")
    print(f"[bridge] 收到消息事件: {str(msg)[:120]}", flush=True)
    try:
        _forward_to_webapp(payload)
    except Exception as exc:
        print(f"[bridge] 转发 webapp 失败: {exc}", file=sys.stderr, flush=True)


def main() -> int:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        print(
            "[bridge] 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，"
            "请参考 .env.example 与 README.md 配置飞书自建应用凭证后写入 .env",
            file=sys.stderr,
        )
        return 1

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .build()
    )
    client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    print("[bridge] 启动飞书长连接...", flush=True)
    client.start()  # 阻塞运行，断线自动重连
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
