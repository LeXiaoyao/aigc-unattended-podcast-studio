#!/usr/bin/env python3
"""Unified desktop + mobile notifications."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def _feishu_webhook_and_secret() -> tuple[str, str]:
    webhook = (
        os.environ.get("FEISHU_WEBHOOK_URL")
        or os.environ.get("LARK_WEBHOOK_URL")
        or ""
    ).strip()
    secret = (
        os.environ.get("FEISHU_BOT_SECRET")
        or os.environ.get("LARK_BOT_SECRET")
        or ""
    ).strip()
    return webhook, secret


def _sign_feishu_payload(payload: dict[str, object], secret: str) -> dict[str, object]:
    if not secret:
        return payload
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    sign = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    signed_payload = dict(payload)
    signed_payload["timestamp"] = timestamp
    signed_payload["sign"] = sign
    return signed_payload


# 飞书 API 强制直连：本机代理（Clash 等）会掐断到 open.feishu.cn 的请求，
# 而 launchd/webapp 进程无代理环境一直直连正常（2026-06-12 实测）。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post_to_feishu(payload: dict[str, object]) -> None:
    webhook, secret = _feishu_webhook_and_secret()
    if not webhook:
        return

    req = urllib.request.Request(
        webhook,
        data=json.dumps(
            _sign_feishu_payload(payload, secret),
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _DIRECT_OPENER.open(req, timeout=15) as resp:
        response_text = resp.read().decode("utf-8", errors="replace")
        if response_text:
            data = json.loads(response_text)
            if data.get("code") not in (0, None):
                raise RuntimeError(f"飞书返回失败: {response_text}")


def _feishu_tenant_access_token() -> str:
    """用自建应用凭证换 tenant_access_token（上传图片需要，webhook 本身不行）。"""
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        return ""
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _DIRECT_OPENER.open(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
    return str(data.get("tenant_access_token", ""))


def _feishu_upload_image(image_path: str, token: str) -> str:
    """上传图片换 image_key（需要应用开通 im:resource 权限）。"""
    boundary = f"----feishu{int(time.time() * 1000)}"
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n',
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="image"; filename="{os.path.basename(image_path)}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            image_bytes,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/images",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with _DIRECT_OPENER.open(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"图片上传失败: {data}")
    return str(data["data"]["image_key"])


def notify_feishu_image(image_path: str, caption: str = "") -> bool:
    """推送图片到飞书群（封面审稿用）。失败时降级为文字提示，返回是否真的发出了图片。"""
    webhook, _secret = _feishu_webhook_and_secret()
    if not webhook or not os.path.exists(image_path):
        return False
    try:
        token = _feishu_tenant_access_token()
        if not token:
            raise RuntimeError("缺少 FEISHU_APP_ID/FEISHU_APP_SECRET，无法上传图片")
        image_key = _feishu_upload_image(image_path, token)
        if caption:
            _post_to_feishu({"msg_type": "text", "content": {"text": caption}})
        _post_to_feishu({"msg_type": "image", "content": {"image_key": image_key}})
        return True
    except Exception as exc:
        print(f"[notify] 飞书图片推送失败（降级为文字）: {exc}", file=sys.stderr)
        try:
            _post_to_feishu(
                {
                    "msg_type": "text",
                    "content": {"text": f"{caption}\n（封面图片推送失败，请在后台查看 {image_path}）".strip()},
                }
            )
        except Exception:
            pass
        return False


def notify_desktop(title: str, body: str) -> None:
    safe_title = _escape_applescript(title)
    safe_body = _escape_applescript(body)
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_body}" with title "{safe_title}"'],
        check=False,
    )


def notify_feishu(title: str, body: str) -> None:
    webhook, _secret = _feishu_webhook_and_secret()
    if not webhook:
        return

    text = f"{title}\n{body}".strip()
    payload: dict[str, object] = {
        "msg_type": "text",
        "content": {"text": text},
    }
    try:
        _post_to_feishu(payload)
    except urllib.error.URLError as exc:
        print(f"[notify] 飞书通知失败: {exc}", file=sys.stderr)
    except RuntimeError as exc:
        print(f"[notify] {exc}", file=sys.stderr)


def _sections_to_plain_text(title: str, sections: list[dict[str, str]]) -> str:
    lines = [title]
    for section in sections:
        label = str(section.get("label", "")).strip()
        text = str(section.get("text", "")).strip()
        href = str(section.get("href", "")).strip()
        value = text or href
        if not value:
            continue
        if label:
            lines.append(f"{label}：{value}")
        else:
            lines.append(value)
    return "\n".join(lines).strip()


def notify_feishu_post(title: str, sections: list[dict[str, str]]) -> bool:
    webhook, _secret = _feishu_webhook_and_secret()
    if not webhook:
        return False

    content_lines: list[list[dict[str, object]]] = []
    for section in sections:
        line: list[dict[str, object]] = []
        label = str(section.get("label", "")).strip()
        text = str(section.get("text", "")).strip()
        href = str(section.get("href", "")).strip()
        if label:
            line.append({"tag": "text", "text": f"【{label}】"})
        if href:
            line.append({"tag": "a", "text": text or href, "href": href})
        elif text:
            line.append({"tag": "text", "text": text})
        if line:
            content_lines.append(line)

    if not content_lines:
        return False

    payload: dict[str, object] = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": content_lines,
                }
            }
        },
    }
    try:
        _post_to_feishu(payload)
        return True
    except urllib.error.URLError as exc:
        print(f"[notify] 飞书富文本通知失败: {exc}", file=sys.stderr)
    except RuntimeError as exc:
        print(f"[notify] {exc}", file=sys.stderr)
    return False


def notify_feishu_long_text(title: str, text: str, *, chunk_size: int = 2800) -> bool:
    """把长文本（如完整口播稿）按段落边界分段发到飞书，每段一条 post 消息。

    飞书单条消息体有大小上限，口播稿 3000-9000 字需要分段；
    在段落边界切分，避免一句话被劈在两条消息里。
    """
    content = str(text or "").strip()
    if not content:
        return False

    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, chunk_size)
        if cut < chunk_size // 3:
            cut = remaining.rfind("\n", 0, chunk_size)
        if cut < chunk_size // 3:
            cut = remaining.rfind("。", 0, chunk_size) + 1
        if cut <= 0:
            cut = chunk_size
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    total = len(chunks)
    ok = True
    for index, chunk in enumerate(chunks, start=1):
        part_title = title if total == 1 else f"{title}（{index}/{total}）"
        if not notify_feishu_post(part_title, [{"label": "", "text": chunk}]):
            ok = False
    return ok


def notify(title: str, body: str, *, desktop: bool = True) -> None:
    if desktop:
        notify_desktop(title, body)
    notify_feishu(title, body)


def notify_rich(title: str, sections: list[dict[str, str]], *, desktop: bool = False) -> None:
    if desktop:
        summary = "；".join(
            f"{item.get('label', '').strip()} {item.get('text', '').strip()}".strip()
            for item in sections[:4]
            if item.get("label") or item.get("text")
        )
        notify_desktop(title, summary[:200] or title)
    if not notify_feishu_post(title, sections):
        notify_feishu(title, _sections_to_plain_text("", sections))


def main() -> int:
    parser = argparse.ArgumentParser(description="Send desktop + Feishu notifications")
    parser.add_argument("title")
    parser.add_argument("body")
    parser.add_argument("--no-desktop", action="store_true")
    parser.add_argument("--post-json", help="JSON file containing rich post sections")
    args = parser.parse_args()

    if args.post_json:
        sections = json.loads(Path(args.post_json).read_text(encoding="utf-8"))
        notify_rich(args.title, sections, desktop=not args.no_desktop)
    else:
        notify(args.title, args.body, desktop=not args.no_desktop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
