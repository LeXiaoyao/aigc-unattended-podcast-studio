from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.competitor_radar.competitor_radar import DEFAULT_CONFIG, enabled_accounts
from tools.competitor_radar.feed_discovery import failed_item, parse_html_episode_items
from tools.competitor_radar.topic_compare import (
    build_report_markdown,
    compare_competitor_topics,
    write_suggested_review_file,
)
from tools.competitor_radar.topic_keywords import enrich_topic_item


def make_topic(title: str, account_id: str = "vivi_podcast", account_name: str = "Vivi Podcast") -> dict:
    item = {
        "source_role": "competitor",
        "account_id": account_id,
        "account_name": account_name,
        "title": title,
        "url": f"https://example.com/{account_id}",
        "published_at": "2026-04-30T00:00:00Z",
        "summary": title,
        "shownotes": title,
        "raw_text": title,
        "topics": [],
        "entities": [],
        "keywords": [],
        "source_status": "rss",
        "fetch_error": "",
    }
    return enrich_topic_item(item, DEFAULT_CONFIG)


def make_context(today_text: str = "", recent_text: str = "", search_items: list[dict] | None = None) -> dict:
    recent = []
    if recent_text:
        recent.append(
            {
                "episode_id": "20260428E1",
                "title": "历史内容",
                "text": recent_text,
                "entities": [],
                "topics": [],
            }
        )
    return {
        "episode_id": "20260430E1",
        "today_text": today_text,
        "today_entities": [],
        "today_keywords": [],
        "selected_urls": set(),
        "search_items": search_items or [],
        "recent_episodes": recent,
        "own_reference_items": [],
        "source_files": {},
    }


def test_competitor_topic_extracts_topic_entity_keyword() -> None:
    topic = make_topic("OpenAI Codex 上线 AWS Bedrock，coding agent 进入企业云")
    assert "AI编程工具" in topic["topics"]
    assert "OpenAI" in topic["entities"]
    assert "Codex" in topic["keywords"]


def test_own_xiaoyuzhou_is_not_competitor() -> None:
    competitors, own = enabled_accounts(DEFAULT_CONFIG)
    assert own is not None
    assert own["id"] == "own_xiaoyuzhou"
    assert own.get("role") == "own_podcast"
    assert all(account["homepage"] != own["homepage"] for account in competitors)


def test_xiaoyuzhou_competitor_is_competitor() -> None:
    competitors, _ = enabled_accounts(DEFAULT_CONFIG)
    matched = [account for account in competitors if account["id"] == "xiaoyuzhou_competitor"]
    assert matched
    assert matched[0]["homepage"] == "https://www.xiaoyuzhoufm.com/podcast/69561f756d2fb2759d4a512c"


def test_topic_matching_today_is_already_covered() -> None:
    topic = make_topic("OpenAI 上 AWS Bedrock，Codex 也进入亚马逊云")
    context = make_context(today_text="今天我们已经讲过 OpenAI 登陆 AWS Bedrock，以及 Codex 对云厂商的影响。")
    analysis = compare_competitor_topics([topic], context, DEFAULT_CONFIG)
    assert analysis["items"][0]["status"] == "already_covered"


def test_topic_matching_recent_episode_is_recently_covered() -> None:
    topic = make_topic("Perplexity 推出 AI browser 新功能")
    context = make_context(recent_text="上一期我们讲过 Perplexity 的 AI browser 和搜索入口变化。")
    analysis = compare_competitor_topics([topic], context, DEFAULT_CONFIG)
    assert analysis["items"][0]["status"] == "recently_covered"


def test_topic_in_search_results_but_not_selected_is_source_filtered() -> None:
    topic = make_topic("Cursor 发布 coding agent 新定价")
    context = make_context(
        search_items=[
            {
                "title": "Cursor 发布 coding agent 新定价",
                "url": "https://news.example/cursor",
                "content": "Cursor pricing update for coding agent",
                "score": 0.4,
                "tags": [],
                "category": "developer",
                "reject_reason": "low relevance",
                "path": "ai_news.filtered_out[0]",
            }
        ]
    )
    analysis = compare_competitor_topics([topic], context, DEFAULT_CONFIG)
    assert analysis["items"][0]["status"] == "source_filtered"
    assert "tagger 漏打标签" in analysis["items"][0]["possible_problems"]


