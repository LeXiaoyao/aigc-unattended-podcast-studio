"""News selection, scoring, and fallback content assembly."""
from __future__ import annotations

from .shared import *
from .solo_llm import *


def _normalize_structured_fact_payload(
    raw_payload: dict[str, object],
    item: dict[str, str],
) -> dict[str, str | float | bool]:
    title = _strip_source_suffix(item.get("title", ""))
    content = _clean_text(item.get("content", ""))
    url = item.get("url", "")
    fallback_topic = _compact_topic_label(
        {
            "title": title,
            "content": content,
            "url": url,
            "feed_category": str(item.get("feed_category", "") or ""),
        }
    )

    entity = _display_entity(_clean_optional_text(raw_payload.get("entity"), limit=40))
    if _entity_looks_generic(entity):
        entity = ""
    action = _clean_optional_text(raw_payload.get("action"), limit=20)
    natural_title = _clean_optional_text(raw_payload.get("natural_title"), limit=88) or title
    fact_metrics = _clean_optional_text(raw_payload.get("fact_metrics"), limit=96)
    why_it_matters = _clean_optional_text(raw_payload.get("why_it_matters"), limit=88)
    topic = _normalize_structured_topic(raw_payload.get("topic"), fallback_topic)
    discard_reason = _clean_optional_text(raw_payload.get("discard_reason"), limit=40)

    confidence_value = raw_payload.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(float(confidence_value), 1.0))
    except Exception:
        confidence = 0.0

    is_valuable_raw = raw_payload.get("is_valuable", True)
    is_valuable = bool(is_valuable_raw)
    banned_haystack = " ".join(
        part for part in (entity, action, natural_title, fact_metrics, why_it_matters, discard_reason) if part
    )
    if _looks_like_banned_structured_fact(banned_haystack):
        is_valuable = False
        discard_reason = discard_reason or "generic_or_machine_translated"
    if not entity and _entity_looks_generic(_extract_specific_company_name(title, content)):
        pass
    if not entity and _english_heavy(natural_title) and not _extract_preserved_name(natural_title, content):
        is_valuable = False
        discard_reason = discard_reason or "english_marketing_title"
    if not action or len(action) < 2:
        is_valuable = False
        discard_reason = discard_reason or "missing_action"
    if not entity and not _extract_preserved_name(natural_title, content):
        is_valuable = False
        discard_reason = discard_reason or "missing_entity"
    if not fact_metrics and not why_it_matters:
        confidence = min(confidence, 0.45)

    return {
        "entity": entity,
        "action": action,
        "natural_title": natural_title,
        "fact_metrics": fact_metrics,
        "why_it_matters": why_it_matters,
        "topic": topic,
        "is_valuable": is_valuable,
        "discard_reason": discard_reason,
        "confidence": confidence,
        "url": url,
    }


def _extract_structured_solo_items(items: list[dict[str, str]]) -> list[dict[str, str | float | bool]]:
    pending: list[tuple[str, dict[str, str]]] = []
    results: list[dict[str, str | float | bool]] = []
    for item in items:
        cache_key = _structured_cache_key(item)
        cached = _SOLO_FACT_CACHE.get(cache_key)
        if cached:
            results.append({**item, **cached})
            continue
        pending.append((cache_key, item))

    if pending:
        system_prompt = (
            "你是严格的 AI 科技新闻结构化提取器。"
            "输入里也可能出现非 AI 领域的重大国际或商业新闻，它们属于当天宏观背景，同样需要按事实抽取，不能因为不是纯 AI 新闻就忽略。"
            "你只根据输入新闻抽取事实，绝不编造主体、金额、日期、版本号。"
            "请返回 JSON 对象，格式必须是："
            "{\"items\":[{\"id\":\"1\",\"entity\":\"...或null\",\"action\":\"不超过15个汉字\",\"natural_title\":\"适合中文口播的标题，保留 Gemma 4、Codex 这类模型名原文\",\"fact_metrics\":\"一条具体事实或数字，没有就写null\",\"why_it_matters\":\"一句贴着事实的补充说明，没有就写null\",\"topic\":\"一句短中文栏目名\",\"is_valuable\":true,\"discard_reason\":\"...或null\",\"confidence\":0.0}]}"
            "判断标准：营销页、FAQ、没有明确主体的泛报告、机器翻译痕迹重的标题、老项目主页、空泛观点稿，is_valuable 必须为 false。"
            "如果新闻里没有明确主体，不能把抽象概念、行业类别或商业模式当成主体。"
        )
        request_items = []
        for idx, (_, item) in enumerate(pending, start=1):
            request_items.append(
                {
                    "id": str(idx),
                    "title": _strip_source_suffix(item.get("title", "")),
                    "content": _trim_text(_localize_numbers(item.get("content", "")), limit=820),
                    "published_date": item.get("published_date", ""),
                    "source_bucket": item.get("bucket", ""),
                    "source_host": host_for_url(item.get("url", "")),
                }
            )
        payload = {"items": request_items}
        raw_response = _llm_json(
            system_prompt,
            json.dumps(payload, ensure_ascii=False),
            order_env="SOLO_EXTRACT_MODEL_ORDER",
            default_order="deepseek,qwen,gemini",
            label="结构化新闻提取",
            temperature=0.1,
        )
        raw_items = raw_response.get("items", [])
        response_by_id: dict[str, dict[str, object]] = {}
        if isinstance(raw_items, list):
            for value in raw_items:
                if isinstance(value, dict):
                    response_by_id[str(value.get("id", "")).strip()] = value
        for idx, (cache_key, item) in enumerate(pending, start=1):
            structured = _normalize_structured_fact_payload(response_by_id.get(str(idx), {}), item)
            _SOLO_FACT_CACHE[cache_key] = structured
            results.append({**item, **structured})

    return results


