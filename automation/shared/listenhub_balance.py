#!/usr/bin/env python3
"""Read the account balance from ListenHub's documented API."""

import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def get_balance():
    root = Path(__file__).resolve().parents[2]
    api_key = os.environ.get("LISTENHUB_API_KEY", "").strip()
    if not api_key:
        for rel in ("shared/config/.env.listenhub", ".env.listenhub", ".env"):
            config = root / rel
            if not config.exists():
                continue
            for line in config.read_text(encoding="utf-8", errors="ignore").splitlines():
                name, separator, value = line.partition("=")
                if separator and name.strip() == "LISTENHUB_API_KEY":
                    api_key = value.strip().strip("\"'")
                    break
            if api_key:
                break
    if not api_key:
        raise RuntimeError("ListenHub 积分查询密钥未配置")

    request = urllib.request.Request(
        "https://api.marswave.ai/openapi/v1/user/subscription",
        headers={"Authorization": "Bearer " + api_key},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
            if payload.get("code") != 0:
                raise RuntimeError("积分查询失败，服务返回码：" + str(payload.get("code")))
            balance = payload.get("data", {}).get("totalAvailableCredits")
            if type(balance) not in (int, float) or balance < 0:
                raise RuntimeError("积分查询未返回有效余额")
            return balance
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError("积分接口请求失败，状态码：" + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError("积分接口连接失败，已重试") from None
        time.sleep(2 ** attempt)


if __name__ == "__main__":
    try:
        print(get_balance())
    except Exception as error:
        raise SystemExit(str(error))
