from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .simple_yaml import parse_simple_yaml
    from .topic_keywords import (
        extract_entities,
        extract_keywords,
        is_low_value_topic,
        normalize_space,
        title_candidate_keywords,
    )
except ImportError:  # pragma: no cover
    from simple_yaml import parse_simple_yaml
    from topic_keywords import (
        extract_entities,
        extract_keywords,
        is_low_value_topic,
        normalize_space,
        title_candidate_keywords,
    )


EPISODE_ID_RE = re.compile(r"^\d{8}E\d+$")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,}|[\u4e00-\u9fff]{2,}")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def latest_episode_id(episodes_root: Path) -> str:
    ids = [path.name for path in episodes_root.iterdir() if path.is_dir() and EPISODE_ID_RE.match(path.name)]
    if not ids:
        raise FileNotFoundError(f"no episode directories found under {episodes_root}")
    return sorted(ids)[-1]


def recent_episode_dirs(episodes_root: Path, current_episode_id: str, limit: int) -> list[Path]:
    dirs = [path for path in episodes_root.iterdir() if path.is_dir() and EPISODE_ID_RE.match(path.name)]
    dirs = sorted([path for path in dirs if path.name != current_episode_id], key=lambda item: item.name, reverse=True)
    return dirs[:limit]


def normalize_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for token in TOKEN_RE.findall(text or ""):
        token = token.lower()
        if token in {"the", "and", "for", "with", "from", "this", "that", "about", "into", "podcast"}:
            continue
        tokens.add(token)
    return tokens


def similarity(left: str, right: str, left_entities: list[str] | None = None) -> float:
    left_tokens = normalize_tokens(left)
    right_tokens = normalize_tokens(right)
    if not left_tokens or not right_tokens:
        base = 0.0
    else:
        base = len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    bonus = 0.0
    lowered_right = right.lower()
    for entity in left_entities or []:
        if entity and entity.lower() in lowered_right:
            bonus += 0.18
    return min(1.0, base + bonus)


