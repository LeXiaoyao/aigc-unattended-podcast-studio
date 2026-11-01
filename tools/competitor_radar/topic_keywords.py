from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any


DEFAULT_HIGH_VALUE_ENTITIES: dict[str, list[str]] = {
    "companies": [
        "OpenAI",
        "Anthropic",
        "Google",
        "Meta",
        "Microsoft",
        "Apple",
        "Nvidia",
        "NVIDIA",
        "Perplexity",
        "xAI",
        "ByteDance",
        "Alibaba",
        "Tencent",
        "DeepSeek",
        "Moonshot",
        "MiniMax",
        "Zhipu",
        "Baidu",
        "Amazon",
        "AWS",
        "Huawei",
        "Xiaomi",
        "Cursor",
        "Lovable",
        "Replit",
        "Windsurf",
        "Manus",
        "Shopify",
        "Duolingo",
        "Wiz",
    ],
    "products": [
        "ChatGPT",
        "Claude",
        "Gemini",
        "Codex",
        "Cursor",
        "Claude Code",
        "NotebookLM",
        "Sora",
        "Veo",
        "Manus",
        "Lovable",
        "Replit",
        "Windsurf",
        "Copilot",
        "GPT-5.4",
        "GPT-5.5",
        "GPT-Image-2",
        "MiMo",
        "Ascend",
        "Bedrock",
    ],
    "topics": [
        "AI agent",
        "coding agent",
        "open source model",
        "multimodal",
        "video generation",
        "voice cloning",
        "AI search",
        "AI browser",
        "AI hardware",
        "model release",
        "funding",
        "acquisition",
        "pricing",
        "subscription",
        "API",
        "developer tool",
        "Community Notes",
    ],
}


CATEGORY_RULES: dict[str, list[str]] = {
    "AI模型": [
        "model",
        "模型",
        "GPT",
        "Claude",
        "Gemini",
        "DeepSeek",
        "Qwen",
        "Llama",
        "开源模型",
        "多模态",
        "multimodal",
    ],
    "AI Agent": ["agent", "智能体", "Operator", "Manus", "AutoGPT", "代理"],
    "AI编程工具": ["coding", "Codex", "Cursor", "Windsurf", "Copilot", "Claude Code", "Replit", "编程", "代码"],
    "开源项目": ["open source", "开源", "GitHub", "Hugging Face", "MIT 协议"],
    "大厂产品更新": ["发布", "上线", "update", "launch", "推出", "升级", "Google", "Meta", "Microsoft", "Apple", "OpenAI"],
    "融资创业": ["融资", "funding", "估值", "acquired", "收购", "startup", "创业"],
    "消费电子": ["iPhone", "Mac", "Vision Pro", "手机", "硬件", "芯片", "机器人", "drone", "无人机"],
    "泛科技公司动态": ["裁员", "财报", "capex", "营收", "利润", "公司", "战略", "CEO", "股价"],
    "开发者工具": ["API", "SDK", "developer", "devtool", "IDE", "GitHub", "workflow"],
    "监管政策": ["监管", "policy", "lawsuit", "诉讼", "合规", "欧盟", "FTC", "下架"],
    "商业化定价": ["pricing", "price", "订阅", "涨价", "套餐", "付费", "subscription", "token"],
    "内容创作工具": ["video", "image", "voice", "Sora", "Veo", "剪辑", "创作", "播客", "生成视频"],
}


EVENT_KEYWORDS = [
    "发布",
    "上线",
    "融资",
    "收购",
    "涨价",
    "开源",
    "下架",
    "监管",
    "测试版",
    "API",
    "Agent",
    "coding",
    "launch",
    "funding",
    "acquisition",
    "pricing",
    "subscription",
    "lawsuit",
    "earnings",
    "capex",
]


LOW_VALUE_HINTS = [
    "闲聊",
    "随想",
    "感想",
    "我的看法",
    "吐槽",
    "AMA",
    "Q&A",
    "newsletter",
]


TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,}|[\u4e00-\u9fff]{2,}")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return normalize_space(text)


def merge_high_value_entities(config: dict[str, Any] | None = None) -> dict[str, list[str]]:
    merged = {key: list(values) for key, values in DEFAULT_HIGH_VALUE_ENTITIES.items()}
    configured = ((config or {}).get("high_value_entities") or {})
    for group, values in configured.items():
        if isinstance(values, list):
            merged.setdefault(group, [])
            for value in values:
                if value and str(value) not in merged[group]:
                    merged[group].append(str(value))
    return merged


def _contains_term(text: str, term: str) -> bool:
    if not term:
        return False
    escaped = re.escape(term)
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in text
    return re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None


