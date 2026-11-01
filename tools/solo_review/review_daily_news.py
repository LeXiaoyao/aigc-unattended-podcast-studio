#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from difflib import SequenceMatcher


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EPISODES_ROOT = PROJECT_ROOT / "episodes"
REPORT_DIR = PROJECT_ROOT / "tools" / "solo_review"

REQUIRED_FILES = ("search_results.json", "research.json", "podcast_script.txt")
OPTIONAL_FILES = ("research.md", "shownotes.txt", "pipeline_log.log")

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "name", "headline"),
    "url": ("url", "link", "href", "source_url"),
    "source": ("source", "source_name", "provider", "feed_name", "site", "domain", "source_query"),
    "published_at": ("published_at", "published_date", "date", "created_at", "event_time", "published"),
    "summary": ("summary", "content", "description", "snippet", "text"),
    "tags": ("tags", "tag", "topics", "keywords"),
    "category": ("category", "bucket", "sector", "pipeline_tag"),
    "score": ("score", "rank_score", "final_score", "news_score"),
    "selected": ("selected", "is_selected", "picked", "included"),
    "reject_reason": ("reject_reason", "reason", "filter_reason", "duplicate_reason"),
    "dedupe_key": ("dedupe_key", "canonical_key", "cluster_key"),
    "importance_score": ("importance_score", "importance", "priority_score"),
    "freshness_score": ("freshness_score", "freshness", "recency_score"),
    "relevance_score": ("relevance_score", "relevance", "signal_score"),
    "risk_flags": ("risk_flags", "flags", "warnings"),
}

NEWS_ARRAY_KEYS = {
    "results",
    "tavily_results",
    "brave_results",
    "rss_results",
    "github_results",
    "hackernews_results",
    "product_hunt_results",
    "backlog_results",
    "filtered_out",
    "removed_items",
    "dropped_items",
    "backlog_added_items",
    "items",
    "sources",
}

FILTER_PATH_HINTS = (
    "filtered_out",
    "removed_items",
    "dropped_items",
    "history_filter",
    "hard_time_filter",
    "hard_history_filter",
)

KNOWN_ENTITIES = (
    "OpenAI",
    "ChatGPT",
    "Codex",
    "Anthropic",
    "Claude",
    "Google",
    "Gemini",
    "DeepMind",
    "Meta",
    "Llama",
    "Microsoft",
    "微软",
    "Azure",
    "Copilot",
    "Nvidia",
    "NVIDIA",
    "英伟达",
    "Apple",
    "苹果",
    "Siri",
    "Amazon",
    "亚马逊",
    "AWS",
    "GitHub",
    "Hugging Face",
    "DeepSeek",
    "Qwen",
    "Alibaba",
    "ByteDance",
    "Tencent",
    "Baidu",
    "华为",
    "MiniMax",
    "Manus",
    "Mistral",
    "xAI",
    "Grok",
    "Perplexity",
    "Cursor",
    "Replit",
    "Vercel",
    "TSMC",
    "AMD",
    "Intel",
    "Micron",
    "SK Hynix",
    "Samsung",
    "Scout AI",
    "Parallel Web Systems",
    "BMW",
    "小米",
    "阿里",
    "通义",
    "腾讯",
    "百度",
    "字节",
    "豆包",
    "月之暗面",
    "智谱",
    "阶跃星辰",
    "昆仑万维",
)

GENERIC_ENTITY_STOPWORDS = {
    "AI",
    "A I",
    "The",
    "This",
    "That",
    "What",
    "Why",
    "How",
    "April",
    "May",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
    "News",
    "Update",
    "Review",
    "Series",
    "Report",
    "Reuters",
    "Bloomberg",
    "Yahoo Finance",
    "TechCrunch",
    "CNBC",
}

HIGH_TRUST_DOMAINS = (
    "openai.com",
    "anthropic.com",
    "googleblog.com",
    "blog.google",
    "deepmind.google",
    "microsoft.com",
    "nvidia.com",
    "meta.com",
    "about.fb.com",
    "apple.com",
    "github.com",
    "huggingface.co",
    "techcrunch.com",
    "bloomberg.com",
    "reuters.com",
    "theverge.com",
    "wired.com",
    "cnbc.com",
    "semianalysis.com",
)

ENTITY_ALIASES = {
    "apple": "apple",
    "苹果": "apple",
    "google": "google",
    "谷歌": "google",
    "microsoft": "microsoft",
    "微软": "microsoft",
    "nvidia": "nvidia",
    "英伟达": "nvidia",
    "amazon": "amazon",
    "亚马逊": "amazon",
    "huawei": "huawei",
    "华为": "huawei",
    "deepseek": "deepseek",
    "openai": "openai",
    "anthropic": "anthropic",
    "claude": "claude",
    "meta": "meta",
    "scout ai": "scout ai",
    "parallel web systems": "parallel web systems",
}

LOW_VALUE_MATCH_TOKENS = {
    "ai",
    "news",
    "model",
    "models",
    "release",
    "released",
    "launch",
    "launches",
    "update",
    "updates",
    "agent",
    "agents",
    "人工智能",
    "模型",
    "发布",
    "推出",
    "新闻",
}

EVENT_KEYWORDS = (
    "announces",
    "announced",
    "launches",
    "launched",
    "released",
    "unveils",
    "ships",
    "open sources",
    "open-sources",
    "raises",
    "raised",
    "funding",
    "acquires",
    "acquired",
    "reports",
    "earnings",
    "forecast",
    "发布",
    "推出",
    "上线",
    "融资",
    "开源",
    "收购",
    "财报",
    "监管",
)

LOW_QUALITY_SOURCE_HINTS = (
    "press release",
    "prnewswire",
    "globenewswire",
    "businesswire",
    "sponsored",
    "advertorial",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "lookback_episodes": 7,
    "min_score_for_suspicious_reject": 70,
    "high_value_keywords": [
        "model release",
        "open source",
        "GitHub",
        "agent",
        "AI agent",
        "Claude",
        "OpenAI",
        "Google",
        "Gemini",
        "Anthropic",
        "Meta",
        "Microsoft",
        "Nvidia",
        "融资",
        "开源",
        "发布",
        "模型",
        "智能体",
        "算力",
        "芯片",
        "监管",
        "产品发布",
        "大厂动态",
        "开源爆款",
    ],
    "duplicate_similarity_threshold": 0.72,
    "recent_duplicate_threshold": 0.68,
    "max_missed_news_candidates": 10,
    "max_recent_episodes_to_scan": 7,
}


class ReviewBlocked(Exception):
    def __init__(self, episode_id: str, missing_files: list[str], blocked_path: Path) -> None:
        super().__init__(f"{episode_id} missing required files: {', '.join(missing_files)}")
        self.episode_id = episode_id
        self.missing_files = missing_files
        self.blocked_path = blocked_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review daily solo news selection quality.")
    parser.add_argument("--episode-id", required=True, help="Episode id such as 20260424E1.")
    parser.add_argument("--episodes-root", default=str(DEFAULT_EPISODES_ROOT), help="Episodes root directory.")
    parser.add_argument("--lookback", type=int, default=7, help="Recent episode count for duplication checks.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True, help="Do not update rule files.")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Allow non-report writes when explicitly implemented.")
    parser.add_argument("--write-report", dest="write_report", action="store_true", default=True, help="Write Markdown and JSON reports.")
    parser.add_argument("--no-write-report", dest="write_report", action="store_false", help="Do not write report files.")
    parser.add_argument("--no-update-rules", action="store_true", default=True, help="Keep rule/config files unchanged.")
    parser.add_argument("--config", default="config/news_review.yaml", help="Review config path.")
    return parser.parse_args(argv)


