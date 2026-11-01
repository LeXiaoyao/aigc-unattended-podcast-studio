#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "solo_review"))

from review_daily_news import ReviewBlocked, run  # noqa: E402


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_episode(root: Path, episode_id: str, *, search: object | None = None, script: str = "", research: object | None = None) -> Path:
    episode_dir = root / "episodes" / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)
    if search is not None:
        write_json(episode_dir / "search_results.json", search)
    if research is not None:
        write_json(episode_dir / "research.json", research)
    (episode_dir / "podcast_script.txt").write_text(script, encoding="utf-8")
    (episode_dir / "shownotes.txt").write_text(script[:120], encoding="utf-8")
    (episode_dir / "research.md").write_text(script[:120], encoding="utf-8")
    return episode_dir


def test_review_core_cases() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        episodes_root = tmp_path / "episodes"
        make_episode(
            tmp_path,
            "20260423E1",
            search=[],
            research={"trend_titles": ["Acme Model X 发布"]},
            script="上一期我们讲过 Acme Model X 发布，以及它对 Agent 工作流的影响。",
        )
        search = [
            {
                "title": "OpenAI releases a new AI agent SDK",
                "url": "https://openai.com/news/agent-sdk",
                "source": "OpenAI",
                "published_at": "2026-04-24T01:00:00Z",
                "summary": "OpenAI released a new AI agent SDK for production workflows.",
                "score": 0.95,
                "selected": False,
            },
            {
                "title": "Acme Model X launch",
                "url": "https://example.com/acme-x-1",
                "source": "Example",
                "summary": "Acme Model X launch details for AI agents.",
                "score": 0.8,
                "selected": True,
            },
            {
                "title": "Acme Model X release details",
                "url": "https://example.com/acme-x-2",
                "source": "Example",
                "summary": "More Acme Model X launch details for AI agents.",
                "score": 0.7,
                "selected": True,
            },
            {
                "title": "Filtered item without reason",
                "url": "https://example.com/no-reason",
                "source": "Example",
                "summary": "Small AI note.",
                "score": 0.2,
                "selected": False,
            },
        ]
        make_episode(
            tmp_path,
            "20260424E1",
            search=search,
            research={"trend_titles": ["Acme Model X launch", "Acme Model X release details"], "sources": []},
            script="今天第一条是 Acme Model X launch。第二条还是 Acme Model X release details，都是 Agent 工作流相关。",
        )
        payload = run(["--episode-id", "20260424E1", "--episodes-root", str(episodes_root), "--no-write-report"])
        suspicious = payload["overview"]["suspicious"]
        assert suspicious["high_score_rejected"], "高分未入选新闻应被识别"
        assert suspicious["missing_reject_reason"], "reject_reason 缺失应被识别"
        assert payload["internal_duplicates"], "入选新闻重复应被识别"
        assert payload["recent_duplicates"], "近几期重复主题应被识别"


def test_blocked_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        episode_dir = tmp_path / "episodes" / "20260425E1"
        episode_dir.mkdir(parents=True, exist_ok=True)
        write_json(episode_dir / "research.json", {"trend_titles": []})
        (episode_dir / "podcast_script.txt").write_text("draft", encoding="utf-8")
        try:
            run(["--episode-id", "20260425E1", "--episodes-root", str(tmp_path / "episodes"), "--no-write-report"])
        except ReviewBlocked as exc:
            assert exc.blocked_path.exists(), "缺少关键文件时应生成 BLOCKED 报告"
            exc.blocked_path.unlink()
        else:
            raise AssertionError("缺少关键文件时不应伪装成功")


def main() -> None:
    test_review_core_cases()
    test_blocked_report()
    print("test_news_review passed")


if __name__ == "__main__":
    main()