def flatten_search_items(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            title = normalize_space(str(node.get("title") or node.get("name") or ""))
            url = normalize_space(str(node.get("url") or node.get("link") or ""))
            content = normalize_space(str(node.get("content") or node.get("description") or node.get("summary") or ""))
            if title and (url or content):
                reject_reason = node.get("reject_reason") or node.get("reason") or node.get("filter_reason") or ""
                tags = node.get("tags") or node.get("tag") or []
                if isinstance(tags, str):
                    tags = [tags]
                profile = node.get("profile")
                if isinstance(profile, dict):
                    profile_tags = profile.get("tags") or profile.get("categories") or []
                    if isinstance(profile_tags, list):
                        tags = list(tags) + [str(tag) for tag in profile_tags]
                items.append(
                    {
                        "title": title,
                        "url": url,
                        "content": content,
                        "score": node.get("score") or node.get("relevance_score") or node.get("quality_score") or "",
                        "tags": [str(tag) for tag in tags if tag],
                        "category": node.get("category") or node.get("feed_category") or node.get("bucket") or "",
                        "reject_reason": normalize_space(str(reject_reason)),
                        "path": path,
                        "published_at": node.get("published_date") or node.get("published_at") or node.get("age") or "",
                    }
                )
            for key, value in node.items():
                visit(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]")

    visit(payload, "")
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        key = (item.get("url") or item.get("title") or "").lower()
        if key and key not in deduped:
            deduped[key] = item
    return list(deduped.values())


def selected_research_text(research_payload: Any) -> tuple[str, set[str]]:
    parts: list[str] = []
    urls: set[str] = set()
    if isinstance(research_payload, dict):
        meta = research_payload.get("meta")
        if isinstance(meta, dict):
            parts.append(str(meta.get("title") or ""))
        for key in ("trend_titles", "products", "sources"):
            value = research_payload.get(key)
            if isinstance(value, list):
                for entry in value:
                    if isinstance(entry, dict):
                        parts.append(" ".join(str(entry.get(name) or "") for name in ("title", "name", "summary", "content")))
                        if entry.get("url"):
                            urls.add(str(entry["url"]))
                    else:
                        parts.append(str(entry))
    return normalize_space(" ".join(parts)), urls


def episode_title(path: Path) -> str:
    title = normalize_space(read_text(path / "title.txt"))
    if title:
        return title
    record = read_json(path / "record.json")
    if isinstance(record, dict):
        for key in ("title", "episode_title"):
            if record.get(key):
                return normalize_space(str(record[key]))
    research = read_json(path / "research.json")
    if isinstance(research, dict) and isinstance(research.get("meta"), dict):
        return normalize_space(str(research["meta"].get("title") or ""))
    return path.name


def build_our_context(
    episode_id: str,
    episodes_root: Path,
    own_items: list[dict[str, Any]] | None = None,
    own_lookback_episodes: int = 7,
) -> dict[str, Any]:
    episode_dir = episodes_root / episode_id
    search_payload = read_json(episode_dir / "search_results.json")
    research_payload = read_json(episode_dir / "research.json")
    research_text, selected_urls = selected_research_text(research_payload)
    text_files = {
        "research.md": read_text(episode_dir / "research.md"),
        "podcast_script.txt": read_text(episode_dir / "podcast_script.txt"),
        "shownotes.txt": read_text(episode_dir / "shownotes.txt"),
    }
    today_text = normalize_space(" ".join([research_text, *text_files.values()]))
    search_items = flatten_search_items(search_payload)
    recent: list[dict[str, Any]] = []
    for path in recent_episode_dirs(episodes_root, episode_id, own_lookback_episodes):
        combined = normalize_space(
            " ".join(
                [
                    episode_title(path),
                    read_text(path / "research.md"),
                    read_text(path / "podcast_script.txt"),
                    read_text(path / "shownotes.txt"),
                ]
            )
        )
        recent.append(
            {
                "episode_id": path.name,
                "title": episode_title(path),
                "text": combined,
                "entities": extract_entities(combined),
                "topics": extract_keywords(combined, max_count=12),
            }
        )
    own_reference_items = []
    for item in own_items or []:
        if item.get("source_role") == "own_podcast" and item.get("source_status") != "failed":
            own_reference_items.append(item)
    return {
        "episode_id": episode_id,
        "episode_dir": str(episode_dir),
        "today_text": today_text,
        "today_entities": extract_entities(today_text),
        "today_keywords": extract_keywords(today_text, max_count=32),
        "selected_urls": selected_urls,
        "search_items": search_items,
        "recent_episodes": recent,
        "own_reference_items": own_reference_items,
        "source_files": {name: bool(text) for name, text in text_files.items()}
        | {"search_results.json": search_payload is not None, "research.json": research_payload is not None},
    }


def best_search_matches(topic: dict[str, Any], search_items: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    topic_text = normalize_space(" ".join(str(topic.get(key) or "") for key in ("title", "summary", "shownotes", "topic_summary")))
    entities = [str(entity) for entity in topic.get("entities") or []]
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in search_items:
        haystack = normalize_space(f"{item.get('title', '')} {item.get('content', '')} {item.get('url', '')}")
        score = similarity(topic_text, haystack, entities)
        if score >= 0.16 or any(entity.lower() in haystack.lower() for entity in entities):
            candidate = dict(item)
            candidate["match_score"] = round(score, 3)
            scored.append((score, candidate))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


def is_topic_selected(topic: dict[str, Any], context: dict[str, Any], matches: list[dict[str, Any]]) -> bool:
    today_text = str(context.get("today_text") or "")
    topic_text = normalize_space(" ".join(str(topic.get(key) or "") for key in ("title", "topic_summary")))
    if similarity(topic_text, today_text, [str(entity) for entity in topic.get("entities") or []]) >= 0.42:
        return True
    selected_urls = context.get("selected_urls") or set()
    for match in matches:
        if match.get("url") and match["url"] in selected_urls:
            return True
        if match.get("title") and str(match["title"])[:32] in today_text:
            return True
    return False


def best_recent_match(topic: dict[str, Any], recent_episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    topic_text = normalize_space(" ".join(str(topic.get(key) or "") for key in ("title", "summary", "topic_summary")))
    entities = [str(entity) for entity in topic.get("entities") or []]
    best: tuple[float, dict[str, Any] | None] = (0.0, None)
    for episode in recent_episodes:
        score = similarity(topic_text, str(episode.get("text") or ""), entities)
        if score > best[0]:
            best = (score, episode)
    if best[1] and best[0] >= 0.38:
        matched = dict(best[1])
        matched["match_score"] = round(best[0], 3)
        return matched
    return None


def infer_miss_reason(matches: list[dict[str, Any]]) -> tuple[str, list[str]]:
    if not matches:
        return "search_results.json 未找到相关标题、URL 或实体", ["source list 覆盖不足", "关键词覆盖不足"]
    top = matches[0]
    path = str(top.get("path") or "")
    score = top.get("score")
    tags = top.get("tags") or []
    reject_reason = str(top.get("reject_reason") or "")
    problems: list[str] = []
    if "filtered_out" in path or reject_reason:
        problems.append("relevance_score 低估或过滤规则误杀")
    if "history_filter" in path or "dedup" in reject_reason.lower() or "history" in reject_reason.lower():
        problems.append("dedupe 或历史过滤误杀")
    if not tags:
        problems.append("tagger 漏打标签")
    try:
        if score != "" and float(score) < 0.7:
            problems.append("ranking 权重低估")
    except Exception:
        pass
    if not problems:
        problems.append("selection topN 截断导致漏掉")
    return "；".join(problems), problems


def compare_competitor_topics(
    competitor_items: list[dict[str, Any]],
    context: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for item in competitor_items:
        if item.get("source_status") == "failed":
            results.append(
                {
                    "status": "fetch_failed",
                    "topic": item,
                    "matches": [],
                    "miss_reason": item.get("fetch_error") or "抓取失败",
                    "action": "manual_review",
                    "recommend_supplement": False,
                }
            )
            continue
        matches = best_search_matches(item, context.get("search_items") or [])
        covered_today = is_topic_selected(item, context, matches)
        recent = best_recent_match(item, context.get("recent_episodes") or [])
        low_value = is_low_value_topic(item, config)
        miss_reason, problems = infer_miss_reason(matches)
        if covered_today:
            status = "already_covered"
            action = "ignore"
            recommend = False
        elif recent:
            status = "recently_covered"
            action = "ignore"
            recommend = False
            miss_reason = "近 7 期已覆盖；除非出现新进展，不建议重复播报"
            problems = []
        elif matches:
            status = "source_filtered"
            action = "manual_review"
            recommend = True
        elif low_value:
            status = "low_value"
            action = "ignore"
            recommend = False
            miss_reason = "竞品话题偏观点或旧闻，未发现明确泛科技硬信息"
            problems = []
        else:
            status = "source_gap"
            action = "keep_for_tomorrow"
            recommend = True
        results.append(
            {
                "status": status,
                "topic": item,
                "matches": matches,
                "recent_match": recent,
                "miss_reason": miss_reason,
                "possible_problems": problems,
                "action": action,
                "recommend_supplement": recommend,
            }
        )
    return {
        "items": results,
        "summary": summarize_results(results),
        "supplement_topics": build_supplement_suggestions(results),
        "keyword_suggestions": build_keyword_suggestions(results, config),
        "source_suggestions": build_source_suggestions(results),
    }


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    missed = counts.get("source_filtered", 0) + counts.get("source_gap", 0)
    return {
        "counts": counts,
        "has_important_miss": missed > 0,
        "suggest_today_supplement": counts.get("source_filtered", 0) > 0,
        "biggest_gap": "新闻源覆盖不足" if counts.get("source_gap", 0) else ("过滤或排序误杀" if counts.get("source_filtered", 0) else "暂无明显盲区"),
    }


def build_supplement_suggestions(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for result in results:
        if not result.get("recommend_supplement"):
            continue
        topic = result["topic"]
        status = result["status"]
        urgency = "高" if status == "source_filtered" else "中"
        suggestions.append(
            {
                "topic": topic.get("title") or topic.get("topic_summary") or "未命名竞品话题",
                "reason": result.get("miss_reason") or "",
                "urgency": urgency,
                "suggested_angle": "不要复述竞品标题，建议从事实增量、产品影响和用户/开发者影响三段切入。",
                "evidence": {"account": topic.get("account_name"), "url": topic.get("url")},
                "whether_in_our_source": status == "source_filtered",
                "whether_recently_covered_by_us": bool(result.get("recent_match")),
                "action": result.get("action") or "manual_review",
            }
        )
    return suggestions[:8]


def configured_keyword_set(config: dict[str, Any] | None) -> set[str]:
    terms: set[str] = set()
    entities = ((config or {}).get("high_value_entities") or {})
    for values in entities.values():
        if isinstance(values, list):
            terms.update(str(value).lower() for value in values)
    for category in (config or {}).get("topic_categories") or []:
        terms.add(str(category).lower())
    return terms


def build_keyword_suggestions(results: list[dict[str, Any]], config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    configured = configured_keyword_set(config)
    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        if result["status"] not in {"source_filtered", "source_gap"}:
            continue
        topic = result["topic"]
        for keyword in title_candidate_keywords(str(topic.get("title") or ""), config):
            key = keyword.lower()
            if key in configured or key in seen:
                continue
            seen.add(key)
            keyword_type = "product" if re.search(r"[A-Z][A-Za-z0-9+.-]", keyword) else "topic"
            suggestions.append(
                {
                    "keyword": keyword,
                    "keyword_type": keyword_type,
                    "reason": f"竞品 {topic.get('account_name')} 触发，当前对比状态为 {result['status']}",
                    "triggered_by_competitor": topic.get("account_name") or "",
                    "suggested_weight": "high" if result["status"] == "source_filtered" else "medium",
                    "risk": "medium" if len(keyword) <= 3 else "low",
                }
            )
            if len(suggestions) >= 20:
                return suggestions
    return suggestions


def build_source_suggestions(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for result in results:
        topic = result.get("topic") or {}
        if result.get("status") == "fetch_failed" and topic.get("account_id") == "xiaoyuzhou_competitor":
            suggestions.append(
                {
                    "source_name": "RSSHub 小宇宙播客 feed",
                    "source_url": "https://docs.rsshub.app/routes/social-media#xiao-yu-zhou",
                    "source_type": "RSS",
                    "reason": "小宇宙网页抓取受限时，可人工配置 RSSHub 辅助获取竞品标题。",
                    "priority": "medium",
                }
            )
    return suggestions[:10]


def yaml_quote(value: Any) -> str:
    text = str(value if value is not None else "")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_suggested_review_file(path: Path, analysis: dict[str, Any], episode_id: str, allow_update: bool, dry_run: bool) -> bool:
    if not allow_update or dry_run:
        return False
    existing = {"suggested_keywords": [], "suggested_sources": []}
    try:
        import yaml  # type: ignore

        if path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                existing.update({key: loaded.get(key) or [] for key in existing})
    except ImportError:
        if path.exists():
            loaded = parse_simple_yaml(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                existing.update({key: loaded.get(key) or [] for key in existing})
    except Exception:
        pass
    keyword_seen = {str(item.get("keyword", "")).lower() for item in existing["suggested_keywords"] if isinstance(item, dict)}
    for suggestion in analysis.get("keyword_suggestions") or []:
        keyword = str(suggestion.get("keyword") or "")
        if not keyword or keyword.lower() in keyword_seen:
            continue
        existing["suggested_keywords"].append(
            {
                "keyword": keyword,
                "type": suggestion.get("keyword_type") or "",
                "reason": suggestion.get("reason") or "",
                "triggered_by": suggestion.get("triggered_by_competitor") or "",
                "first_seen_episode": episode_id,
                "suggested_weight": suggestion.get("suggested_weight") or "",
                "noise_risk": suggestion.get("risk") or "",
                "status": "pending_review",
            }
        )
        keyword_seen.add(keyword.lower())
    source_seen = {str(item.get("name", "")).lower() for item in existing["suggested_sources"] if isinstance(item, dict)}
    for suggestion in analysis.get("source_suggestions") or []:
        name = str(suggestion.get("source_name") or "")
        if not name or name.lower() in source_seen:
            continue
        existing["suggested_sources"].append(
            {
                "name": name,
                "url": suggestion.get("source_url") or "",
                "type": suggestion.get("source_type") or "",
                "reason": suggestion.get("reason") or "",
                "priority": suggestion.get("priority") or "",
                "status": "pending_review",
            }
        )
        source_seen.add(name.lower())
    lines = ["suggested_keywords:"]
    for item in existing["suggested_keywords"]:
        lines.append(f"  - keyword: {yaml_quote(item.get('keyword'))}")
        for key in ("type", "reason", "triggered_by", "first_seen_episode", "suggested_weight", "noise_risk", "status"):
            lines.append(f"    {key}: {yaml_quote(item.get(key))}")
    lines.append("")
    lines.append("suggested_sources:")
    for item in existing["suggested_sources"]:
        lines.append(f"  - name: {yaml_quote(item.get('name'))}")
        for key in ("url", "type", "reason", "priority", "status"):
            lines.append(f"    {key}: {yaml_quote(item.get(key))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def report_status_title(status: str) -> str:
    return {
        "already_covered": "已覆盖",
        "recently_covered": "近期已播过，不建议重复",
        "source_filtered": "新闻源有但被过滤",
        "source_gap": "新闻源没有，属于检索盲区",
        "low_value": "不建议跟进",
        "fetch_failed": "抓取失败",
    }.get(status, status)


def build_report_markdown(
    episode_id: str,
    fetched_items: list[dict[str, Any]],
    context: dict[str, Any],
    analysis: dict[str, Any],
) -> str:
    summary = analysis.get("summary") or {}
    keyword_suggestions = analysis.get("keyword_suggestions") or []
    source_suggestions = analysis.get("source_suggestions") or []
    lines: list[str] = [f"# {episode_id} 竞品选题雷达报告", ""]
    lines += [
        "## 1. 今日结论",
        f"- 是否发现漏播重要话题：{'是' if summary.get('has_important_miss') else '否'}",
        f"- 是否建议补充今日稿件：{'是' if summary.get('suggest_today_supplement') else '否'}",
        f"- 是否建议扩大关键词：{'是' if keyword_suggestions else '否'}",
        f"- 是否建议增加新闻源：{'是' if source_suggestions else '否'}",
        f"- 是否与我们近几期内容重复：{'是' if (summary.get('counts') or {}).get('recently_covered') else '否'}",
        f"- 今日最大盲区：{summary.get('biggest_gap') or '暂无'}",
        "",
        "## 2. 竞品最新节目概览",
    ]
    competitors = [item for item in fetched_items if item.get("source_role") == "competitor"]
    if not competitors:
        lines.append("- 未抓取到竞品节目。")
    for item in competitors:
        lines += [
            f"### {item.get('account_name') or item.get('account_id')}",
            f"- 标题：{item.get('title') or '抓取失败'}",
            f"- 发布时间：{item.get('published_at') or '未知'}",
            f"- URL：{item.get('url') or '无'}",
            f"- 主题摘要：{item.get('topic_summary') or item.get('summary') or '无'}",
            f"- 主题类型：{', '.join(item.get('topics') or []) or '无'}",
            f"- 关键实体：{', '.join(item.get('entities') or []) or '无'}",
            f"- source_status：{item.get('source_status')}",
        ]
        if item.get("fetch_error"):
            lines.append(f"- fetch_error：{item.get('fetch_error')}")
    lines += ["", "## 3. 我们自己的近期内容基准"]
    lines += [
        "- 来源：本地 episodes/；own_podcast 抓取成功时仅辅助校验正式发布内容。",
        "- 自己的小宇宙正式发布页：由环境变量 XIAOYUZHOU_PODCAST_PID 或 config/competitor_radar.yaml 配置",
    ]
    for episode in context.get("recent_episodes") or []:
        lines += [
            f"### {episode.get('episode_id')}",
            f"- 标题：{episode.get('title') or '无'}",
            f"- 主要话题：{', '.join((episode.get('topics') or [])[:8]) or '无'}",
            f"- 关键实体：{', '.join((episode.get('entities') or [])[:8]) or '无'}",
        ]
    own_items = [item for item in fetched_items if item.get("source_role") == "own_podcast"]
    for item in own_items:
        lines += [
            "### own_podcast 抓取状态",
            f"- account_id：{item.get('account_id')}",
            f"- 账号：{item.get('account_name') or item.get('account_id')}",
            f"- source_status：{item.get('source_status')}",
            f"- 标题：{item.get('title') or '无'}",
        ]
        if item.get("fetch_error"):
            lines.append(f"- fetch_error：{item.get('fetch_error')}")
    lines += ["", "## 4. 与我们今日播报的对比"]
    for status in ["already_covered", "recently_covered", "source_filtered", "source_gap", "low_value"]:
        lines.append(f"### {report_status_title(status)}")
        subset = [result for result in analysis.get("items") or [] if result.get("status") == status]
        if not subset:
            lines.append("- 无。")
        for result in subset:
            topic = result["topic"]
            lines += [
                f"- 竞品话题：{topic.get('title') or topic.get('topic_summary') or '无标题'}",
                f"  来源账号：{topic.get('account_name')}",
                f"  漏掉原因：{result.get('miss_reason') or '无'}",
                f"  动作：{result.get('action')}",
            ]
    lines += ["", "## 5. 疑似漏播话题"]
    missed = [result for result in analysis.get("items") or [] if result.get("status") in {"source_filtered", "source_gap"}]
    if not missed:
        lines.append("- 未发现疑似漏播话题。")
    for result in missed:
        topic = result["topic"]
        lines += [
            f"### {topic.get('title') or '未命名话题'}",
            f"- 来源账号：{topic.get('account_name')}",
            f"- 竞品链接：{topic.get('url') or '无'}",
            f"- 我们是否抓到：{'是' if result.get('matches') else '否'}",
            f"- 我们是否播报：{'否'}",
            f"- 我们近期是否讲过：{'是' if result.get('recent_match') else '否'}",
            f"- 漏掉原因：{result.get('miss_reason')}",
            f"- 是否建议补充：{'是' if result.get('recommend_supplement') else '否'}",
            "- 建议讲法：从事实增量、影响对象、后续观察三段讲，避免复述竞品口径。",
        ]
    lines += ["", "## 6. 新闻源 JSON 反查结果"]
    for result in missed:
        topic = result["topic"]
        lines.append(f"### {topic.get('title') or '未命名话题'}")
        matches = result.get("matches") or []
        if not matches:
            lines.append("- search_results.json 中未找到相关内容。")
        for match in matches[:3]:
            lines += [
                f"- 对应标题：{match.get('title')}",
                f"  URL：{match.get('url') or '无'}",
                f"  当前标签：{', '.join(match.get('tags') or []) or '无'}",
                f"  当前分数：{match.get('score') or '无'}",
                f"  reject_reason：{match.get('reject_reason') or '无'}",
                f"  可能的问题代码逻辑：{', '.join(result.get('possible_problems') or []) or '无'}",
            ]
    lines += ["", "## 7. 可能需要修复的代码逻辑"]
    logic_map = {
        "关键词覆盖不足": "ai_news_pipeline.py / news_pipeline_rules.json / rss_feeds.json",
        "source list 覆盖不足": "rss_feeds.json / ai_news_pipeline.py 的 provider 查询配置",
        "tagger 漏标": "ai_news_pipeline.py quality_category_for_item / solo_pipeline_modular/solo_news.py",
        "dedupe 误杀": "ai_news_pipeline.py dedupe_search_items / history_filter",
        "ranker 低估": "ai_news_pipeline.py source_quality_score / editorial_title_score / _solo_item_score",
        "selection 截断": "ai_news_pipeline.py final selection / solo_pipeline_modular/solo_news.py",
        "prompt 选题偏好有问题": "solo_pipeline_modular/solo_script.py / ai_news_pipeline.py prompt 组装",
    }
    active_problems = set()
    for result in missed:
        active_problems.update(result.get("possible_problems") or [])
    if not active_problems:
        active_problems.add("暂无明确代码问题")
    for problem, files in logic_map.items():
        if problem == "暂无明确代码问题":
            continue
        should_handle = any(problem in active or active in problem for active in active_problems)
        lines += [
            f"### {problem}",
            f"- 问题描述：{'本次命中或疑似相关。' if should_handle else '本次未直接命中，保留观察。'}",
            f"- 可能相关文件：{files}",
            "- 建议修复方式：先补 pending_review 关键词或来源，再用真实 search_results 回放验证。",
            f"- 风险等级：{'中' if should_handle else '低'}",
            f"- 是否建议本周处理：{'是' if should_handle else '否'}",
        ]
    lines += ["", "## 8. 建议新增关键词索引"]
    if not keyword_suggestions:
        lines.append("- 暂无新增关键词建议。")
    for item in keyword_suggestions:
        lines += [
            f"### {item.get('keyword')}",
            f"- 类型：{item.get('keyword_type')}",
            f"- 触发来源：{item.get('triggered_by_competitor')}",
            f"- 建议权重：{item.get('suggested_weight')}",
            f"- 可能带来的噪音：{item.get('risk')}",
            "- 是否建议加入 config：先进入 pending_review，人工确认后再合并。",
        ]
    lines += ["", "## 9. 建议新增新闻源"]
    if not source_suggestions:
        lines.append("- 暂无新增新闻源建议。")
    for item in source_suggestions:
        lines += [
            f"### {item.get('source_name')}",
            f"- URL：{item.get('source_url')}",
            f"- 类型：{item.get('source_type')}",
            f"- 为什么值得加：{item.get('reason')}",
            f"- 优先级：{item.get('priority')}",
            "- 是否需要人工确认：是。",
        ]
    lines += ["", "## 10. 明日观察点"]
    observations = []
    for suggestion in analysis.get("supplement_topics") or []:
        observations.append(str(suggestion.get("topic") or ""))
    observations += ["AI 云多云策略变化", "AI 编程工具定价和使用率", "国产模型与芯片适配进展", "AI 产品真实 ROI 案例"]
    for item in observations[:5]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append(f"_生成时间：{datetime.now(timezone.utc).isoformat()}_")
    return "\n".join(lines)