def resolve_project_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key = ""
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith(("  - ", "- ")) and current_key:
            value = stripped[2:].strip()
            if not isinstance(result.get(current_key), list):
                result[current_key] = []
            result[current_key].append(parse_scalar(value))
            continue
        if ":" in stripped and not line.startswith(" "):
            key, raw_value = stripped.split(":", 1)
            current_key = key.strip()
            raw_value = raw_value.strip()
            result[current_key] = [] if raw_value == "" else parse_scalar(raw_value)
    return result


def load_config(path: Path) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_CONFIG)
    if not path.exists():
        return config
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except ImportError:
        loaded = parse_simple_yaml(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[news_review] config fallback: {exc}", file=sys.stderr)
        loaded = {}
    if isinstance(loaded, dict):
        config.update(loaded)
    return config


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def write_blocked_report(episode_id: str, episode_dir: Path, missing_files: list[str]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    blocked_path = REPORT_DIR / f"{episode_id}_BLOCKED.md"
    lines = [
        f"# {episode_id} 单人口播新闻筛选复盘阻塞",
        "",
        "## 1. 结论",
        "- 今日结论：阻塞",
        "- 阻塞原因：缺少关键文件，不能继续猜测新闻筛选质量。",
        "- 是否建议进入 TTS：否",
        "- 是否建议发布：否",
        "",
        "## 2. 缺失文件",
    ]
    lines.extend(f"- {episode_dir / name}" for name in missing_files)
    lines.extend(
        [
            "",
            "## 3. 下一步",
            "- 先确认上游单人口播流程是否完整生成 `search_results.json`、`research.json`、`podcast_script.txt`。",
            "- 文件补齐后重新运行复盘脚本。",
        ]
    )
    blocked_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return blocked_path


def ensure_episode_ready(episode_id: str, episode_dir: Path) -> list[str]:
    missing = [name for name in REQUIRED_FILES if not (episode_dir / name).exists()]
    if missing:
        blocked_path = write_blocked_report(episode_id, episode_dir, missing)
        raise ReviewBlocked(episode_id, missing, blocked_path)
    return [name for name in OPTIONAL_FILES if not (episode_dir / name).exists()]


def value_from_aliases(item: dict[str, Any], field: str) -> Any:
    for key in FIELD_ALIASES[field]:
        if key in item and item.get(key) not in ("", None, []):
            return item.get(key)
    return None


def clean_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(clean_str(item) for item in value if clean_str(item))
    return str(value).strip()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\u4e00-\u9fff]+", " ", text.lower())).strip()


def text_tokens(text: str) -> set[str]:
    normalized = normalize_text(text)
    return {
        token
        for token in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9][a-z0-9+._-]{1,}", normalized)
        if len(token) >= 2
    }


def distinctive_tokens(text: str) -> set[str]:
    return {token for token in text_tokens(text) if token not in LOW_VALUE_MATCH_TOKENS and len(token) >= 2}


def canonical_entity(entity: str) -> str:
    key = normalize_text(entity)
    return ENTITY_ALIASES.get(key, key)


def canonical_entities(entities: list[str] | set[str]) -> set[str]:
    return {canonical_entity(entity) for entity in entities if canonical_entity(entity)}


def text_similarity(left: str, right: str) -> float:
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    seq = SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = text_tokens(left)
    right_tokens = text_tokens(right)
    if not left_tokens or not right_tokens:
        return seq
    jaccard = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    overlap = len(left_tokens & right_tokens) / max(min(len(left_tokens), len(right_tokens)), 1)
    return max(seq, jaccard, overlap * 0.82)


def domain_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def is_social_item(item: dict[str, Any]) -> bool:
    domain = (item.get("domain") or domain_from_url(item.get("url") or "")).lower()
    source = (item.get("source") or "").lower()
    return domain in {"x.com", "twitter.com"} or "follow_builder" in source


def has_clear_event(item: dict[str, Any]) -> bool:
    haystack = " ".join([item.get("title") or "", item.get("summary") or ""]).lower()
    return any(keyword in haystack for keyword in EVENT_KEYWORDS)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    return f"{netloc}{path}".lower()


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1", "selected", "picked", "included"}:
            return True
        if lowered in {"false", "no", "n", "0", "rejected", "filtered", "excluded"}:
            return False
    return None


def normalize_score(value: Any) -> tuple[float | None, float | None]:
    if value in ("", None):
        return None, None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None, None
    if math.isnan(raw):
        return None, None
    normalized = raw * 100 if 0 <= raw <= 1 else raw
    return raw, normalized


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，/|]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [clean_str(part) for part in value if clean_str(part)]
    return [clean_str(value)] if clean_str(value) else []


def path_has_filter_hint(path: str) -> bool:
    return any(hint in path for hint in FILTER_PATH_HINTS)


def is_news_like_dict(item: dict[str, Any]) -> bool:
    return bool(value_from_aliases(item, "title") or value_from_aliases(item, "url"))


def extract_raw_items(value: Any, path: str = "", bucket: str = "") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, list):
        for index, entry in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            if isinstance(entry, dict) and (is_news_like_dict(entry) or path.split(".")[-1] in NEWS_ARRAY_KEYS):
                enriched = dict(entry)
                enriched["_review_path"] = path
                if bucket and "bucket" not in enriched:
                    enriched["_review_bucket"] = bucket
                items.append(enriched)
            else:
                items.extend(extract_raw_items(entry, child_path, bucket))
        return items
    if isinstance(value, dict):
        next_bucket = bucket
        path_tail = path.split(".")[-1] if path else ""
        if path_tail in {"ai_news", "product_hunt"}:
            next_bucket = path_tail
        if is_news_like_dict(value):
            enriched = dict(value)
            enriched["_review_path"] = path
            if next_bucket and "bucket" not in enriched:
                enriched["_review_bucket"] = next_bucket
            items.append(enriched)
        for key, child in value.items():
            if key.startswith("_"):
                continue
            child_path = f"{path}.{key}" if path else key
            items.extend(extract_raw_items(child, child_path, next_bucket))
    return items