def extract_entities(text: str, config: dict[str, Any] | None = None) -> list[str]:
    text = normalize_space(text)
    found: OrderedDict[str, None] = OrderedDict()
    for values in merge_high_value_entities(config).values():
        for entity in values:
            if _contains_term(text, entity):
                canonical = "Nvidia" if entity == "NVIDIA" else entity
                found[canonical] = None
    return list(found.keys())


def classify_topic_types(text: str, config: dict[str, Any] | None = None) -> list[str]:
    text = normalize_space(text)
    found: list[str] = []
    configured = (config or {}).get("topic_categories")
    allowed = set(configured) if isinstance(configured, list) and configured else None
    for category, terms in CATEGORY_RULES.items():
        if allowed and category not in allowed:
            continue
        if any(_contains_term(text, term) for term in terms):
            found.append(category)
    return found or ["其他"]


def extract_keywords(text: str, config: dict[str, Any] | None = None, max_count: int = 16) -> list[str]:
    text = normalize_space(text)
    found: OrderedDict[str, None] = OrderedDict()
    for entity in extract_entities(text, config):
        found[entity] = None
    for keyword in EVENT_KEYWORDS:
        if _contains_term(text, keyword):
            found[keyword] = None
    for token in TOKEN_RE.findall(text):
        if len(token) < 3 and not re.search(r"[\u4e00-\u9fff]", token):
            continue
        if token.lower() in {"the", "and", "for", "with", "from", "this", "that", "your", "about"}:
            continue
        if len(found) >= max_count:
            break
        found[token] = None
    return list(found.keys())[:max_count]


def title_candidate_keywords(title: str, config: dict[str, Any] | None = None, max_count: int = 6) -> list[str]:
    title = normalize_space(title)
    entities = extract_entities(title, config)
    candidates: OrderedDict[str, None] = OrderedDict((entity, None) for entity in entities)
    for acronym in re.findall(r"(?<![A-Za-z])(?:[A-Z]{2,}|AI\+[A-Za-z0-9+.-]+)(?![A-Za-z])", title):
        if acronym not in {"AI", "API"}:
            candidates[acronym] = None
    phrases = re.findall(r"[A-Z][A-Za-z0-9+.-]*(?:\s+[A-Z][A-Za-z0-9+.-]*){0,3}", title)
    for phrase in phrases:
        phrase = normalize_space(phrase)
        if phrase in {"AI", "API", "PM"}:
            continue
        if phrase.endswith(" AI") or len(phrase) >= 4:
            candidates[phrase] = None
    for token in extract_keywords(title, config, max_count=12):
        if token in {"AI", "API", "Agent", "coding", "发布", "上线"}:
            continue
        if re.search(r"[\u4e00-\u9fff]", token) and not (
            len(token) <= 8 and any(mark in token for mark in ("AI", "模型", "智能体", "芯片", "机器人", "量子", "开源"))
        ):
            continue
        candidates[token] = None
    return list(candidates.keys())[:max_count]


def summarize_topic(title: str, body: str, config: dict[str, Any] | None = None) -> str:
    title = normalize_space(title)
    text = normalize_space(f"{title} {body}")
    entities = extract_entities(text, config)
    categories = [category for category in classify_topic_types(text, config) if category != "其他"]
    if entities and categories:
        return f"围绕 {', '.join(entities[:4])} 的{categories[0]}动态，重点是{title or '相关更新'}。"
    if entities:
        return f"围绕 {', '.join(entities[:4])} 的最新动态，重点是{title or '相关更新'}。"
    if title:
        return f"这一期主要讨论「{title}」相关的泛科技话题。"
    return "未能从标题或摘要中提取清晰主题。"


def enrich_topic_item(item: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    text = normalize_space(" ".join(str(item.get(key) or "") for key in ("title", "summary", "shownotes", "raw_text")))
    enriched = dict(item)
    enriched["topics"] = classify_topic_types(text, config)
    enriched["entities"] = extract_entities(text, config)
    enriched["keywords"] = extract_keywords(text, config)
    enriched["topic_summary"] = summarize_topic(str(item.get("title") or ""), text, config)
    return enriched


def is_low_value_topic(item: dict[str, Any], config: dict[str, Any] | None = None) -> bool:
    text = normalize_space(" ".join(str(item.get(key) or "") for key in ("title", "summary", "shownotes", "raw_text")))
    if item.get("entities"):
        return False
    if any(category != "其他" for category in item.get("topics") or []):
        return False
    lowered = text.lower()
    return any(hint.lower() in lowered for hint in LOW_VALUE_HINTS) or len(text) < 12
