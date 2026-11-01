#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


FORBIDDEN_PHRASES = [
    "今天有三条主线特别清晰",
    "下面按这六个事件展开",
    "这个事件的产业含义",
    "对产业链来说",
    "真正跑赢这一轮的不是",
]

BOUNDARY_HINTS = [
    "但这还不能",
    "接下来要看",
    "真正的难点",
    "还不能直接等同于",
    "风险在于",
    "观察点",
]

EXPLANATION_HINTS = [
    "也就是",
    "可以理解为",
    "一家",
    "一个",
    "一种",
    "高带宽",
    "自研",
    "中文",
    "简称",
]

EN_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9.+_-]{1,}\b")


def count_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def sentence_chunks(text: str) -> list[str]:
    return [chunk.strip() for chunk in re.split(r"[。！？!?；;\n]+", text) if chunk.strip()]


def find_english_risks(text: str) -> list[str]:
    risks: list[str] = []
    for chunk in sentence_chunks(text):
        tokens = EN_TOKEN_RE.findall(chunk)
        if len(tokens) < 3:
            continue
        has_explanation = any(hint in chunk for hint in EXPLANATION_HINTS)
        if not has_explanation:
            risks.append(" / ".join(tokens[:6]))
    return risks


def append_report(report_path: Path, lines: list[str]) -> None:
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    suffix = "\n\n" if existing and not existing.endswith("\n\n") else ""
    report_path.write_text(existing + suffix + "\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check solo v2 podcast script outputs.")
    parser.add_argument("episode_dir", help="Path to the episode directory.")
    args = parser.parse_args()

    episode_dir = Path(args.episode_dir)
    script_path = episode_dir / "podcast_script.txt"
    title_path = episode_dir / "title.txt"
    report_path = episode_dir / "script_quality_report.md"

    failures: list[str] = []
    warnings: list[str] = []

    for path in [script_path, title_path, report_path]:
        if not path.exists():
            failures.append(f"缺少文件：{path.name}")

    script = script_path.read_text(encoding="utf-8") if script_path.exists() else ""

    forbidden_hits = [phrase for phrase in FORBIDDEN_PHRASES if phrase in script]
    if forbidden_hits:
        failures.append("新稿包含禁用模板句：" + "、".join(forbidden_hits))

    char_count = count_chars(script)
    if script_path.exists() and not (4200 <= char_count <= 7000):
        warnings.append(f"新稿字数为 {char_count}，不在 4200–7000 目标范围内")

    english_risks = find_english_risks(script)
    if english_risks:
        warnings.append("存在连续 3 个以上英文专名可能未解释：" + "；".join(english_risks[:5]))

    boundary_count = sum(script.count(hint) for hint in BOUNDARY_HINTS)
    if script_path.exists() and boundary_count < 2:
        failures.append(f"观察边界提示词只有 {boundary_count} 处，少于 2 处")

    if failures:
        status = "FAIL"
    elif warnings:
        status = "WARN"
    else:
        status = "PASS"

    report_lines = [
        "## solo v2 本地脚本自检",
        f"- 结果：{status}",
        f"- 字数：{char_count}",
        f"- 禁用模板句命中：{('、'.join(forbidden_hits) if forbidden_hits else '无')}",
        f"- 观察边界提示词数量：{boundary_count}",
        f"- 英文专名堆叠风险：{('；'.join(english_risks[:5]) if english_risks else '无')}",
    ]
    if failures:
        report_lines.append("- FAIL：")
        report_lines.extend(f"  - {item}" for item in failures)
    if warnings:
        report_lines.append("- WARN：")
        report_lines.extend(f"  - {item}" for item in warnings)

    if report_path.exists() or episode_dir.exists():
        append_report(report_path, report_lines)

    print(status)
    for item in failures:
        print(f"FAIL: {item}")
    for item in warnings:
        print(f"WARN: {item}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