def _extract_specific_company_name(title: str, content: str) -> str:
    text = _clean_text(f"{title} {content}")
    stopwords = {
        "Former",
        "Everyone",
        "Google",
        "Anthropic",
        "Meta",
        "Microsoft",
        "OpenAI",
        "GitHub",
        "TechCrunch",
        "Axios",
        "HBM4",
        "Gemma",
        "Avocado",
    }
    patterns = (
        r"\b([A-Z][A-Za-z0-9&.-]{2,})\s+is entering\b",
        r"\b([A-Z][A-Za-z0-9&.-]{2,})\s+(?:announced|raised|raises|launched|launches|released|releases)\b",
        r"\bcalled\s+([A-Z][A-Za-z0-9&.-]{2,})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = match.group(1).strip(" .,:;()[]{}")
        if candidate in stopwords or _entity_looks_generic(candidate):
            continue
        return candidate
    return ""


def _is_solo_scriptworthy(title: str, content: str, url: str, item: dict[str, Any] | None = None) -> bool:
    macro_context_story = is_macro_context_result(title, content, url, item=item)
    if not is_relevant_ai_result(title, content, url, item=item):
        return False
    lowered_title = _strip_source_suffix(title).lower()
    lowered_body = _clean_text(content).lower()
    lowered_url = (url or "").lower()
    if "openclaw" in lowered_title or "openclaw" in lowered_url:
        return False
    if any(pattern in lowered_title for pattern in SOLO_GENERIC_PAGE_PATTERNS):
        return False
    if any(part in lowered_url for part in SOLO_GENERIC_PAGE_URL_PARTS):
        return False
    if lowered_title.startswith("github -") and not any(part in lowered_url for part in ("/releases", "/tags", "/pulls", "/commit", "/tree/")):
        return False
    if any(pattern in lowered_title for pattern in SOLO_LOW_SIGNAL_PATTERNS):
        return False
    if any(domain in lowered_url for domain in SOLO_LOW_SIGNAL_DOMAINS):
        return False
    if "basketball" in lowered_title or "training camp" in lowered_title:
        return False
    strong_signal = macro_context_story or any(token in f"{lowered_title} {lowered_body} {lowered_url}" for token in SOLO_ACTION_KEYWORDS)
    if not strong_signal:
        return False
    if "imax" in lowered_title and "ai" not in lowered_title:
        return False
    if (
        not macro_context_story
        and count_ai_signals(f"{title} {content}") < 2
        and not any(token in lowered_title for token in ("openai", "nvidia", "microsoft", "oracle", "anthropic", "gemini", "hbm", "copilot", "agent studio", "product hunt", "github"))
    ):
        return False
    return True


def _extract_section(markdown: str, heading: str) -> str:
    lines = markdown.splitlines()
    inside = False
    collected: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if line.startswith("## "):
            if inside:
                break
            inside = line.strip() == heading
            continue
        if inside:
            collected.append(line)
    return "\n".join(collected).strip()


def _extract_trend_blocks(research_markdown: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, list[str]] | None = None
    current_key = ""
    for raw_line in research_markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("### 趋势"):
            if current:
                blocks.append(
                    {
                        "title": _clean_text(current["title"][0]) if current["title"] else "",
                        "what": _clean_text(" ".join(current["what"])),
                        "why": _clean_text(" ".join(current["why"])),
                        "implication": _clean_text(" ".join(current["implication"])),
                    }
                )
            current = {"title": [line.lstrip("# ").strip()], "what": [], "why": [], "implication": []}
            current_key = ""
            continue
        if not current:
            continue
        if line.startswith("## "):
            current_key = ""
            continue
        if line.startswith("发生了什么："):
            current_key = "what"
            value = line.split("：", 1)[1].strip()
            if value:
                current[current_key].append(value)
            continue
        if line.startswith("为什么重要："):
            current_key = "why"
            value = line.split("：", 1)[1].strip()
            if value:
                current[current_key].append(value)
            continue
        if line.startswith("意味着什么："):
            current_key = "implication"
            value = line.split("：", 1)[1].strip()
            if value:
                current[current_key].append(value)
            continue
        if line.startswith("### "):
            current_key = ""
            continue
        if current_key:
            current[current_key].append(line)

    if current:
        blocks.append(
            {
                "title": _clean_text(current["title"][0]) if current["title"] else "",
                "what": _clean_text(" ".join(current["what"])),
                "why": _clean_text(" ".join(current["why"])),
                "implication": _clean_text(" ".join(current["implication"])),
            }
        )
    return blocks[:3]


def _english_heavy(text: str) -> bool:
    letters = re.findall(r"[A-Za-z]", text or "")
    if len(letters) < 18:
        return False
    ratio = len(letters) / max(len(text), 1)
    return ratio > 0.18 or bool(re.search(r"[A-Za-z]{4,}(?:\s+[A-Za-z0-9'’&().,\-]{2,}){2,}", text or ""))


def _topic_summary_in_chinese(title: str, block_text: str) -> str:
    haystack = f"{title} {block_text}".lower()
    if any(token in haystack for token in ["hbm", "hynix", "memory", "dram", "存储"]):
        return "上游高带宽内存与算力供给"
    if any(token in haystack for token in ["anthropic", "gemini", "glm", "mistral", "model", "多模态"]):
        return "头部模型与多模态能力升级"
    if any(token in haystack for token in ["desktop", "browser", "codex", "openai", "chatgpt"]):
        return "桌面入口与 AI 工作台整合"
    if any(token in haystack for token in ["agent", "workflow", "automation", "assistant"]):
        return "智能体与工作流产品化"
    if any(token in haystack for token in ["chip", "gpu", "semiconductor", "foundry", "photonic"]):
        return "AI 基础设施与芯片链"
    return "AI 产业关键变化"


def _topic_happened_line(topic: str) -> str:
    mapping = {
        "上游高带宽内存与算力供给": "从今天的产业动态来看，资本、产能和供应链资源仍在向高带宽内存与关键算力环节集中，这说明 AI 基础设施的紧平衡状态还在持续。",
        "头部模型与多模态能力升级": "从最新动态来看，头部厂商正把竞争重点从单纯参数规模，推进到更稳定的多模态理解、工作流执行和更高门槛的行业场景。",
        "桌面入口与 AI 工作台整合": "值得注意的是，AI 能力正在被打包进桌面端、浏览器和工作台产品，入口之争已经不只是聊天界面，而是用户每天真正工作的地方。",
        "智能体与工作流产品化": "一个明显变化是，越来越多产品不再只做问答，而是开始接手流程、组织任务，并朝着结果交付型工具继续演进。",
        "AI 基础设施与芯片链": "从芯片、网络到数据中心配套，产业链上游的每一个关键环节都在被重新定价，基础设施的重要性正在快速抬升。",
    }
    return mapping.get(topic, "从今天的行业动态来看，市场关注点已经不只是模型本身，而是模型背后的供给能力、入口位置和真实使用场景。")


def _topic_implication_line(topic: str) -> str:
    mapping = {
        "上游高带宽内存与算力供给": "换句话说，谁能更稳定拿到上游资源，谁就更有机会把模型训练、推理成本和交付速度握在自己手里。",
        "头部模型与多模态能力升级": "这意味着，未来模型竞争不只是更会回答问题，而是更能理解复杂任务，并在真实流程里持续工作。",
        "桌面入口与 AI 工作台整合": "这背后的真正信号是，AI 产品正在争夺用户的第一工作界面，谁离任务更近，谁就更容易形成长期使用习惯。",
        "智能体与工作流产品化": "对于企业和个人用户来说，这种变化最直接的意义，就是 AI 开始从辅助工具走向能直接交付结果的执行系统。",
        "AI 基础设施与芯片链": "从产业视角看，基础设施的每一次紧张和扩张，都会直接影响下游模型公司和应用公司的节奏与利润空间。",
    }
    return mapping.get(topic, "更重要的是，这类变化正在把 AI 从单点能力竞争，推向整条产业链协同竞争。")


def _compact_topic_label(item: dict[str, str]) -> str:
    if is_macro_context_result(
        item.get("title", ""),
        item.get("content", ""),
        item.get("url", ""),
        item=item,
    ):
        return "国际宏观与商业背景"
    return _topic_summary_in_chinese(item.get("title", ""), item.get("content", ""))


def _selection_bucket(title: str, content: str, url: str, item: dict[str, Any] | None = None) -> str:
    haystack = f"{title} {content} {url}".lower()
    if is_macro_context_result(title, content, url, item=item) and count_ai_signals(f"{title} {content}") < 2:
        return "宏观背景"
    if any(token in haystack for token in ("model", "llm", "multimodal", "reasoning", "gemini", "gpt", "claude", "qwen", "tongyi", "gemma")):
        return "模型发布"
    if any(token in haystack for token in ("funding", "fundraise", "raised", "raises", "valuation", "valued", "acquire", "acquires", "acquisition", "merger", "invest", "investment")):
        return "资本并购"
    if any(token in haystack for token in ("chip", "cpu", "gpu", "semiconductor", "foundry", "hbm", "dram", "memory", "photon", "aicore")):
        return "芯片基础设施"
    if any(token in haystack for token in ("robot", "robotics", "humanoid", "optimus", "manufacturing")):
        return "机器人制造"
    if any(token in haystack for token in ("regulator", "regulation", "government", "ministry", "commission", "policy", "safety rules", "审查", "监管")):
        return "监管政策"
    if any(token in haystack for token in ("legal", "law", "lawyer", "court", "compliance")):
        return "法律合规"
    if any(token in haystack for token in ("biology", "biotech", "drug", "virus", "medical", "healthcare", "clinical")):
        return "生物医疗"
    if any(token in haystack for token in ("defense", "military", "army", "air force", "battlefield", "security")):
        return "国防安全"
    if any(token in haystack for token in ("agent", "assistant", "workflow", "automation", "copilot", "manus")):
        return "智能体应用"
    if any(token in haystack for token in ("android", "desktop", "browser", "workspace", "app", "developer preview", "early access")):
        return "终端产品"
    return "综合动态"


def _topic_cluster_key(title: str, content: str, url: str) -> str:
    haystack = f"{title} {content} {url}".lower()
    if "gemma 4" in haystack:
        return "gemma_4"
    if any(
        token in haystack
        for token in (
            "github copilot",
            "copilot studio",
            "microsoft 365 copilot",
            "agent-framework",
            "agent framework",
            "researcher in microsoft 365 copilot",
            "claude with researcher",
        )
    ):
        return "microsoft_copilot_agent"
    return ""


def _solo_rough_score(item: dict[str, str]) -> int:
    title = item.get("title", "")
    content = item.get("content", "")
    url = item.get("url", "")
    source_episode = str(item.get("source_episode", "") or "").strip()
    source_bucket = str(item.get("source_bucket", "") or "").strip()
    lowered = f"{title} {content} {url}".lower()
    score = source_quality_score(url) * 10
    score += count_ai_signals(f"{title} {content}") * 4
    if is_macro_context_result(title, content, url, item=item):
        score += 8
    if any(token in lowered for token in SOLO_TITLE_VERB_PATTERNS):
        score += 10
    if _extract_preserved_name(title, content):
        score += 10
    if any(
        token in lowered
        for token in (
            "product hunt",
            "github",
            "nvidia",
            "hbm",
            "micron",
            "samsung",
            "sk hynix",
            "oracle",
            "openai",
            "gemma 4",
            "claude",
            "codex",
            "agent-framework",
            "autogen",
            "sycamore",
        )
    ):
        score += 8
    if any(token in lowered for token in ("funding", "valuation", "raise", "seed", "series a")):
        score += 6
    if any(token in lowered for token in ("launch", "release", "released", "introduc", "announc", "deploy", "expand")):
        score += 8
    if any(pattern in lowered for pattern in SOLO_LOW_SIGNAL_PATTERNS):
        score -= 20
    if source_episode:
        score -= 20
        if source_bucket == "product_hunt":
            score -= 10
    if _english_heavy(title) and not _extract_preserved_name(title, content):
        score -= 12
    published_value = str(item.get("published_date") or "")
    published_dt = parse_result_datetime(published_value)
    if published_dt:
        age_days = max((now_local().astimezone(published_dt.tzinfo) - published_dt).total_seconds() / 86400.0, 0.0)
        if age_days <= 1.5:
            score += 12
        elif age_days <= 3:
            score += 5
        elif age_days > 14:
            score -= 40
        elif age_days > 7:
            score -= 18
    return score


def _solo_item_score(item: dict[str, str]) -> int:
    title = item.get("title", "")
    content = item.get("content", "")
    url = item.get("url", "")
    source_episode = str(item.get("source_episode", "") or "").strip()
    source_bucket = str(item.get("source_bucket", "") or "").strip()
    lowered = f"{title} {content} {url}".lower()
    entity = str(item.get("entity", "") or "").strip()
    action = str(item.get("action", "") or "").strip()
    fact = str(item.get("fact_metrics", "") or "").strip()
    why_it_matters = str(item.get("why_it_matters", "") or "").strip()
    confidence = float(item.get("confidence", 0.0) or 0.0)
    score = _solo_rough_score(item)
    if bool(item.get("is_valuable", True)):
        score += 18
    else:
        score -= 40
    if entity and not _entity_looks_generic(entity):
        score += 12
    if action:
        score += 10
    if fact:
        score += 12
    if why_it_matters:
        score += 6
    if confidence:
        score += round(confidence * 20)
    if any(pattern in lowered for pattern in SOLO_LOW_SIGNAL_PATTERNS):
        score -= 18
    if _entity_looks_generic(entity):
        score -= 12
    if _looks_like_banned_structured_fact(" ".join((entity, action, fact, why_it_matters))):
        score -= 60
    if source_episode:
        score -= 20
        if source_bucket == "product_hunt":
            score -= 10
    published_value = str(item.get("published_date") or item.get("publishedAt") or item.get("date") or "")
    published_dt = parse_result_datetime(published_value)
    if published_dt:
        age_days = max((now_local().astimezone(published_dt.tzinfo) - published_dt).total_seconds() / 86400.0, 0.0)
        if age_days <= 1.5:
            score += 14
        elif age_days <= 3:
            score += 6
        elif age_days > 14:
            score -= 40
        elif age_days > 7:
            score -= 18
    return score


def _preselect_solo_news_items(search_payload: dict[str, object], limit: int = 12) -> list[dict[str, str]]:
    ai_candidates = extract_items(bucket_result_items(search_payload["ai_news"]), limit=30, diversify_sectors=False)
    product_candidates = extract_items(bucket_result_items(search_payload["product_hunt"]), limit=30, diversify_sectors=False)
    combined: list[dict[str, str]] = []
    relaxed_backup: list[dict[str, str]] = []
    seen_keys: set[str] = set()

    for item in ai_candidates + product_candidates:
        title = _clean_text(item.get("title", ""))
        content = _clean_text(item.get("content", ""))
        url = item.get("url", "")
        source_episode = str(item.get("source_episode", "") or "").strip()
        source_bucket = str(item.get("source_bucket", "") or "").strip()
        lowered_title = _strip_source_suffix(title).lower()
        lowered_body = _clean_text(content).lower()
        if not title:
            continue
        if source_episode and source_bucket == "product_hunt":
            if any(pattern in lowered_title for pattern in SOLO_LOW_SIGNAL_PATTERNS):
                continue
            if any(marker in lowered_title for marker in ("guide", "last 7 days", "this week")):
                continue
            if "fujitsu" in lowered_body and "specific" in lowered_body:
                continue
            if source_quality_score(url) < 4:
                continue
        if not _is_solo_scriptworthy(title, content, url, item=item):
            continue
        dedupe_key = _normalize_for_dedupe(title, url)
        if dedupe_key in seen_keys:
            continue
        normalized_item = {
            "title": _strip_source_suffix(title),
            "content": content,
            "url": url,
            "topic": _compact_topic_label({"title": title, "content": content, "url": url, "feed_category": str(item.get("feed_category", "") or "")}),
            "bucket": _selection_bucket(title, content, url, item=item),
            "cluster": _topic_cluster_key(title, content, url),
            "published_date": item.get("published_date") or item.get("publishedAt") or item.get("date") or "",
            "source_episode": source_episode,
            "source_bucket": source_bucket,
            "feed_category": str(item.get("feed_category", "") or "").strip(),
        }
        published_value = str(normalized_item.get("published_date") or "")
        published_dt = parse_result_datetime(published_value)
        if published_dt:
            age_days = max((now_local().astimezone(published_dt.tzinfo) - published_dt).total_seconds() / 86400.0, 0.0)
            if age_days > 21:
                continue
        score = _solo_rough_score(normalized_item)
        if score < 18:
            # 分数偏低的条目暂存，当"平淡日"候选不足时再兜底使用
            if score >= 10:
                relaxed_backup.append(normalized_item)
            continue
        combined.append(normalized_item)
        seen_keys.add(dedupe_key)

    # 平淡日兜底第一层：如果达标条目不足 8 条，把 relaxed_backup 中得分较高的补进来
    SOLO_PRESELECT_FLOOR = 8
    if len(combined) < SOLO_PRESELECT_FLOOR and relaxed_backup:
        relaxed_backup.sort(key=_solo_rough_score, reverse=True)
        needed = SOLO_PRESELECT_FLOOR - len(combined)
        added_cnt = 0
        for item in relaxed_backup[:needed * 2]:
            dedupe_key = _normalize_for_dedupe(item["title"], item["url"])
            if dedupe_key in seen_keys:
                continue
            combined.append(item)
            seen_keys.add(dedupe_key)
            added_cnt += 1
            if added_cnt >= needed:
                break
        print(f"[solo] 平淡日兜底(一层)：补入 {added_cnt} 条 relaxed 条目，当前 {len(combined)}", file=sys.stderr)

    # 平淡日兜底第二层：依然不足 8 条时，绕过 extract_items 的 anchor+sector 去重，
    # 直接从 bucket_result_items 里再扫一遍，只要标题与已选不同就允许进入。
    # 原因：extract_items 会合并同一 anchor 下的多条报道为 1 条，
    # 在"今天只有 3-4 个 anchor"的情况下会把整期压到 3-4 条，无视 B2/B3 补入的 backlog 条目。
    if len(combined) < 8:
        extra_pool: list[dict[str, Any]] = []
        for b in ("ai_news", "product_hunt"):
            for raw in bucket_result_items(search_payload.get(b, {})):
                extra_pool.append({**raw, "source_bucket": raw.get("source_bucket") or b})
        added_cnt = 0
        for item in extra_pool:
            title = _clean_text(item.get("title", ""))
            content = _clean_text(item.get("content", ""))
            url = item.get("url", "")
            if not title or len(title) > 140:
                continue
            if not _is_solo_scriptworthy(title, content, url, item=item):
                continue
            dedupe_key = _normalize_for_dedupe(title, url)
            if dedupe_key in seen_keys:
                continue
            normalized_item = {
                "title": _strip_source_suffix(title),
                "content": content,
                "url": url,
                "topic": _compact_topic_label({"title": title, "content": content, "url": url, "feed_category": str(item.get("feed_category", "") or "")}),
                "bucket": _selection_bucket(title, content, url, item=item),
                "cluster": _topic_cluster_key(title, content, url),
                "published_date": item.get("published_date") or item.get("publishedAt") or item.get("date") or "",
                "source_episode": str(item.get("source_episode", "") or "").strip(),
                "source_bucket": str(item.get("source_bucket", "") or "").strip(),
                "feed_category": str(item.get("feed_category", "") or "").strip(),
            }
            # 年龄限制：21 天
            pdt = parse_result_datetime(str(normalized_item["published_date"]))
            if pdt:
                age_days = max((now_local().astimezone(pdt.tzinfo) - pdt).total_seconds() / 86400.0, 0.0)
                if age_days > 21:
                    continue
            # 平淡日兜底阶段，放宽分数门槛（backlog 条目有 -20 分惩罚，容易到负分）
            if _solo_rough_score(normalized_item) < -8:
                continue
            combined.append(normalized_item)
            seen_keys.add(dedupe_key)
            added_cnt += 1
            if len(combined) >= 8:
                break
        if added_cnt:
            print(f"[solo] 平淡日兜底(二层/绕过 anchor 去重)：补入 {added_cnt} 条", file=sys.stderr)

    combined.sort(key=_solo_rough_score, reverse=True)
    return combined[: max(limit, SOLO_STRUCTURED_EXTRACT_BATCH_LIMIT)]


def _solo_entity_focus_key(item: dict[str, str | float | bool]) -> str:
    entity = str(item.get("entity", "") or "").strip().lower()
    bucket = str(item.get("bucket", "") or "").strip()
    if not entity:
        return ""
    return f"{entity}|{bucket}"


def _select_solo_news_items(search_payload: dict[str, object], limit: int | None = 5) -> list[dict[str, str]]:
    requested_limit = limit
    if requested_limit is None:
        requested_limit = max(count_payload_candidates(search_payload), SOLO_STRUCTURED_EXTRACT_BATCH_LIMIT)
    requested_limit = max(int(requested_limit), 1)
    raw_structured = _extract_structured_solo_items(
        _preselect_solo_news_items(
            search_payload,
            limit=max(requested_limit * 2, SOLO_STRUCTURED_EXTRACT_BATCH_LIMIT),
        )
    )
    combined = [item for item in raw_structured if bool(item.get("is_valuable", True))]
    # 平淡日兜底：valuable 池 < 5 时，把 confidence>=0.35 的 is_valuable=false 条目也放进来，
    # 避免某一天因为 LLM 对边缘新闻判死刑导致整期只剩 3-4 条。
    if len(combined) < 5:
        borderline = [
            item for item in raw_structured
            if not bool(item.get("is_valuable", True))
            and float(item.get("confidence", 0.0) or 0.0) >= 0.35
            and str(item.get("discard_reason", "")) not in ("banned_pattern",)
        ]
        if borderline:
            borderline.sort(key=_solo_item_score, reverse=True)
            needed = 5 - len(combined)
            combined.extend(borderline[:needed])
            print(f"[solo] 结构化兜底：补入 {min(needed, len(borderline))} 条边缘 valuable=false 条目", file=sys.stderr)
    combined.sort(key=_solo_item_score, reverse=True)
    selected: list[dict[str, str]] = []
    used_entities: set[str] = set()
    used_clusters: set[str] = set()

    for item in combined:
        cluster = str(item.get("cluster", "")).strip()
        entity = _solo_entity_focus_key(item)
        if (cluster and cluster in used_clusters) or (entity and entity in used_entities):
            continue
        selected.append(item)
        if cluster:
            used_clusters.add(cluster)
        if entity:
            used_entities.add(entity)
        if len(selected) >= requested_limit:
            return selected

    for item in combined:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= requested_limit:
            break
    return selected


def _solo_story_target(search_payload: dict[str, object]) -> int:
    available = len(_select_solo_news_items(search_payload, limit=None))
    if available <= 0:
        return SOLO_MIN_STORY_COUNT
    return available


def _solo_script_length_bounds(story_count: int) -> tuple[int, int]:
    extra_stories = max(story_count - SOLO_MIN_STORY_COUNT, 0)
    max_target = SOLO_SCRIPT_TARGET_MAX
    min_target = min(SOLO_SCRIPT_TARGET_MIN + extra_stories * 120, max_target - 240)
    return min_target, max_target


def _solo_story_floor(story_count: int) -> int:
    return max(story_count, 0)


def _build_solo_raw_news_digest(search_payload: dict[str, object], limit: int = 12) -> str:
    items = _select_solo_news_items(search_payload, limit=limit)
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. 标题：{item['title']}")
        if item["content"]:
            lines.append(f"摘要：{_localize_numbers(item['content'][:320])}")
        lines.append("")
    return "\n".join(lines).strip()


def _build_solo_news_brief(search_payload: dict[str, object]) -> str:
    items = _select_solo_news_items(search_payload, limit=6)
    return _build_solo_news_brief_from_items(items)


SOLO_PRESERVED_NAME_PATTERNS = (
    re.compile(r"\bGemma\s*\d+(?:\.\d+)?\b", flags=re.I),
    re.compile(r"\bGPT[- ]?\d+(?:\.\d+)?(?:-Codex)?\b", flags=re.I),
    re.compile(r"\bClaude(?:\s+(?:Sonnet|Opus))?\s*\d+(?:\.\d+)?\b", flags=re.I),
    re.compile(r"\bCodex\b", flags=re.I),
    re.compile(r"\bCopilot\b", flags=re.I),
    re.compile(r"\bLlama\s*\d+(?:\.\d+)?\b", flags=re.I),
    re.compile(r"\bQwen\s*\d+(?:\.\d+)?\b", flags=re.I),
    re.compile(r"\bDeepSeek(?:[- ]?[A-Za-z0-9.]+)?\b", flags=re.I),
    re.compile(r"\bHBM\d+\b", flags=re.I),
    re.compile(r"\bAICore\b", flags=re.I),
    re.compile(r"\bagent-framework\b", flags=re.I),
)


def _extract_preserved_name(title: str, content: str) -> str:
    for pattern in SOLO_PRESERVED_NAME_PATTERNS:
        match = pattern.search(title)
        if match:
            return match.group(0).strip()
    for pattern in SOLO_PRESERVED_NAME_PATTERNS:
        match = pattern.search(content)
        if match:
            return match.group(0).strip()
    repo_match = re.match(r"GitHub\s*-\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", title, flags=re.I)
    if repo_match:
        return repo_match.group(1).strip()
    release_repo_match = re.match(r"Releases\s*[·•]\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", title, flags=re.I)
    if release_repo_match:
        return release_repo_match.group(1).strip()
    return ""


def _structured_title(item: dict[str, str | float | bool]) -> str:
    natural_title = _clean_optional_text(item.get("natural_title"), limit=86)
    if natural_title:
        return natural_title
    return _trim_text(_strip_source_suffix(str(item.get("title", "") or "")), limit=86)


def _structured_fact_line(item: dict[str, str | float | bool]) -> str:
    fact = _clean_optional_text(item.get("fact_metrics"), limit=92)
    if fact and re.search(r"[\u4e00-\u9fff]", fact):
        return fact
    why = _clean_optional_text(item.get("why_it_matters"), limit=88)
    if why and re.search(r"[\u4e00-\u9fff]", why):
        return why
    content = _clean_text(str(item.get("content", "") or ""))
    sentence = re.split(r"(?<=[.!?。！？])\s+", _localize_numbers(content))[0].strip()
    if sentence and not _english_heavy(sentence) and len(sentence) >= 16:
        return _trim_text(sentence.rstrip("。") + "。", limit=92)
    return ""


def _structured_detail_line(item: dict[str, str | float | bool]) -> str:
    details = []
    fact = _clean_optional_text(item.get("fact_metrics"), limit=92)
    why = _clean_optional_text(item.get("why_it_matters"), limit=88)
    if fact:
        details.append(fact)
    if why and why != fact:
        details.append(why)
    if details:
        return _trim_text("；".join(details), limit=96)
    return ""


def _build_solo_news_brief_from_items(items: list[dict[str, str | float | bool]]) -> str:
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. 事件标题：{_structured_title(item)}")
        entity = _clean_optional_text(item.get("entity"), limit=36)
        action = _clean_optional_text(item.get("action"), limit=20)
        fact = _structured_fact_line(item)
        if entity:
            lines.append(f"主体：{entity}")
        if action:
            lines.append(f"动作：{action}")
        if fact:
            lines.append(f"事实：{fact}")
        lines.append("")
    return "\n".join(lines).strip()


def _build_solo_news_cards_from_items(items: list[dict[str, str | float | bool]]) -> str:
    cards: list[str] = []
    for index, item in enumerate(items, start=1):
        title = _structured_title(item)
        source = _trim_text(str(item.get("url", "")).split("//")[-1].split("/", 1)[0].removeprefix("www."), limit=36)
        published = str(item.get("published_date", "") or "").strip()
        card_lines = [f"{index}. 标题：{title}"]
        bucket = _clean_optional_text(item.get("bucket"), limit=16)
        topic = _clean_optional_text(item.get("topic"), limit=20)
        if bucket:
            card_lines.append(f"板块：{bucket}")
        if topic and topic != bucket:
            card_lines.append(f"主题：{topic}")
        entity = _clean_optional_text(item.get("entity"), limit=40)
        if entity and not _entity_looks_generic(entity):
            card_lines.append(f"主体：{entity}")
        action = _clean_optional_text(item.get("action"), limit=20)
        if action:
            card_lines.append(f"动作：{action}")
        fact = _structured_fact_line(item)
        if fact:
            card_lines.append(f"核心事实：{fact}")
        detail = _structured_detail_line(item)
        if detail and detail != fact:
            card_lines.append(f"补充细节：{detail}")
        if published:
            card_lines.append(f"时间：{published[:10]}")
        if source:
            card_lines.append(f"来源：{source}")
        cards.append("\n".join(card_lines))
    return "\n\n".join(cards).strip()


def _build_solo_news_cards(search_payload: dict[str, object], limit: int = 6) -> str:
    items = _select_solo_news_items(search_payload, limit=limit)
    return _build_solo_news_cards_from_items(items)


def _build_solo_angle_note(research_markdown: str) -> str:
    metadata = parse_research_metadata(research_markdown)
    trend_titles = [title for title in metadata.get("trend_titles", []) if title]
    if trend_titles:
        return "；".join(_trim_text(title, limit=28) for title in trend_titles[:3])
    return _trim_text(_compress_deep_research(_extract_section(research_markdown, "## 深度研究")), limit=120)


def _compress_deep_research(text: str) -> str:
    cleaned = _clean_text(text)
    sentences = re.split(r"(?<=[。！？])", cleaned)
    kept: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence or _english_heavy(sentence):
            continue
        if sentence.startswith("## ") or sentence.startswith("### "):
            continue
        kept.append(sentence)
        if len("".join(kept)) >= 220:
            break
    return "".join(kept)[:260]


def _compose_trend_paragraph(index: int, trend: dict[str, str]) -> str:
    block_text = " ".join([trend.get("what", ""), trend.get("why", ""), trend.get("implication", "")]).strip()
    topic = _topic_summary_in_chinese(trend.get("title", ""), block_text)
    what = trend.get("what", "")
    why = trend.get("why", "")
    implication = trend.get("implication", "")

    if _english_heavy(what) or len(what) < 20:
        what = _topic_happened_line(topic)
    if _english_heavy(why) or len(why) < 20:
        why = "这件事之所以重要，在于它反映的不是单点变化，而是行业重心正在移动。"
    if _english_heavy(implication) or len(implication) < 20:
        implication = "这也意味着，接下来更值得关注的，是谁能把能力真正落到产品、入口和交付结果上。"

    openers = {
        1: "首先关注第一条消息。",
        2: "接下来请关注第二条消息。",
        3: "此外，第三条消息同样值得关注。",
    }
    opener = openers.get(index, "下面继续关注一条重要动态。")
    return (
        f"{opener}{topic}正在成为今天最值得看的方向。"
        f"{what}"
        f"{why}"
        f"{implication}"
    )


def _build_solo_fallback_script(research_markdown: str) -> str:
    trend_blocks = _extract_trend_blocks(research_markdown)
    date_str = now_local().strftime("%Y年%-m月%-d日")
    deep_research = _compress_deep_research(_extract_section(research_markdown, "## 深度研究"))

    if not trend_blocks:
        meta = parse_research_metadata(research_markdown)
        trend_blocks = [
            {
                "title": title,
                "what": "",
                "why": "这条变化说明 AI 竞争正在从单点能力走向更完整的产业协同。",
                "implication": "未来更有价值的，不只是模型能力本身，而是把能力转成结果的系统。",
            }
            for title in (meta["trend_titles"] or ["AI产业关键变化"])
        ][:3]

    headlines = "、".join(t["title"] for t in trend_blocks[:3] if t.get("title"))[:60] or "AI产业关键变化"
    body_parts = [SOLO_OPENING_TEMPLATE.format(date_str=date_str, headlines=headlines)]
    for index, trend in enumerate(trend_blocks[:3], start=1):
        body_parts.append(_compose_trend_paragraph(index, trend))

    if deep_research:
        body_parts.append(f"综合来看，{deep_research}")

    body_parts.append(SOLO_CLOSING_LINE)
    return "\n\n".join(body_parts)


def _build_solo_quick_fallback_script(search_payload: dict[str, object], research_markdown: str) -> str:
    items = _select_solo_news_items(search_payload, limit=6)
    date_str = now_local().strftime("%Y年%m月%d日")
    if not items:
        return _build_solo_fallback_script(research_markdown)

    openers = [
        "先看第一条。",
        "接下来。",
        "第三条。",
        "此外。",
        "第五条。",
        "最后一条。",
    ]
    # Build quick headlines for opening
    headlines_parts = []
    for item in items[:3]:
        entity = _clean_optional_text(item.get("entity"), limit=20)
        action = _clean_optional_text(item.get("action"), limit=16)
        title = _structured_title(item)
        headlines_parts.append(f"{entity}{action}" if entity and action else title)
    headlines = "；".join(headlines_parts)[:80] or "多条AI产业重要消息"
    lines = [SOLO_OPENING_TEMPLATE.format(date_str=date_str, headlines=headlines)]
    for index, item in enumerate(items, start=1):
        entity = _clean_optional_text(item.get("entity"), limit=20)
        display_entity = entity or _structured_title(item)
        action = _clean_optional_text(item.get("action"), limit=16)
        fact = _structured_fact_line(item)
        opener = openers[index - 1] if index <= len(openers) else "此外。"
        fact_plain = fact.rstrip("。").strip()
        if fact and (fact_plain.startswith(display_entity) or fact_plain.startswith(entity)):
            lines.append(f"{opener}{fact.rstrip()}")
        elif fact:
            lines.append(f"{opener}{display_entity}{action}。{fact}")
        else:
            lines.append(f"{opener}{display_entity}{action}。")
    lines.append(SOLO_CLOSING_LINE)
    return "\n\n".join(lines)


__all__ = [name for name in globals() if not name.startswith("__")]
