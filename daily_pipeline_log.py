"""每日 pipeline 日志。

记录从新闻获取、过滤漏斗、脚本润色、LLM 调用、质检到发布的关键阶段数据，
既写入人读 plaintext（{episode_dir}/pipeline_log.log），
也写入结构化 JSONL（{episode_dir}/pipeline_log.jsonl），
与当期 MP3 放在同一目录下，方便后续回溯与分析。

若期号目录尚未注册（例如在初始化阶段），则回退写入全局 logs/ 目录。
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
GLOBAL_LOG_DIR = ROOT_DIR / "logs"
GLOBAL_LOG_DIR.mkdir(parents=True, exist_ok=True)

# 当前活跃的期号目录（在 pipeline 入口由 solo_pipeline 设置）
_STATE_LOCK = threading.Lock()
_CURRENT_EPISODE_DIR: Path | None = None
_CURRENT_EPISODE: str | None = None


def set_current_episode(episode: str, episode_dir: Path) -> None:
    """由 pipeline 入口调用：指定本次运行的期号与输出目录。后续所有 log_* 会写入此目录。"""
    global _CURRENT_EPISODE_DIR, _CURRENT_EPISODE
    with _STATE_LOCK:
        _CURRENT_EPISODE_DIR = Path(episode_dir)
        _CURRENT_EPISODE = episode
        _CURRENT_EPISODE_DIR.mkdir(parents=True, exist_ok=True)


def clear_current_episode() -> None:
    global _CURRENT_EPISODE_DIR, _CURRENT_EPISODE
    with _STATE_LOCK:
        _CURRENT_EPISODE_DIR = None
        _CURRENT_EPISODE = None


def _log_files(episode: str | None) -> tuple[Path, Path]:
    # 优先写入当前期号目录（与 MP3 同级），否则回退到全局 logs/
    with _STATE_LOCK:
        active_dir = _CURRENT_EPISODE_DIR
    if active_dir is not None and active_dir.is_dir():
        return active_dir / "pipeline_log.log", active_dir / "pipeline_log.jsonl"

    date_key = ""
    if episode:
        digits = "".join(ch for ch in str(episode) if ch.isdigit())
        if len(digits) >= 8:
            date_key = digits[:8]
    if not date_key:
        date_key = dt.datetime.now().strftime("%Y%m%d")
    return GLOBAL_LOG_DIR / f"{date_key}_pipeline.log", GLOBAL_LOG_DIR / f"{date_key}_pipeline.jsonl"


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ": "))
        except Exception:
            return str(value)
    return str(value)


def log_stage(stage: str, *, episode: str | None = None, **fields: Any) -> None:
    """记录一个阶段事件。

    - stage: 阶段标识，例如 "funnel", "script_polish", "llm_call", "publish"
    - episode: 期号（可选，用于回退路径）
    - fields: 任意业务字段
    """
    if episode is None:
        with _STATE_LOCK:
            episode = _CURRENT_EPISODE

    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plain_path, jsonl_path = _log_files(episode)

    record: dict[str, Any] = {"ts": timestamp, "stage": stage}
    if episode:
        record["episode"] = episode
    for k, v in fields.items():
        record[k] = v

    try:
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        print(f"[daily_log] 写入 JSONL 失败: {exc}")

    header = f"[{timestamp}] {stage}"
    if episode:
        header += f" episode={episode}"
    body_lines: list[str] = []
    for k, v in fields.items():
        body_lines.append(f"  - {k}: {_format_value(v)}")
    block = header + ("\n" + "\n".join(body_lines) if body_lines else "") + "\n"
    try:
        with plain_path.open("a", encoding="utf-8") as f:
            f.write(block)
    except Exception as exc:
        print(f"[daily_log] 写入日志失败: {exc}")


def log_error(stage: str, error: BaseException, *, episode: str | None = None, **fields: Any) -> None:
    log_stage(
        stage + ".error",
        episode=episode,
        error_type=type(error).__name__,
        error_msg=str(error),
        **fields,
    )


def log_llm_call(
    *,
    task: str,
    chain: list[str],
    attempts: list[dict[str, Any]],
    success_provider: str | None,
    total_latency_ms: int | None = None,
    episode: str | None = None,
    **extra: Any,
) -> None:
    """记录一次多 provider 的 LLM 调用链。

    - task: 调用用途，如 "shownotes_bullets", "script_polish", "quality_check"
    - chain: 本次配置的 provider 顺序，如 ["Qwen-Turbo", "DeepSeek", "Gemini"]
    - attempts: 每次尝试的结果列表，元素形如
        {"provider": "Qwen-Turbo", "result": "ok|fail", "error": "...", "latency_ms": 1234}
    - success_provider: 最终成功的 provider；全部失败时为 None
    - total_latency_ms: 整体耗时
    - extra: 业务上下文，如 input_chars, output_chars, temperature

    根据 attempts 判断是否触发兜底（success 不是 chain[0] 或 attempts 中有 fail）。
    """
    fallback_triggered = False
    if success_provider is None:
        fallback_triggered = True
    elif chain and success_provider != chain[0]:
        fallback_triggered = True
    elif any(a.get("result") == "fail" for a in attempts):
        fallback_triggered = True

    log_stage(
        "llm_call",
        episode=episode,
        task=task,
        chain=chain,
        attempts=attempts,
        success_provider=success_provider or "none",
        fallback_triggered=fallback_triggered,
        total_latency_ms=total_latency_ms,
        **extra,
    )
