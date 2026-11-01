#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .feed_discovery import fetch_account_items
    from .topic_compare import (
        build_our_context,
        build_report_markdown,
        compare_competitor_topics,
        latest_episode_id,
        write_suggested_review_file,
    )
    from .simple_yaml import parse_simple_yaml
    from .topic_keywords import enrich_topic_item
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from feed_discovery import fetch_account_items
    from topic_compare import (
        build_our_context,
        build_report_markdown,
        compare_competitor_topics,
        latest_episode_id,
        write_suggested_review_file,
    )
    from simple_yaml import parse_simple_yaml
    from topic_keywords import enrich_topic_item


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EPISODES_ROOT = PROJECT_ROOT / "episodes"
_OWN_PID = os.environ.get("XIAOYUZHOU_PODCAST_PID", "your_podcast_pid").strip() or "your_podcast_pid"


DEFAULT_CONFIG: dict[str, Any] = {
    "competitors": [
        {
            "id": "vivi_podcast",
            "name": "Vivi Podcast",
            "homepage": "https://claw.vivi.wiki/",
            "type": "website",
            "enabled": True,
        },
        {
            "id": "sheng_espresso",
            "name": "Sheng FM / Espresso",
            "homepage": "https://sheng-espresso.fireside.fm/",
            "type": "podcast",
            "enabled": True,
        },
        {
            "id": "xiaoyuzhou_competitor",
            "name": "小宇宙竞品账号",
            "homepage": "https://www.xiaoyuzhoufm.com/podcast/69561f756d2fb2759d4a512c",
            "type": "podcast",
            "enabled": True,
        },
    ],
    "own_podcast": {
        "id": "own_xiaoyuzhou",
        "name": "我的小宇宙播客",
        "homepage": f"https://www.xiaoyuzhoufm.com/podcast/{_OWN_PID}",
        "type": "podcast",
        "enabled": True,
        "role": "own_podcast",
    },
    "lookback_hours": 48,
    "competitor_lookback_days": 7,
    "recent_episodes_lookback": 7,
    "thresholds": {
        "topic_similarity": 0.68,
        "entity_match_bonus": 0.15,
        "suspicious_filtered_score": 70,
        "max_supplement_topics": 8,
        "max_keyword_suggestions": 20,
        "max_source_suggestions": 10,
    },
}


def resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if not path.exists():
        return config
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            config.update(loaded)
    except ImportError:
        loaded = parse_simple_yaml(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config.update(loaded)
    except Exception as exc:
        print(f"[competitor_radar] config fallback: {exc}", file=sys.stderr)
    return config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily competitor topic radar for the AI podcast.")
    parser.add_argument("--episode-id", default="", help="Episode id such as 20260424E1. Defaults to latest episode.")
    parser.add_argument("--lookback-hours", type=int, default=48, help="Competitor precise-time lookback window.")
    parser.add_argument("--competitor-lookback-days", type=int, default=7, help="Conservative scan window for undated feeds.")
    parser.add_argument("--own-lookback-episodes", type=int, default=7, help="Local own episode lookback count.")
    parser.add_argument("--episodes-root", default=str(DEFAULT_EPISODES_ROOT), help="Episodes root directory.")
    parser.add_argument("--config", default="config/competitor_radar.yaml", help="Competitor radar config path.")
    parser.add_argument("--write-report", dest="write_report", action="store_true", default=True, help="Write Markdown and JSON reports.")
    parser.add_argument("--no-write-report", dest="write_report", action="store_false", help="Do not write report files.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="Do not write suggested keyword review file.")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Allow non-report writes when explicitly enabled.")
    parser.add_argument("--update-keywords", action="store_true", default=False, help="Write pending_review suggestions when allowed.")
    parser.add_argument("--allow-keyword-update", action="store_true", default=False, help="Explicitly allow suggested_news_keywords.yaml updates.")
    return parser.parse_args(argv)


def enabled_accounts(config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    competitors = [account for account in config.get("competitors") or [] if account.get("enabled", True)]
    own = config.get("own_podcast")
    if isinstance(own, dict) and own.get("enabled", True):
        return competitors, own
    return competitors, None


def fetch_all_accounts(
    competitors: list[dict[str, Any]],
    own: dict[str, Any] | None,
    lookback_hours: int,
    competitor_lookback_days: int,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    fetched: list[dict[str, Any]] = []
    competitor_items: list[dict[str, Any]] = []
    own_items: list[dict[str, Any]] = []
    for account in competitors:
        for item in fetch_account_items(account, "competitor", lookback_hours, competitor_lookback_days):
            enriched = enrich_topic_item(item, config) if item.get("source_status") != "failed" else item
            fetched.append(enriched)
            competitor_items.append(enriched)
    if own:
        for item in fetch_account_items(own, "own_podcast", lookback_hours, competitor_lookback_days):
            fetched.append(item)
            own_items.append(item)
    return fetched, competitor_items, own_items


def write_reports(episode_id: str, payload: dict[str, Any], markdown: str) -> tuple[Path, Path]:
    reports_dir = PROJECT_ROOT / "tools" / "competitor_radar" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{episode_id}_competitor_radar.md"
    json_path = reports_dir / f"{episode_id}_competitor_radar.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(json_safe(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    return value


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    episodes_root = Path(args.episodes_root)
    config_path = resolve_project_path(args.config)
    config = load_config(config_path)
    episode_id = args.episode_id or latest_episode_id(episodes_root)
    competitors, own = enabled_accounts(config)
    fetched_items, competitor_items, own_items = fetch_all_accounts(
        competitors=competitors,
        own=own,
        lookback_hours=args.lookback_hours or int(config.get("lookback_hours") or 48),
        competitor_lookback_days=args.competitor_lookback_days or int(config.get("competitor_lookback_days") or 7),
        config=config,
    )
    context = build_our_context(
        episode_id=episode_id,
        episodes_root=episodes_root,
        own_items=own_items,
        own_lookback_episodes=args.own_lookback_episodes or int(config.get("recent_episodes_lookback") or 7),
    )
    analysis = compare_competitor_topics(competitor_items, context, config)
    keyword_file_updated = False
    if args.update_keywords:
        keyword_file_updated = write_suggested_review_file(
            PROJECT_ROOT / "config" / "suggested_news_keywords.yaml",
            analysis,
            episode_id,
            allow_update=args.allow_keyword_update,
            dry_run=args.dry_run,
        )
    payload = {
        "episode_id": episode_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_path": str(config_path),
        "dry_run": args.dry_run,
        "keyword_file_updated": keyword_file_updated,
        "competitor_items": competitor_items,
        "own_items": own_items,
        "our_context": context,
        "analysis": analysis,
    }
    markdown = build_report_markdown(episode_id, fetched_items, context, analysis)
    if args.write_report:
        md_path, json_path = write_reports(episode_id, payload, markdown)
        print(f"[competitor_radar] wrote {md_path}")
        print(f"[competitor_radar] wrote {json_path}")
    print(
        "[competitor_radar] "
        f"episode={episode_id} competitors={len(competitor_items)} "
        f"own_items={len(own_items)} misses={len(analysis.get('supplement_topics') or [])} "
        f"keyword_suggestions={len(analysis.get('keyword_suggestions') or [])}"
    )
    return payload


def main() -> None:
    try:
        run()
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