def test_topic_absent_from_search_results_is_source_gap() -> None:
    topic = make_topic("Nvidia 发布 AI hardware 新路线图")
    analysis = compare_competitor_topics([topic], make_context(), DEFAULT_CONFIG)
    assert analysis["items"][0]["status"] == "source_gap"


def test_keyword_suggestions_write_pending_review_without_main_config(tmp_path: Path) -> None:
    path = tmp_path / "suggested_news_keywords.yaml"
    analysis = {
        "keyword_suggestions": [
            {
                "keyword": "Meshy AI",
                "keyword_type": "product",
                "reason": "竞品触发",
                "triggered_by_competitor": "Vivi Podcast",
                "suggested_weight": "medium",
                "risk": "low",
            }
        ],
        "source_suggestions": [],
    }
    updated = write_suggested_review_file(path, analysis, "20260430E1", allow_update=True, dry_run=False)
    text = path.read_text(encoding="utf-8")
    assert updated is True
    assert "Meshy AI" in text
    assert 'status: "pending_review"' in text


def test_fetch_failure_does_not_block_other_topics() -> None:
    failed = failed_item(
        {
            "id": "xiaoyuzhou_competitor",
            "name": "小宇宙竞品账号",
            "homepage": "https://www.xiaoyuzhoufm.com/podcast/69561f756d2fb2759d4a512c",
        },
        "competitor",
        "blocked",
    )
    topic = make_topic("Anthropic Claude Code 发布企业版")
    analysis = compare_competitor_topics([failed, topic], make_context(), DEFAULT_CONFIG)
    statuses = [item["status"] for item in analysis["items"]]
    assert statuses == ["fetch_failed", "source_gap"]


def test_report_keeps_own_podcast_out_of_competitor_overview() -> None:
    competitor = make_topic("OpenAI Codex 更新", "xiaoyuzhou_competitor", "小宇宙竞品账号")
    own = {
        "source_role": "own_podcast",
        "account_id": "own_xiaoyuzhou",
        "account_name": "我的小宇宙播客",
        "title": "我们自己的正式发布",
        "url": "https://www.xiaoyuzhoufm.com/podcast/your_podcast_pid",
        "published_at": "",
        "summary": "",
        "shownotes": "",
        "raw_text": "",
        "topics": [],
        "entities": [],
        "keywords": [],
        "source_status": "failed",
        "fetch_error": "blocked",
    }
    analysis = compare_competitor_topics([competitor], make_context(), DEFAULT_CONFIG)
    report = build_report_markdown("20260430E1", [competitor, own], make_context(), analysis)
    overview = report.split("## 2. 竞品最新节目概览", 1)[1].split("## 3. 我们自己的近期内容基准", 1)[0]
    own_section = report.split("## 3. 我们自己的近期内容基准", 1)[1]
    assert "小宇宙竞品账号" in overview
    assert "own_xiaoyuzhou" not in overview
    assert "own_xiaoyuzhou" in own_section


def test_html_parser_skips_fireside_host_links() -> None:
    html = """
    <html><body>
      <a href="/hosts/mengyi">Mengyi</a>
      <a href="/369">又一头部鲜花电商凋零，网上「贩卖浪漫」为何如此艰难？</a>
    </body></html>
    """
    items = parse_html_episode_items(
        html,
        {"id": "sheng_espresso", "name": "Sheng FM / Espresso"},
        "competitor",
        "https://sheng-espresso.fireside.fm/",
    )
    assert [item["title"] for item in items] == ["又一头部鲜花电商凋零，网上「贩卖浪漫」为何如此艰难？"]