def normalized_item(raw: dict[str, Any]) -> dict[str, Any]:
    raw_score, score = normalize_score(value_from_aliases(raw, "score"))
    title = clean_str(value_from_aliases(raw, "title"))
    url = clean_str(value_from_aliases(raw, "url"))
    source = clean_str(value_from_aliases(raw, "source")) or domain_from_url(url) or clean_str(raw.get("_review_bucket"))
    published_at = clean_str(value_from_aliases(raw, "published_at"))
    summary = clean_str(value_from_aliases(raw, "summary"))
    category = clean_str(value_from_aliases(raw, "category")) or clean_str(raw.get("_review_bucket"))
    reject_reason = clean_str(value_from_aliases(raw, "reject_reason"))
    if not reject_reason and path_has_filter_hint(clean_str(raw.get("_review_path"))):
        reject_reason = clean_str(raw.get("reason")) or "filtered_by_pipeline_stage"
    tags = normalize_tags(value_from_aliases(raw, "tags"))
    if category and category not in tags:
        tags.append(category)
    item = {
        "title": title,
        "url": url,
        "source": source,
        "domain": domain_from_url(url),
        "published_at": published_at,
        "summary": summary,
        "tags": tags,
        "category": category,
        "score_raw": raw_score,
        "score": score,
        "selected_explicit": parse_bool(value_from_aliases(raw, "selected")),
        "selected": False,
        "reject_reason": reject_reason,
        "dedupe_key": clean_str(value_from_aliases(raw, "dedupe_key")),
        "importance_score": normalize_score(value_from_aliases(raw, "importance_score"))[1],
        "freshness_score": normalize_score(value_from_aliases(raw, "freshness_score"))[1],
        "relevance_score": normalize_score(value_from_aliases(raw, "relevance_score"))[1],
        "risk_flags": normalize_tags(value_from_aliases(raw, "risk_flags")),
        "review_path": clean_str(raw.get("_review_path")),
        "bucket": clean_str(raw.get("_review_bucket")) or category,
        "raw": raw,
    }
    item["entities"] = extract_entities(" ".join([title, summary, url]))
    item["topic_type"] = classify_topic_type(" ".join([title, summary]))
    return item


def merge_duplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = normalize_url(item["url"]) or normalize_text(item["title"])
        if not key:
            key = item["review_path"] + item["title"]
        if key not in merged:
            item["review_paths"] = [item["review_path"]] if item["review_path"] else []
            merged[key] = item
            continue
        current = merged[key]
        current["review_paths"].extend(path for path in [item["review_path"]] if path and path not in current["review_paths"])
        for field in ("summary", "published_at", "source", "domain", "category", "bucket", "reject_reason", "dedupe_key"):
            if not current.get(field) and item.get(field):
                current[field] = item[field]
        current["tags"] = sorted(set(current.get("tags") or []) | set(item.get("tags") or []))
        current["risk_flags"] = sorted(set(current.get("risk_flags") or []) | set(item.get("risk_flags") or []))
        current["entities"] = sorted(set(current.get("entities") or []) | set(item.get("entities") or []))
        if item.get("score") is not None and (current.get("score") is None or item["score"] > current["score"]):
            current["score"] = item["score"]
            current["score_raw"] = item["score_raw"]
        if item.get("selected_explicit") is True:
            current["selected_explicit"] = True
        if item.get("selected_explicit") is False and current.get("selected_explicit") is None:
            current["selected_explicit"] = False
    return list(merged.values())


def extract_research_context(research_json: dict[str, Any], script_text: str, shownotes_text: str, research_md: str) -> dict[str, Any]:
    selected_titles: list[str] = []
    selected_urls: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            title = clean_str(value.get("title") or value.get("name") or value.get("headline"))
            url = clean_str(value.get("url") or value.get("link"))
            if title:
                selected_titles.append(title)
            if url:
                selected_urls.add(normalize_url(url))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and len(value.strip()) >= 4:
            selected_titles.append(value.strip())

    for key in ("trend_titles", "products", "sources", "selected", "items", "stories"):
        walk(research_json.get(key))
    combined_text = "\n".join([script_text, shownotes_text, research_md, "\n".join(selected_titles)])
    return {
        "selected_titles": sorted(set(selected_titles)),
        "selected_urls": selected_urls,
        "today_text": combined_text,
        "today_entities": extract_entities(combined_text),
        "selected_title_entities": canonical_entities(extract_entities("\n".join(selected_titles))),
    }


def title_matches_selected_title(item: dict[str, Any], selected_title: str) -> bool:
    title = item.get("title") or ""
    summary = item.get("summary") or ""
    if text_similarity(title, selected_title) >= 0.68:
        return True
    item_entities = canonical_entities(item.get("entities") or extract_entities(title))
    selected_entities = canonical_entities(extract_entities(selected_title))
    if not (item_entities & selected_entities):
        return False
    item_tokens = distinctive_tokens(" ".join([title, summary]))
    selected_tokens = distinctive_tokens(selected_title)
    overlap = item_tokens & selected_tokens
    if overlap:
        return True
    if item.get("topic_type") != "general" and classify_topic_type(selected_title) == item.get("topic_type"):
        return True
    return False


def item_matches_selected_context(item: dict[str, Any], context: dict[str, Any]) -> bool:
    explicit = item.get("selected_explicit")
    if explicit is not None:
        return bool(explicit)
    url_key = normalize_url(item.get("url") or "")
    if url_key and url_key in context["selected_urls"]:
        return True
    today_text_norm = normalize_text(context["today_text"])
    title = item.get("title") or ""
    title_key = normalize_text(title)
    if title_key and len(title_key) >= 12 and len(distinctive_tokens(title)) >= 2 and title_key in today_text_norm:
        return True
    for selected_title in context["selected_titles"]:
        if title_matches_selected_title(item, selected_title):
            return True
    return False


def mark_selected(items: list[dict[str, Any]], context: dict[str, Any]) -> None:
    for item in items:
        item["selected"] = item_matches_selected_context(item, context)


def extract_entities(text: str) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    lowered = text.lower()
    for entity in KNOWN_ENTITIES:
        if entity.lower() in lowered:
            found.append(entity)
    for match in re.findall(r"\b[A-Z][A-Za-z0-9+.-]*(?:\s+[A-Z][A-Za-z0-9+.-]*){0,3}\b", text):
        match = match.strip()
        if len(match) < 3 or match in GENERIC_ENTITY_STOPWORDS:
            continue
        if any(stop == match for stop in GENERIC_ENTITY_STOPWORDS):
            continue
        found.append(match)
    for match in re.findall(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text, flags=re.I):
        found.append(match)
    unique: list[str] = []
    for entity in found:
        key = normalize_text(entity)
        if key and key not in {normalize_text(existing) for existing in unique}:
            unique.append(entity)
    return unique[:12]


def classify_topic_type(text: str) -> str:
    lowered = text.lower()
    checks = [
        ("funding", ("raises", "raised", "funding", "series a", "series b", "valuation", "融资", "估值", "投资")),
        ("model_release", ("model release", "released", "launches", "unveils", "benchmark", "模型", "发布", "推出")),
        ("open_source", ("open source", "open-source", "github", "开源")),
        ("agent", ("agent", "agentic", "智能体", "代理")),
        ("compute", ("nvidia", "gpu", "chip", "hbm", "data center", "算力", "芯片", "数据中心")),
        ("regulation", ("regulation", "regulator", "eu ai act", "policy", "监管", "法规")),
        ("product_launch", ("product", "app", "feature", "ios", "camera mode", "产品", "功能", "上线")),
        ("earnings", ("earnings", "revenue", "capex", "cloud", "财报", "营收", "资本开支")),
        ("workforce", ("jobs", "hiring", "layoffs", "workforce", "岗位", "招聘", "裁员")),
    ]
    for topic, keywords in checks:
        if any(keyword in lowered for keyword in keywords):
            return topic
    return "general"


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    match = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", raw)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def episode_date(episode_id: str) -> datetime | None:
    match = re.match(r"^(20\d{2})(\d{2})(\d{2})E\d+$", episode_id)
    if not match:
        return None
    return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)


def is_fresh(item: dict[str, Any], reference: datetime | None) -> bool:
    if reference is None:
        return False
    published = parse_datetime(item.get("published_at") or "")
    if not published:
        return False
    delta_hours = abs((reference - published.astimezone(timezone.utc)).total_seconds()) / 3600
    return delta_hours <= 72


