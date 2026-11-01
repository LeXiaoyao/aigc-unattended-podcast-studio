#!/usr/bin/env python3

import json
import os
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types
from google.genai import errors as genai_errors


def main() -> None:
    if len(sys.argv) < 3:
        raise RuntimeError("Usage: gemini_genai_cover.py <output_path> <prompt>")

    output_path = Path(sys.argv[1])
    prompt = sys.argv[2]
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY")

    client = genai.Client(api_key=api_key)
    notes = []
    saved = False
    last_response = None
    last_error = None
    # 外层 3 次尝试：对 503/Deadline 等容量类错误做 30s / 60s 退避重试
    # （Paid Tier 1 的 gemini-2.5-flash-image 仍是 preview，无 SLA，容量抖动常见）
    retry_backoff = [30, 60]
    for attempt in range(1 + len(retry_backoff)):
        try:
            # 内层 3 次：模型有时返回空 parts（安全过滤/采样失败），非服务端错误
            for _ in range(3):
                response = client.models.generate_content(
                    model="gemini-2.5-flash-image",
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                    ),
                )
                last_response = response
                for candidate in response.candidates or []:
                    content = getattr(candidate, "content", None)
                    parts = getattr(content, "parts", None) or []
                    for part in parts:
                        inline_data = getattr(part, "inline_data", None)
                        if inline_data and getattr(inline_data, "data", None):
                            output_path.write_bytes(inline_data.data)
                            saved = True
                        if getattr(part, "text", None):
                            notes.append(part.text)
                if saved:
                    break
            if saved:
                break
        except genai_errors.ServerError as exc:
            last_error = exc
            status = getattr(exc, "status_code", None)
            if status in (500, 502, 503, 504) and attempt < len(retry_backoff):
                wait = retry_backoff[attempt]
                print(f"[gemini-cover] 第{attempt+1}次失败 (HTTP {status}), {wait}s 后重试", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except genai_errors.APIError as exc:
            # 非 5xx（例如 4xx 配置问题）不重试
            raise
        # 内层循环走完但未 saved（空响应），跳出外层
        break

    if not saved:
        detail = last_error if last_error is not None else last_response
        raise RuntimeError(f"No image data returned: {detail}")

    print(json.dumps({"model": "gemini-2.5-flash-image", "notes": "\n".join(notes).strip()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
