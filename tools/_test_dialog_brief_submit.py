#!/usr/bin/env python3
"""
Dialog brief B版测试：提交 listenhub_query.dialog.txt，standard mode，轮询到完成。
用法：python tools/_test_dialog_brief_submit.py
"""
import json
import os
import sys
import time
from pathlib import Path

# 加载环境变量
env_path = Path(__file__).parent.parent / "shared/config/.env.listenhub"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

import urllib.request

API_KEY   = os.environ["LISTENHUB_API_KEY"]
SPEAKER_1 = os.environ.get("LISTENHUB_SPEAKER_1", "chat-girl-105-cn")
SPEAKER_2 = os.environ.get("LISTENHUB_SPEAKER_2", "CN-Man-Beijing-V2")
BASE_URL  = "https://api.marswave.ai/openapi/v1"

EPISODE_DIR  = Path(__file__).parent.parent / "episodes/20260525E2_test_brief"
QUERY_FILE   = EPISODE_DIR / "listenhub_query.txt"
RESULT_FILE  = EPISODE_DIR / "listenhub_create_result.json"
EPISODE_FILE = EPISODE_DIR / "listenhub_episode_result.json"


def lh_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def lh_get(path: str) -> dict:
    req = urllib.request.Request(
        BASE_URL + path,
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def submit() -> str:
    query_text = QUERY_FILE.read_text(encoding="utf-8")
    payload = {
        "query": query_text,
        "speakers": [{"speakerId": SPEAKER_1}, {"speakerId": SPEAKER_2}],
        "language": "zh",
        "mode": "quick",
    }
    print("→ 提交 B版 (standard mode)...")
    result = lh_post("/podcast/episodes", payload)
    RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    episode_id = result.get("data", {}).get("episodeId", "")
    if not episode_id:
        print(f"✗ 创建失败: {result}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ episode_id = {episode_id}")
    print(f"  https://listenhub.ai/zh/app/episode/{episode_id}")
    return episode_id


def poll(episode_id: str, timeout: int = 600) -> dict:
    print("→ 轮询生成状态 (最多 10 分钟)...")
    time.sleep(60)
    start = time.time()
    while time.time() - start < timeout:
        resp = lh_get(f"/podcast/episodes/{episode_id}")
        data = resp.get("data", {})
        status = data.get("processStatus")
        print(f"  status={status}")
        if status == "success":
            EPISODE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            return data
        if status == "failed":
            print(f"✗ 生成失败: {resp}", file=sys.stderr)
            sys.exit(1)
        time.sleep(15)
    print("✗ 超时", file=sys.stderr)
    sys.exit(1)


def summarize(data: dict) -> None:
    scripts = data.get("scripts", [])
    total_chars = sum(len(s.get("content", "")) for s in scripts)
    audio_url = data.get("audioUrl", "")
    credits = data.get("credits", "?")
    title = data.get("title", "")
    print("\n========== B版结果 ==========")
    print(f"标题   : {title}")
    print(f"脚本字数: {total_chars}")
    print(f"片段数  : {len(scripts)}")
    print(f"消耗credits: {credits}")
    print(f"音频   : {audio_url}")
    # 存 outline（前 3000 字）
    outline_lines = []
    for s in scripts[:40]:
        speaker = s.get("speakerName", "?")
        content = s.get("content", "")
        outline_lines.append(f"{speaker}：{content}")
    outline = "\n".join(outline_lines)
    (EPISODE_DIR / "listenhub_outline.md").write_text(outline, encoding="utf-8")
    print(f"\n--- 脚本预览（前 500 字）---\n{outline[:500]}")


if __name__ == "__main__":
    episode_id = submit()
    data = poll(episode_id)
    summarize(data)
    print("\n✓ 完成，结果已写入 episodes/20260525E2_test_brief/")