def source_trust_score(item: dict[str, Any]) -> int:
    source_text = " ".join([item.get("source") or "", item.get("domain") or "", item.get("url") or ""]).lower()
    if any(domain in source_text for domain in HIGH_TRUST_DOMAINS):
        return 2
    if any(hint in source_text for hint in LOW_QUALITY_SOURCE_HINTS):
        return -1
    return 0


def high_value_hits(item: dict[str, Any], keywords: list[str]) -> list[str]:
    haystack = " ".join(
        [
            item.get("title") or "",
            item.get("summary") or "",
            item.get("source") or "",
            item.get("url") or "",
            " ".join(item.get("tags") or []),
        ]
    ).lower()
    return [keyword for keyword in keywords if keyword.lower() in haystack]


def score_bucket(score: float | None) -> str:
    if score is None:
        return "missing"
    if score >= 70:
        return "70-100"
    if score >= 50:
        return "50-69"
    if score >= 30:
        return "30-49"
    return "0-29"


def field_missing_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for field in FIELD_ALIASES:
        missing = 0
        for item in items:
            if field == "score":
                exists = item.get("score") is not None
            elif field == "selected":
                exists = item.get("selected_explicit") is not None
            else:
                exists = bool(item.get(field))
            if not exists:
                missing += 1
        counts[field] = missing
    return counts


def analyze_source_overview(items: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    selected_items = [item for item in items if item.get("selected")]
    rejected_items = [item for item in items if not item.get("selected")]
    source_distribution = Counter(item.get("domain") or item.get("source") or "missing" for item in items)
    tag_distribution: Counter[str] = Counter()
    for item in items:
        tags = item.get("tags") or ["missing"]
        for tag in tags:
            tag_distribution[tag or "missing"] += 1
    reject_distribution = Counter(item.get("reject_reason") or "missing" for item in rejected_items)
    score_distribution = Counter(score_bucket(item.get("score")) for item in items)
    missing_counts = field_missing_counts(items)
    threshold = float(config.get("min_score_for_suspicious_reject") or 70)
    keywords = [str(value) for value in config.get("high_value_keywords") or []]
    suspicious = {
        "high_score_rejected": [],
        "high_value_keyword_rejected": [],
        "low_score_selected": [],
        "missing_reject_reason": [],
        "low_quality_source_selected": [],
        "possible_dedupe_misses": [],
    }
    for item in rejected_items:
        if item.get("score") is not None and item["score"] >= threshold:
            suspicious["high_score_rejected"].append(item)
        hits = high_value_hits(item, keywords)
        if hits:
            copy_item = dict(item)
            copy_item["high_value_hits"] = hits[:6]
            suspicious["high_value_keyword_rejected"].append(copy_item)
        if not item.get("reject_reason"):
            suspicious["missing_reject_reason"].append(item)
    for item in selected_items:
        if item.get("score") is not None and item["score"] < 30:
            suspicious["low_score_selected"].append(item)
        if source_trust_score(item) < 0:
            suspicious["low_quality_source_selected"].append(item)
    suspicious["possible_dedupe_misses"] = find_possible_duplicate_items(items)
    return {
        "total_news": len(items),
        "selected_count": len(selected_items),
        "rejected_count": len(rejected_items),
        "source_distribution": dict(source_distribution.most_common()),
        "tag_distribution": dict(tag_distribution.most_common()),
        "reject_reason_distribution": dict(reject_distribution.most_common()),
        "score_distribution": dict(score_distribution),
        "missing_fields": missing_counts,
        "suspicious": {key: compact_items(value, limit=20) for key, value in suspicious.items()},
    }


def compact_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title") or "",
        "url": item.get("url") or "",
        "source": item.get("source") or item.get("domain") or "",
        "published_at": item.get("published_at") or "",
        "score": item.get("score"),
        "tags": item.get("tags") or [],
        "reject_reason": item.get("reject_reason") or "",
        "entities": item.get("entities") or [],
        "topic_type": item.get("topic_type") or "",
        **({"high_value_hits": item.get("high_value_hits")} if item.get("high_value_hits") else {}),
    }


def compact_items(items: list[Any], limit: int = 10) -> list[Any]:
    compacted = []
    for item in items[:limit]:
        if isinstance(item, dict) and "items" in item:
            compacted.append(item)
        elif isinstance(item, dict):
            compacted.append(compact_item(item))
        else:
            compacted.append(item)
    return compacted


def find_possible_duplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if not left.get("title") or not right.get("title"):
                continue
            same_dedupe = left.get("dedupe_key") and left.get("dedupe_key") == right.get("dedupe_key")
            entity_overlap = set(normalize_text(entity) for entity in left.get("entities") or []) & set(
                normalize_text(entity) for entity in right.get("entities") or []
            )
            similarity = text_similarity(left["title"], right["title"])
            if same_dedupe or (entity_overlap and similarity >= 0.64):
                groups.append(
                    {
                        "degree": "中" if similarity < 0.78 and not same_dedupe else "高",
                        "reason": "dedupe_key 相同" if same_dedupe else "标题实体和文本相似",
                        "items": [compact_item(left), compact_item(right)],
                    }
                )
            if len(groups) >= 12:
                return groups
    return groups


def missed_candidate_reason(item: dict[str, Any], keyword_hits: list[str], fresh: bool, trust: int) -> str:
    reasons = []
    if fresh:
        reasons.append("时效性较强")
    if trust > 0:
        reasons.append("来源可信度较高")
    if has_clear_event(item):
        reasons.append("有明确事件")
    if item.get("entities"):
        reasons.append("涉及重要主体：" + "、".join((item.get("entities") or [])[:3]))
    if keyword_hits:
        reasons.append("命中高价值信号：" + "、".join(keyword_hits[:4]))
    if item.get("topic_type") != "general":
        reasons.append("事件类型清晰：" + item["topic_type"])
    if item.get("summary") and len(item["summary"]) > 80:
        reasons.append("摘要信息量足够支撑口播")
    return "；".join(reasons) or "具备一定新闻价值，需要人工判断"


def infer_logic_problem(item: dict[str, Any], keyword_hits: list[str]) -> str:
    if item.get("reject_reason") and "duplicate" in item["reject_reason"].lower():
        return "dedupe 误杀或历史重复判断过严"
    if item.get("reject_reason") and any(word in item["reject_reason"].lower() for word in ("stale", "time", "old")):
        return "freshness 判断异常或发布时间字段解析不一致"
    if keyword_hits and not item.get("tags"):
        return "tagger 漏打标签"
    if item.get("score") is not None and item["score"] >= 70:
        return "selection/ranking 权重不合理"
    if source_trust_score(item) > 0:
        return "source_weight 偏低或 source 白名单覆盖不足"
    if item.get("topic_type") != "general":
        return "keyword_filter 过窄或 relevance_score 低估"
    return "ranking 或口播适配评分需要人工复核"


def suggest_script_position(item: dict[str, Any]) -> str:
    topic = item.get("topic_type")
    if topic in {"model_release", "open_source", "agent", "product_launch"}:
        return "主体新闻段或工具/模型更新段"
    if topic in {"funding", "earnings"}:
        return "商业动态段"
    if topic in {"compute", "regulation"}:
        return "行业变化段"
    return "尾声快讯或明日观察点"


