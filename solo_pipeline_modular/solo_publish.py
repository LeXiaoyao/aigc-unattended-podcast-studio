"""Publishing-side helpers for cover retries and stage notifications."""
from __future__ import annotations

from .shared import *
from .solo_shownotes import _format_audio_summary


def _schedule_cover_retry(
    episode_dir: Path,
    *,
    provider: str,
    publish_after_retry: bool,
    delay_seconds: int | None = None,
) -> dict[str, object]:
    if delay_seconds is None:
        delay_seconds = SOLO_COVER_RETRY_DELAY_SECONDS
    meta_path = _cover_retry_meta_path(episode_dir)
    now = now_local()
    run_at = now + dt.timedelta(seconds=delay_seconds)
    existing = read_json(meta_path) if meta_path.exists() else {}
    if (
        existing.get("status") in {"scheduled", "running"}
        and str(existing.get("scheduled_at", "")).strip()
    ):
        return existing

    meta: dict[str, object] = {
        "status": "scheduled",
        "episode": episode_dir.name,
        "provider": provider,
        "publish_after_retry": publish_after_retry,
        "scheduled_at": run_at.isoformat(timespec="seconds"),
        "requested_at": now.isoformat(timespec="seconds"),
        "delay_seconds": delay_seconds,
    }
    write_json(meta_path, meta)

    log_path = ROOT_DIR / "logs" / f"cover-retry-{episode_dir.name}.log"
    with log_path.open("a", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT_DIR / "solo_pipeline.py"),
                "retry-cover",
                "--episode",
                episode_dir.name,
                "--provider",
                provider,
                "--delay-seconds",
                str(delay_seconds),
                *(
                    ["--publish-after-retry"]
                    if publish_after_retry
                    else []
                ),
            ],
            cwd=str(ROOT_DIR),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    meta["pid"] = proc.pid
    write_json(meta_path, meta)
    return meta


def _cover_brief_from_meta(episode_dir: Path) -> str:
    meta_path = episode_dir / "cover_generation.json"
    if not meta_path.exists():
        return ""
    try:
        meta = read_json(meta_path)
    except Exception:
        return ""
    prompt = str(meta.get("prompt", "")).strip()
    if not prompt:
        return ""
    match = re.search(r"优先考虑这些画面方向：(.+?)。今天新闻的核心张力是：", prompt)
    if match:
        return _trim_text(match.group(1), limit=240)
    return _trim_text(prompt, limit=240)


def _notify_episode_stage(
    *,
    stage_title: str,
    progress_text: str,
    episode: str,
    title: str,
    shownotes: str = "",
    cover_brief: str = "",
    audio_path: Path | None = None,
    next_step: str = "",
) -> None:
    shownotes_display = shownotes.strip()
    if len(shownotes_display) > 520:
        shownotes_display = shownotes_display[:517].rstrip() + "..."
    sections = [
        {"label": "当前进度", "text": progress_text},
        {"label": "期号", "text": episode},
        {"label": "标题", "text": title},
    ]
    if shownotes_display:
        sections.append({"label": "Shownotes", "text": shownotes_display})
    if cover_brief:
        sections.append({"label": "封面构图", "text": cover_brief})
    if audio_path and audio_path.exists():
        sections.append({"label": "音频", "text": _format_audio_summary(audio_path)})
    if next_step:
        sections.append({"label": "下一步", "text": next_step})
    _notify_feishu_rich(stage_title, sections)


def _replace_research_title(research_markdown: str, title: str) -> str:
    lines = research_markdown.splitlines()
    if lines and lines[0].startswith("# "):
        lines[0] = f"# {title}"
        return "\n".join(lines)
    return f"# {title}\n\n{research_markdown.strip()}"


__all__ = [name for name in globals() if not name.startswith("__")]
