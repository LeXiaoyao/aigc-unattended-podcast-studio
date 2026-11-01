#!/usr/bin/env python3
"""Gemini 网页端封面生成 — 通过 JXA 控制已登录的 Chrome。

用法:
  python3 gemini_web_cover.py <output_image_path> "<prompt_text>"

原理:
  调用 gemini_jxa_cover.jxa，在用户已登录的 Chrome 里打开 Gemini，
  输入提示词，等待图片生成，抓取 base64 数据，写入输出文件。
  不需要额外登录，复用 Chrome 现有登录状态。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent)
JXA_SCRIPT = ROOT_DIR / "gemini_jxa_cover.jxa"


def _normalize_prompt(prompt_text: str) -> str:
    """Keep the browser prompt compact enough for reliable Gemini web input."""
    prompt_text = " ".join((prompt_text or "").split())
    return prompt_text[:2200]


def generate_cover_via_web(prompt_text: str, output_path: Path, *, headless: bool = False) -> bool:
    """
    Generate a cover image via Gemini web UI using Chrome JXA automation.
    Returns True on success, False on failure.
    headless param is ignored (JXA always uses real Chrome).
    """
    if not JXA_SCRIPT.exists():
        print(f"[gemini-web] JXA 脚本不存在: {JXA_SCRIPT}", file=sys.stderr)
        return False

    prompt = _normalize_prompt(prompt_text)
    print(f"[gemini-web] 调用 JXA 生成封面, prompt={prompt[:80]}...", file=sys.stderr)

    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", str(JXA_SCRIPT),
             str(output_path), prompt],
            capture_output=True, text=True, timeout=420,
        )
    except subprocess.TimeoutExpired:
        print("[gemini-web] 超时（420秒）", file=sys.stderr)
        return False

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0 or not stdout.startswith("BASE64:"):
        print(f"[gemini-web] JXA 失败 (code={result.returncode}): {stderr or stdout}", file=sys.stderr)
        return False

    # Decode base64 and write image
    b64_data = stdout[len("BASE64:"):]
    try:
        img_bytes = base64.b64decode(b64_data)
    except Exception as exc:
        print(f"[gemini-web] base64 解码失败: {exc}", file=sys.stderr)
        return False

    # Determine extension from magic bytes
    if img_bytes[:4] == b'\xff\xd8\xff\xe0' or img_bytes[:4] == b'\xff\xd8\xff\xe1':
        img_type = "jpeg"
    elif img_bytes[:4] == b'\x89PNG':
        img_type = "png"
    elif img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
        img_type = "webp"
    else:
        img_type = "png"
    if output_path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        output_path = output_path.with_suffix(f".{img_type}")

    output_path.write_bytes(img_bytes)
    print(f"[gemini-web] 封面已保存: {output_path} ({len(img_bytes):,} bytes)", file=sys.stderr)
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Gemini 网页端封面生成（JXA/Chrome）")
    parser.add_argument("output", nargs="?", help="输出图片路径")
    parser.add_argument("prompt_text", nargs="?", help="完整提示词")
    parser.add_argument("--setup", action="store_true", help="（已废弃，无需登录设置）")
    args = parser.parse_args()

    if args.setup:
        print("使用 JXA 方案，复用 Chrome 已有登录状态，无需单独 setup。")
        print("请确保 Chrome 已打开并登录 Google 账号即可。")
        sys.exit(0)

    if not args.output or not args.prompt_text:
        parser.print_help()
        sys.exit(1)

    output_path = Path(args.output)
    success = generate_cover_via_web(args.prompt_text, output_path)
    if success:
        print(f"成功: {output_path}")
        sys.exit(0)
    else:
        print("生成失败", file=sys.stderr)
        sys.exit(1)