def find_missed_good_news(items: list[dict[str, Any]], episode_id: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    keywords = [str(value) for value in config.get("high_value_keywords") or []]
    reference_date = episode_date(episode_id)
    candidates: list[dict[str, Any]] = []
    for item in items:
        if item.get("selected"):
            continue
        hits = high_value_hits(item, keywords)
        fresh = is_fresh(item, reference_date)
        trust = source_trust_score(item)
        clear_event = has_clear_event(item)
        social_penalty = 2.5 if is_social_item(item) and not clear_event else 0
        topic_bonus = 2 if item.get("topic_type") != "general" else 0
        entity_bonus = min(len(item.get("entities") or []), 3)
        score = (item.get("score") or 0) / 25
        value = score + len(hits) * 1.0 + (2 if fresh else 0) + trust + topic_bonus + entity_bonus + (1.5 if clear_event else 0) - social_penalty
        if is_social_item(item) and not clear_event:
            continue
        if value < 4 and not (hits and (fresh or trust > 0 or ((item.get("score") or 0) >= 70 and clear_event))):
            continue
        candidates.append(
            {
                **compact_item(item),
                "value_score": round(value, 2),
                "why_missed_candidate": missed_candidate_reason(item, hits, fresh, trust),
                "suggest_add": value >= 6,
                "suggest_position": suggest_script_position(item),
                "possible_logic_problem": infer_logic_problem(item, hits),
                "high_value_hits": hits,
            }
        )
    candidates.sort(key=lambda item: item["value_score"], reverse=True)
    return candidates[: int(config.get("max_missed_news_candidates") or 10)]


def representative_title(item: dict[str, Any]) -> str:
    return item.get("title") or item.get("summary", "")[:80] or item.get("url") or "未命名新闻"


def selected_items_for_review(items: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    selected = [item for item in items if item.get("selected")]
    existing_titles = {normalize_text(item.get("title") or "") for item in selected}
    for title in context["selected_titles"]:
        key = normalize_text(title)
        if key and key not in existing_titles:
            selected.append(
                {
                    "title": title,
                    "url": "",
                    "source": "research.json",
                    "published_at": "",
                    "score": None,
                    "tags": [],
                    "reject_reason": "",
                    "entities": extract_entities(title),
                    "topic_type": classify_topic_type(title),
                    "summary": "",
                    "selected": True,
                }
            )
    return selected


def duplicate_degree(similarity: float, entity_overlap: set[str], same_topic: bool) -> str:
    if similarity >= 0.82 or (entity_overlap and same_topic):
        return "高"
    if similarity >= 0.72 or entity_overlap:
        return "中"
    return "低"


def find_internal_duplicates(selected: list[dict[str, Any]], script_text: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    threshold = float(config.get("duplicate_similarity_threshold") or 0.72)
    parent = list(range(len(selected)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    pair_reasons: dict[tuple[int, int], dict[str, Any]] = {}
    for left_index, left in enumerate(selected):
        for right_index, right in enumerate(selected[left_index + 1 :], start=left_index + 1):
            left_text = " ".join([left.get("title") or "", left.get("summary") or ""])
            right_text = " ".join([right.get("title") or "", right.get("summary") or ""])
            similarity = text_similarity(left_text, right_text)
            left_entities = {normalize_text(entity) for entity in left.get("entities") or []}
            right_entities = {normalize_text(entity) for entity in right.get("entities") or []}
            entity_overlap = {entity for entity in left_entities & right_entities if entity}
            same_topic = left.get("topic_type") == right.get("topic_type") and left.get("topic_type") != "general"
            same_url = normalize_url(left.get("url") or "") and normalize_url(left.get("url") or "") == normalize_url(right.get("url") or "")
            if same_url or similarity >= threshold or (entity_overlap and same_topic) or (entity_overlap and similarity >= 0.58):
                union(left_index, right_index)
                pair_reasons[(left_index, right_index)] = {
                    "similarity": round(similarity, 3),
                    "entity_overlap": sorted(entity_overlap),
                    "same_topic": same_topic,
                    "same_url": bool(same_url),
                }
    grouped: defaultdict[int, list[int]] = defaultdict(list)
    for index in range(len(selected)):
        grouped[find(index)].append(index)
    duplicates: list[dict[str, Any]] = []
    for indexes in grouped.values():
        if len(indexes) < 2:
            continue
        reasons = [pair_reasons[key] for key in pair_reasons if key[0] in indexes and key[1] in indexes]
        max_similarity = max((reason["similarity"] for reason in reasons), default=0)
        all_entities = sorted(
            {
                entity
                for index in indexes
                for entity in (selected[index].get("entities") or [])
                if entity and normalize_text(entity)
            }
        )
        keep_index = max(indexes, key=lambda index: (selected[index].get("score") or 0, len(selected[index].get("summary") or "")))
        remove_titles = [representative_title(selected[index]) for index in indexes if index != keep_index]
        duplicates.append(
            {
                "topic_group": "、".join(all_entities[:4]) or selected[keep_index].get("topic_type") or "相似主题",
                "titles": [representative_title(selected[index]) for index in indexes],
                "degree": duplicate_degree(max_similarity, {normalize_text(entity) for entity in all_entities}, True),
                "suggest_keep": representative_title(selected[keep_index]),
                "suggest_merge_or_delete": remove_titles,
                "script_edit_suggestion": "保留信息量最高的一条，其余合并为一句进展补充，避免重复铺陈同一主体或泛话题。",
                "evidence": reasons[:5],
            }
        )
    paragraph_duplicates = find_repeated_script_paragraphs(script_text)
    duplicates.extend(paragraph_duplicates)
    return duplicates[:12]


def find_repeated_script_paragraphs(script_text: str) -> list[dict[str, Any]]:
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n|(?<=[。！？!?])\s*\n", script_text) if len(chunk.strip()) >= 60]
    results: list[dict[str, Any]] = []
    for left_index, left in enumerate(chunks):
        for right_index, right in enumerate(chunks[left_index + 1 :], start=left_index + 1):
            similarity = text_similarity(left, right)
            if similarity >= 0.82:
                results.append(
                    {
                        "topic_group": "口播稿重复观点",
                        "titles": [left[:80], right[:80]],
                        "degree": "高",
                        "suggest_keep": left[:80],
                        "suggest_merge_or_delete": [right[:80]],
                        "script_edit_suggestion": "删掉后一次重复观点，或改成承接前文的新信息。",
                        "evidence": [{"similarity": round(similarity, 3), "paragraph_indexes": [left_index, right_index]}],
                    }
                )
            if len(results) >= 4:
                return results
    return results


def standard_episode_dirs(episodes_root: Path) -> list[Path]:
    dirs = [
        path
        for path in episodes_root.iterdir()
        if path.is_dir() and re.match(r"^20\d{6}E\d+$", path.name)
    ]
    return sorted(dirs, key=lambda path: path.name)


def previous_episode_dirs(episodes_root: Path, episode_id: str, lookback: int) -> list[Path]:
    dirs = [path for path in standard_episode_dirs(episodes_root) if path.name != episode_id and path.name < episode_id]
    return list(reversed(dirs))[:lookback]


def load_episode_context(path: Path) -> dict[str, Any]:
    research_json = read_json(path / "research.json", {})
    script_text = read_text(path / "podcast_script.txt")
    shownotes_text = read_text(path / "shownotes.txt")
    research_md = read_text(path / "research.md")
    selected_titles: list[str] = []
    selected_urls: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            title = clean_str(value.get("title") or value.get("name") or value.get("headline"))
            url = clean_str(value.get("url") or value.get("link"))
            if title:
                selected_titles.append(title)
            if url:
                selected_urls.add(normalize_url(url))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and len(value.strip()) >= 4:
            selected_titles.append(value.strip())

    for key in ("trend_titles", "products", "sources", "selected", "items", "stories"):
        walk(research_json.get(key))
    text = "\n".join([script_text, shownotes_text, research_md, "\n".join(selected_titles)])
    return {
        "episode_id": path.name,
        "titles": sorted(set(selected_titles)),
        "urls": selected_urls,
        "text": text,
        "entities": extract_entities(text),
    }


def find_recent_duplicates(
    selected: list[dict[str, Any]],
    episodes_root: Path,
    episode_id: str,
    lookback: int,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    threshold = float(config.get("recent_duplicate_threshold") or 0.68)
    max_scan = int(config.get("max_recent_episodes_to_scan") or lookback)
    previous = previous_episode_dirs(episodes_root, episode_id, min(lookback, max_scan))
    contexts = [load_episode_context(path) for path in previous]
    duplicates: list[dict[str, Any]] = []
    for item in selected:
        item_title = representative_title(item)
        item_url = normalize_url(item.get("url") or "")
        item_entities = {normalize_text(entity) for entity in item.get("entities") or [] if entity}
        for context in contexts:
            url_exact = item_url and item_url in context["urls"]
            best_title = ""
            best_similarity = 0.0
            for old_title in context["titles"]:
                similarity = text_similarity(item_title, old_title)
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_title = old_title
            entity_overlap = item_entities & {normalize_text(entity) for entity in context["entities"] if entity}
            text_hit = any(entity and entity in normalize_text(context["text"]) for entity in item_entities)
            if url_exact or best_similarity >= threshold or (entity_overlap and (best_similarity >= 0.48 or text_hit)):
                degree = "高" if url_exact or best_similarity >= 0.82 else "中" if best_similarity >= threshold or entity_overlap else "低"
                duplicates.append(
                    {
                        "episode_id": context["episode_id"],
                        "today_title": item_title,
                        "repeated_theme": "、".join(sorted(entity_overlap)[:4]) or best_title or item.get("topic_type") or "相似主题",
                        "degree": degree,
                        "has_new_info": not url_exact and best_similarity < 0.82,
                        "suggest_keep": degree != "高",
                        "suggest_expression": "此前我们提到过，今天的新变化是……" if degree != "高" else "除非有新进展，否则建议删除或合并为一句回顾。",
                        "evidence": {
                            "matched_title": best_title,
                            "title_similarity": round(best_similarity, 3),
                            "url_exact": bool(url_exact),
                            "entity_overlap": sorted(entity_overlap),
                        },
                    }
                )
                break
        if len(duplicates) >= 16:
            break
    return duplicates


def relevant_code_files() -> list[dict[str, Any]]:
    keywords = (
        "search",
        "crawl",
        "news",
        "rank",
        "score",
        "filter",
        "dedupe",
        "select",
        "tag",
        "classify",
        "research",
        "script",
        "solo",
        "podcast",
    )
    skip_parts = {"episodes", ".git", "__pycache__", "logs", "audio_assets", "assets", "outputs", "backups", "podcast-site", "webapp"}
    candidates: list[dict[str, Any]] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".sh", ".json"}:
            continue
        if any(part in skip_parts for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        rel = str(path.relative_to(PROJECT_ROOT))
        rel_lower = rel.lower()
        if not any(keyword in rel_lower for keyword in keywords):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        hit_terms = [keyword for keyword in keywords if keyword in rel_lower or keyword in text.lower()]
        funcs = re.findall(r"^\s*def\s+([a-zA-Z_][\w]*)\s*\(", text, flags=re.M)
        candidates.append(
            {
                "file": rel,
                "hit_terms": hit_terms[:10],
                "functions": funcs[:12],
            }
        )
    candidates.sort(key=lambda item: (0 if item["file"] in {"ai_news_pipeline.py", "solo_pipeline.py"} else 1, item["file"]))
    return candidates[:24]


def possible_code_locations(
    overview: dict[str, Any],
    missed: list[dict[str, Any]],
    internal_duplicates: list[dict[str, Any]],
    recent_duplicates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    scan = relevant_code_files()

    def files_matching(*needles: str) -> list[str]:
        matches = []
        for item in scan:
            haystack = " ".join([item["file"], " ".join(item["hit_terms"]), " ".join(item["functions"])]).lower()
            if any(needle in haystack for needle in needles):
                matches.append(item["file"])
        return matches[:6]

    problems: list[dict[str, Any]] = []
    suspicious = overview.get("suspicious") or {}
    if missed or suspicious.get("high_score_rejected") or suspicious.get("high_value_keyword_rejected"):
        problems.append(
            {
                "problem_type": "疑似好新闻被过滤或排序靠后",
                "related_files": files_matching("filter", "score", "rank", "select", "news") or ["ai_news_pipeline.py", "solo_pipeline_modular/solo_script.py"],
                "related_functions_or_keywords": ["source_quality_score", "relevance", "summarize_sources", "_select_solo_news_items", "filter_provider_results"],
                "why_suspect": "报告发现高分/高价值关键词新闻未入选，问题更可能发生在过滤、打分、排序或入选阶段。",
                "fix_now": False,
                "fix_suggestion": "先补充审计字段 selected/reject_reason/dedupe_key/importance_score/freshness_score/relevance_score，再回放误杀候选。",
                "risk_level": "P1",
            }
        )
    if internal_duplicates:
        problems.append(
            {
                "problem_type": "当天入选主题重复",
                "related_files": files_matching("dedupe", "script", "solo", "select") or ["ai_news_pipeline.py", "solo_pipeline_modular/solo_script.py"],
                "related_functions_or_keywords": ["dedupe_search_items", "_select_solo_news_items", "_build_solo_news_cards_from_items"],
                "why_suspect": "同一主体或同一泛话题进入当天稿件，说明入选前去重或成稿阶段合并不足。",
                "fix_now": False,
                "fix_suggestion": "在入选前增加按实体和事件类型的审计日志，先人工确认阈值，再决定是否改 dedupe 规则。",
                "risk_level": "P1",
            }
        )
    if recent_duplicates:
        problems.append(
            {
                "problem_type": "近期期重复或旧闻重包装",
                "related_files": files_matching("history", "dedupe", "filter", "solo") or ["solo_pipeline_modular/solo_pipeline.py", "ai_news_pipeline.py"],
                "related_functions_or_keywords": ["filter_recently_covered_search_payload", "classify_recent_duplicate", "hard_history_block_filter"],
                "why_suspect": "今天选题与近几期文本或实体重复，问题可能来自历史过滤覆盖不足或进展跟踪表达缺失。",
                "fix_now": False,
                "fix_suggestion": "把重复主题写成“此前提到过，今天新变化是……”，并记录历史命中实体。",
                "risk_level": "P1",
            }
        )
    if any(value > 0 for key, value in (overview.get("missing_fields") or {}).items() if key in {"selected", "reject_reason", "dedupe_key", "importance_score", "freshness_score", "relevance_score"}):
        problems.append(
            {
                "problem_type": "新闻 JSON 审计字段不统一",
                "related_files": files_matching("research", "news", "score", "filter") or ["solo_pipeline_modular/solo_pipeline.py", "ai_news_pipeline.py"],
                "related_functions_or_keywords": ["write_json(search_results.json)", "summarize_sources", "filtered_counts"],
                "why_suspect": "字段缺失会让复盘只能依赖兼容推断，误杀定位粒度不够。",
                "fix_now": False,
                "fix_suggestion": "下一轮再标准化字段；本轮只在复盘脚本兼容读取，不改上游格式。",
                "risk_level": "P2",
            }
        )
    return problems[:10]


def improvement_suggestions(
    overview: dict[str, Any],
    missed: list[dict[str, Any]],
    internal_duplicates: list[dict[str, Any]],
    recent_duplicates: list[dict[str, Any]],
) -> dict[str, list[str]]:
    suggestions = {"P0": [], "P1": [], "P2": []}
    if overview.get("total_news", 0) == 0:
        suggestions["P0"].append("新闻源为空时阻断后续 TTS/发布，并回查抓取任务日志。")
    if internal_duplicates:
        suggestions["P1"].append("入选前增加按实体、URL、GitHub repo、模型名的重复审计，不直接调低全局阈值。")
    if missed:
        suggestions["P1"].append("把误杀候选沉淀为回放样例，逐条确认是 keyword_filter、source_weight、freshness 还是 dedupe 问题。")
    if recent_duplicates:
        suggestions["P1"].append("近 7 期命中同主体时，默认改成进展跟踪表达，而不是全新新闻表达。")
    missing_fields = overview.get("missing_fields") or {}
    if missing_fields.get("reject_reason", 0) or missing_fields.get("selected", 0):
        suggestions["P2"].append("标准化 search_results.json 审计字段：selected、reject_reason、dedupe_key 和三类 score。")
    if overview.get("suspicious", {}).get("low_score_selected"):
        suggestions["P1"].append("复核低分入选新闻的来源权重和口播适配分，避免来源权重压过事件质量。")
    if not any(suggestions.values()):
        suggestions["P2"].append("保持当前阈值，连续观察 3 天后再调整筛选规则。")
    return suggestions


def tomorrow_watchpoints(
    missed: list[dict[str, Any]],
    internal_duplicates: list[dict[str, Any]],
    recent_duplicates: list[dict[str, Any]],
    overview: dict[str, Any],
) -> list[str]:
    points = []
    if missed:
        points.append("今天的疑似误杀候选明天是否再次被抓到或进入播报。")
    if internal_duplicates:
        points.append("同一公司/模型是否继续在一天内拆成多条播报。")
    if recent_duplicates:
        points.append("近 7 期重复主体是否改成进展跟踪表达。")
    missing = overview.get("missing_fields") or {}
    if missing.get("reject_reason", 0):
        points.append("未入选新闻是否补齐 reject_reason，减少人工猜测。")
    if missing.get("selected", 0):
        points.append("入选状态是否能在新闻源 JSON 中直接审计。")
    points.append("高价值关键词新闻是否仍因 source_weight 或 freshness 被低估。")
    return points[:5]


def format_distribution(distribution: dict[str, Any], limit: int = 12) -> list[str]:
    if not distribution:
        return ["- 无"]
    items = sorted(distribution.items(), key=lambda item: (-int(item[1]), item[0]))[:limit]
    return [f"- {key}：{value}" for key, value in items]


def format_missing_fields(missing_fields: dict[str, int], total: int) -> list[str]:
    return [f"- {field}：缺失 {count}/{total}" for field, count in missing_fields.items() if count]


def conclusion_status(missed: list[dict[str, Any]], internal_duplicates: list[dict[str, Any]], recent_duplicates: list[dict[str, Any]], overview: dict[str, Any]) -> str:
    if overview.get("total_news", 0) == 0:
        return "阻塞"
    if len(internal_duplicates) >= 3 or len(missed) >= 8:
        return "建议重写"
    if missed or internal_duplicates or recent_duplicates or overview.get("suspicious", {}).get("high_score_rejected"):
        return "建议人工复核"
    return "通过"


def build_markdown_report(
    episode_id: str,
    overview: dict[str, Any],
    missed: list[dict[str, Any]],
    internal_duplicates: list[dict[str, Any]],
    recent_duplicates: list[dict[str, Any]],
    code_locations: list[dict[str, Any]],
    suggestions: dict[str, list[str]],
    watchpoints: list[str],
    optional_missing: list[str],
) -> str:
    status = conclusion_status(missed, internal_duplicates, recent_duplicates, overview)
    max_problem = "未发现明显问题"
    if missed:
        max_problem = f"{len(missed)} 条疑似误杀好新闻需要人工复核"
    if internal_duplicates:
        max_problem = f"{len(internal_duplicates)} 组当天稿件内部重复"
    if recent_duplicates and not internal_duplicates:
        max_problem = f"{len(recent_duplicates)} 组近期期内容重复"
    tts = "否，先人工复核" if status in {"建议重写", "阻塞"} else "可在人工复核后进入"
    publish = "否，先人工复核" if status != "通过" else "可发布"
    lines = [
        f"# {episode_id} 单人口播新闻筛选复盘",
        "",
        "为避免终端截断，本报告不用 Markdown 表格，改用分组列表呈现。",
        "",
        "## 1. 结论",
        f"- 今日结论：{status}",
        f"- 最大问题：{max_problem}",
        f"- 是否建议进入 TTS：{tts}",
        f"- 是否建议发布：{publish}",
    ]
    if optional_missing:
        lines.append("- 非关键缺失文件：" + "、".join(optional_missing))
    lines.extend(
        [
            "",
            "## 2. 今日新闻源概览",
            f"- 总新闻数：{overview.get('total_news', 0)}",
            f"- 入选数量：{overview.get('selected_count', 0)}",
            f"- 未入选数量：{overview.get('rejected_count', 0)}",
            "- 来源分布：",
            *format_distribution(overview.get("source_distribution") or {}),
            "- 标签分布：",
            *format_distribution(overview.get("tag_distribution") or {}),
            "- 分数分布：",
            *format_distribution(overview.get("score_distribution") or {}),
            "- reject_reason 分布：",
            *format_distribution(overview.get("reject_reason_distribution") or {}),
            "- 字段缺失情况：",
            *(format_missing_fields(overview.get("missing_fields") or {}, overview.get("total_news", 0)) or ["- 无明显缺失"]),
            "- 可疑打标情况：",
        ]
    )
    suspicious = overview.get("suspicious") or {}
    lines.extend(
        [
            f"- 高分但未入选：{len(suspicious.get('high_score_rejected') or [])}",
            f"- 高价值关键词但未入选：{len(suspicious.get('high_value_keyword_rejected') or [])}",
            f"- 低分但入选：{len(suspicious.get('low_score_selected') or [])}",
            f"- 未入选且没有 reject_reason：{len(suspicious.get('missing_reject_reason') or [])}",
            f"- 低质量来源入选：{len(suspicious.get('low_quality_source_selected') or [])}",
            f"- 疑似未正确去重：{len(suspicious.get('possible_dedupe_misses') or [])}",
            "",
            "## 3. 疑似误杀好新闻",
        ]
    )
    if not missed:
        lines.append("- 未发现需要优先补充的误杀候选。")
    for index, item in enumerate(missed, start=1):
        lines.extend(
            [
                f"### 3.{index}. {item.get('title') or '未命名新闻'}",
                f"- 来源：{item.get('source') or '未知'}",
                f"- URL：{item.get('url') or '无'}",
                f"- 当前标签：{', '.join(item.get('tags') or []) or '无'}",
                f"- 当前分数：{item.get('score') if item.get('score') is not None else '缺失'}",
                f"- 被过滤原因：{item.get('reject_reason') or '缺失'}",
                f"- 为什么值得播：{item.get('why_missed_candidate')}",
                f"- 建议动作：{'建议补充' if item.get('suggest_add') else '人工复核后决定'}",
                f"- 建议位置：{item.get('suggest_position')}",
                f"- 可能逻辑问题：{item.get('possible_logic_problem')}",
            ]
        )
    lines.extend(["", "## 4. 当天稿件内部重复检查"])
    if not internal_duplicates:
        lines.append("- 未发现明显当天内部重复。")
    for index, group in enumerate(internal_duplicates, start=1):
        lines.extend(
            [
                f"### 4.{index}. {group.get('topic_group')}",
                f"- 重复话题组：{group.get('topic_group')}",
                f"- 涉及的新闻标题：{'；'.join(group.get('titles') or [])}",
                f"- 重复程度：{group.get('degree')}",
                f"- 建议保留：{group.get('suggest_keep')}",
                f"- 建议删除 / 合并：{'；'.join(group.get('suggest_merge_or_delete') or [])}",
                f"- 口播稿修改建议：{group.get('script_edit_suggestion')}",
            ]
        )
    lines.extend(["", "## 5. 与近几期内容重复检查"])
    if not recent_duplicates:
        lines.append("- 未发现明显近期期重复。")
    for index, item in enumerate(recent_duplicates, start=1):
        lines.extend(
            [
                f"### 5.{index}. {item.get('episode_id')} / {item.get('repeated_theme')}",
                f"- 重复期号：{item.get('episode_id')}",
                f"- 重复主题：{item.get('repeated_theme')}",
                f"- 今日标题：{item.get('today_title')}",
                f"- 重复程度：{item.get('degree')}",
                f"- 是否有新信息：{'是' if item.get('has_new_info') else '否'}",
                f"- 是否建议保留：{'是' if item.get('suggest_keep') else '否'}",
                f"- 建议表达方式：{item.get('suggest_expression')}",
            ]
        )
    lines.extend(["", "## 6. 可能的问题代码位置"])
    if not code_locations:
        lines.append("- 未定位到需要立即处理的代码位置。")
    for index, item in enumerate(code_locations, start=1):
        lines.extend(
            [
                f"### 6.{index}. {item.get('problem_type')}",
                f"- 问题类型：{item.get('problem_type')}",
                f"- 相关文件：{'；'.join(item.get('related_files') or [])}",
                f"- 相关函数或关键词：{'；'.join(item.get('related_functions_or_keywords') or [])}",
                f"- 可能原因：{item.get('why_suspect')}",
                f"- 是否建议立即修：{'是' if item.get('fix_now') else '否'}",
                f"- 修复建议：{item.get('fix_suggestion')}",
                f"- 风险等级：{item.get('risk_level')}",
            ]
        )
    lines.extend(["", "## 7. 新闻筛选逻辑改进建议"])
    for priority in ("P0", "P1", "P2"):
        lines.append(f"### {priority}")
        entries = suggestions.get(priority) or []
        if not entries:
            lines.append("- 暂无")
        else:
            lines.extend(f"- {entry}" for entry in entries)
    lines.extend(["", "## 8. 明日观察点"])
    lines.extend(f"- {point}" for point in watchpoints)
    return "\n".join(lines) + "\n"


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items() if key != "raw"}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def write_reports(episode_id: str, payload: dict[str, Any], markdown: str) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORT_DIR / f"{episode_id}_news_review.md"
    json_path = REPORT_DIR / f"{episode_id}_news_review.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


def run(argv: list[str] | None = None) -> dict[str, Any]:
    args = parse_args(argv)
    episodes_root = Path(args.episodes_root)
    episode_dir = episodes_root / args.episode_id
    if not episode_dir.exists():
        blocked_path = write_blocked_report(args.episode_id, episode_dir, list(REQUIRED_FILES))
        raise ReviewBlocked(args.episode_id, list(REQUIRED_FILES), blocked_path)
    optional_missing = ensure_episode_ready(args.episode_id, episode_dir)
    config = load_config(resolve_project_path(args.config))
    search_json = read_json(episode_dir / "search_results.json", {})
    research_json = read_json(episode_dir / "research.json", {})
    script_text = read_text(episode_dir / "podcast_script.txt")
    shownotes_text = read_text(episode_dir / "shownotes.txt")
    research_md = read_text(episode_dir / "research.md")
    raw_items = extract_raw_items(search_json)
    items = merge_duplicate_items([normalized_item(item) for item in raw_items])
    context = extract_research_context(research_json, script_text, shownotes_text, research_md)
    mark_selected(items, context)
    overview = analyze_source_overview(items, config)
    missed = find_missed_good_news(items, args.episode_id, config)
    selected = selected_items_for_review(items, context)
    internal_duplicates = find_internal_duplicates(selected, script_text, config)
    recent_duplicates = find_recent_duplicates(
        selected=selected,
        episodes_root=episodes_root,
        episode_id=args.episode_id,
        lookback=args.lookback or int(config.get("lookback_episodes") or 7),
        config=config,
    )
    code_locations = possible_code_locations(overview, missed, internal_duplicates, recent_duplicates)
    suggestions = improvement_suggestions(overview, missed, internal_duplicates, recent_duplicates)
    watchpoints = tomorrow_watchpoints(missed, internal_duplicates, recent_duplicates, overview)
    payload = {
        "episode_id": args.episode_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_path": str(resolve_project_path(args.config)),
        "dry_run": args.dry_run,
        "no_update_rules": args.no_update_rules,
        "optional_missing_files": optional_missing,
        "overview": overview,
        "missed_good_news": missed,
        "internal_duplicates": internal_duplicates,
        "recent_duplicates": recent_duplicates,
        "possible_code_locations": code_locations,
        "improvement_suggestions": suggestions,
        "tomorrow_watchpoints": watchpoints,
    }
    markdown = build_markdown_report(
        args.episode_id,
        overview,
        missed,
        internal_duplicates,
        recent_duplicates,
        code_locations,
        suggestions,
        watchpoints,
        optional_missing,
    )
    if args.write_report:
        md_path, json_path = write_reports(args.episode_id, payload, markdown)
        payload["report_paths"] = {"markdown": str(md_path), "json": str(json_path)}
        print(f"[news_review] wrote {md_path}")
        print(f"[news_review] wrote {json_path}")
    print(
        "[news_review] "
        f"episode={args.episode_id} total={overview.get('total_news', 0)} "
        f"selected={overview.get('selected_count', 0)} missed={len(missed)} "
        f"internal_duplicates={len(internal_duplicates)} recent_duplicates={len(recent_duplicates)}"
    )
    return payload


def main() -> None:
    try:
        run()
    except ReviewBlocked as exc:
        print(f"[news_review] blocked {exc.episode_id}: wrote {exc.blocked_path}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
