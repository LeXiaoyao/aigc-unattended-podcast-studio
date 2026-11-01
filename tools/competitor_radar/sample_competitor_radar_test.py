#!/usr/bin/env python3
from __future__ import annotations

try:
    from .competitor_radar import DEFAULT_CONFIG
    from .topic_compare import compare_competitor_topics
    from .topic_keywords import enrich_topic_item
except ImportError:
    from competitor_radar import DEFAULT_CONFIG
    from topic_compare import compare_competitor_topics
    from topic_keywords import enrich_topic_item


def main() -> None:
    topic = enrich_topic_item(
        {
            "source_role": "competitor",
            "account_id": "vivi_podcast",
            "account_name": "Vivi Podcast",
            "title": "OpenAI Codex 上线 AWS Bedrock",
            "url": "https://example.com",
            "published_at": "",
            "summary": "",
            "shownotes": "",
            "raw_text": "",
            "topics": [],
            "entities": [],
            "keywords": [],
            "source_status": "rss",
            "fetch_error": "",
        },
        DEFAULT_CONFIG,
    )
    context = {
        "today_text": "本期讲过 OpenAI Codex 和 AWS Bedrock。",
        "selected_urls": set(),
        "search_items": [],
        "recent_episodes": [],
    }
    analysis = compare_competitor_topics([topic], context, DEFAULT_CONFIG)
    assert analysis["items"][0]["status"] == "already_covered"
    print("sample_competitor_radar_test passed")


if __name__ == "__main__":
    main()

