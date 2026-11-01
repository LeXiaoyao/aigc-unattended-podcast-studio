"""Shared constants and helpers for the modular solo pipeline."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parent.parent
EPISODES_DIR = ROOT_DIR / "episodes"
ENV_FILE = ROOT_DIR / ".env.listenhub"
SHARED_ENV_FILE = ROOT_DIR / "shared" / "config" / ".env.listenhub"
ENV_FILES = (ENV_FILE, SHARED_ENV_FILE)
RULES_CONFIG_PATH = ROOT_DIR / "news_pipeline_rules.json"
sys.path.insert(0, str(ROOT_DIR))

from ai_news_pipeline import (
    now_local,
    next_episode_id,
    ensure_dir,
    read_json,
    write_json,
    write_text,
    append_record_csv,
    collect_search_data,
    create_solo_research_markdown,
    parse_research_metadata,
    summarize_sources,
    load_recent_coverage_context,
    filter_recently_covered_search_payload,
    supplement_search_payload_with_recent_uncovered_items,
    hard_time_cutoff_filter,
    extract_recent_script_entities,
    hard_history_block_filter,
    count_payload_candidates,
    load_coverage_index,
    revive_unbroadcast_from_pools,
    inject_backlog_candidates,
    persist_drop_pools_to_backlog,
    update_coverage_index_for_episode,
    bucket_result_items,
    extract_items,
    summarize_search_quality,
    count_ai_signals,
    is_relevant_ai_result,
    is_macro_context_result,
    normalize_title_key,
    source_quality_score,
    parse_result_datetime,
    host_for_url,
    regenerate_cover_for_episode,
    notify,
    inject_follow_builder_into_payload,
)
from minimax_tts import minimax_tts, minimax_tts_chunks, minimax_tts_with_alignment
from audio_mixer import (
    mix_podcast_audio,
    mix_podcast_from_chunks,
    generate_highlight_clip,
    voice_start_delay_ms,
)
from daily_pipeline_log import (
    log_stage as _daily_log_stage,
    log_error as _daily_log_error,
    log_llm_call as _daily_log_llm_call,
    set_current_episode as _daily_log_set_episode,
)


def _load_env() -> None:
    """从本地 env 文件加载环境变量（兼容含空格/括号的值）。"""
    for env_file in ENV_FILES:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip('"'))


def _notify_feishu(title: str, body: str) -> None:
    """发送飞书机器人消息（静默失败）。"""
    try:
        from mobile_notify import notify as _mn
        _mn(title, body, desktop=True)
    except Exception as e:
        print(f"[notify] 飞书通知失败: {e}", file=sys.stderr)


def _notify_feishu_rich(title: str, sections: list[dict[str, str]], *, desktop: bool = False) -> None:
    try:
        from mobile_notify import notify_rich as _notify_rich
        _notify_rich(title, sections, desktop=desktop)
    except Exception as exc:
        print(f"[notify] 飞书富文本通知失败: {exc}", file=sys.stderr)


def _update_record(episode_dir: Path, **updates: object) -> dict[str, object]:
    record_path = episode_dir / "record.json"
    record = read_json(record_path) if record_path.exists() else {}
    record.update({key: value for key, value in updates.items() if value is not None})
    write_json(record_path, record)
    return record


def _cover_retry_meta_path(episode_dir: Path) -> Path:
    return episode_dir / "cover_retry.json"


def _notify_step_failure(
    *,
    episode: str,
    step: str,
    error: str,
    title: str = "",
    next_step: str = "",
) -> None:
    sections = [
        {"label": "失败步骤", "text": step},
        {"label": "期号", "text": episode},
    ]
    if title:
        sections.append({"label": "标题", "text": title})
    sections.append({"label": "错误", "text": _trim_text(error, limit=320)})
    if next_step:
        sections.append({"label": "下一步", "text": next_step})
    _notify_feishu_rich("AI观测站 · 流程失败", sections, desktop=False)


def _format_mmss(total_seconds: int) -> str:
    safe_seconds = max(int(total_seconds), 0)
    minutes, seconds = divmod(safe_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


SOLO_OPENING_TEMPLATE = (
    "嗨，早上好，今天是{date_str}。"
    "先用三十秒快速过一下今天的重点：{headlines}"
    "好，我们一条一条来看。"
)
SOLO_CLOSING_LINE = (
    "以上就是今天的AI产业观察，感谢收听，我们明天再见。"
)
SOLO_FIXED_OPENING_HINTS = (
    "早上好",
    "AI观测站",
    "每日晨间AI播报",
    "今日播报",
    "今日AI产业重要动态",
)
SOLO_FIXED_CLOSING_HINTS = (
    "今天的播报到这里结束",
    "感谢您的收听",
    "感谢收听",
    "以上就是今天的AI产业观察",
    "以上是今天的AI产业全链路观察",
    "欢迎订阅AI观测站",
    "我们明天同一时间再见",
    "我们明天见",
    "我们明天再见",
)
SOLO_TITLE_VISIBLE_LIMIT = 36
SOLO_TITLE_BODY_VISIBLE_LIMIT = 24
SOLO_SHOWNOTE_BULLET_LIMIT = 10
SOLO_SHOWNOTE_TIMELINE_LIMIT = 16
SOLO_MIN_STORY_COUNT = 6
SOLO_MAX_STORY_COUNT = 20
SOLO_SHOWNOTE_TRANSITION_PATTERNS = (
    r"好，我们一条一条来看",
    r"先看(?:第?[一二三四五六七八九十\d]+条)?",
    r"先看[^。！？!?]{0,16}",
    r"首先(?:来关注|关注)?(?:今天的)?第?[一二三四五六七八九十\d]+条(?:消息|动态)?",
    r"接下来(?:是|请关注)?第?[一二三四五六七八九十\d]+条(?:消息|动态)?",
    r"再看[^。！？!?]{0,16}",
    r"第[一二三四五六七八九十\d]+条(?:是关于[^。！？]*|值得关注的动态|是美光的另一则消息|消息)?",
    # codex 风格的"第X条 + 动词承接"过渡句，如"第二条看电力""第三条转到烧钱速度"
    r"第[一二三四五六七八九十\d]+条(?:看|转到|讲|聊聊|聊|关注|说|来到|换到|进入)[^。！？!?]{0,20}",
    r"下面(?:请)?关注第?[一二三四五六七八九十\d]+条(?:消息|动态)?",
    r"此外(?:还有一条重要消息)?",
    r"最后看[^。！？!?]{0,16}",
    r"最后(?:再看)?(?:一条(?:动态|消息)|一条(?:是[^。！？]*)?)?",
    r"然后(?:我们)?(?:关注一下|继续关注|再看|看一下)?[^。！？]*",
)
SOLO_SHOWNOTE_DISCOURSE_PREFIXES = (
    "最近",
    "目前",
    "现在",
    "今天",
    "有消息称",
    "消息称",
    "据悉",
    "据报道",
    "报道称",
    "另外",
    "此外",
)
SOLO_SHOWNOTE_COMMENTARY_PREFIXES = (
    "这说明",
    "这意味着",
    "这也说明",
    "这也意味着",
    "这预示着",
    "如果觉得有用",
    "欢迎订阅AI观测站",
    "感谢收听AI观测站",
    "我们明天同一时间再见",
)
# Codex 风格的判断段/边界段/承接段开头。这些段不是新闻，不应被计入新闻条数，
# 也不应作为 shownotes bullet 候选。Codex 把每条新闻拆成"事件段 + 判断段"两段，
# 如果不剔除判断段，会让 _count_script_news_paragraphs 把 6 条新闻误算成 12 条。
SOLO_SCRIPT_COMMENTARY_LEADS = (
    "这条新闻的边界",
    "这个数字的边界",
    "这个数字也要小心",
    "这个数字要小心",
    "这个数字听起来",
    "这里也要划清口径",
    "这里不能把",
    "这条来源只是",
    "这不是一个",
    "这条新闻听起来",
    "这条新闻里",
    "这条听起来",
    "我的判断是",
    "更稳妥的看法",
    "真正要看的是",
    "前几期我们讲过",
    "昨天我们讲过",
    "上周我们讲过",
    "把今天这",
    "把这几条",
    "把这六条",
    "真正跑赢这一轮",
    "如果今天这些新闻",
    "把今天的",
)
TEXT_MARKER_START = "[[CODEX_TEXT_START]]"
TEXT_MARKER_END = "[[CODEX_TEXT_END]]"
SOLO_LOW_SIGNAL_PATTERNS = [
    "newsletter",
    "watch live",
    "outlook",
    "vs.",
    "hits and misses",
    "buying opportunity",
    "medical leave",
    "writer apologizes",
    "ancient chinese game",
    "dangerously close",
    "talks up",
    "industry report projects",
    "best ai agents",
    "best ai coding agents",
    "expert picks",
    "highlights the scientific unknowns",
    "redefining the enterprise attack surface",
    "hint at growing",
    "needs more habits",
    "basketball",
    "training camp",
    "football score",
    "hockey",
    "supergirl",
    "hollywood",
    "movie trailer",
    "sports betting",
    "fertiliser",
    "suborbital flight",
]
SOLO_LOW_SIGNAL_DOMAINS = [
    "nypost.com",
    "fool.com",
    "kotaku.com",
    "espn.com",
    "nhl.com",
    "foxsports.com",
    "hollywoodreporter.com",
    "skynews.com.au",
    "foxnews.com",
]
SOLO_GENERIC_PAGE_PATTERNS = [
    "your ai pair programmer",
    "artificial intelligence | product hunt",
    "product hunt topic",
    "trending repositories on github today",
    "explore github",
]
SOLO_GENERIC_PAGE_URL_PARTS = [
    "/topics/",
    "/features/copilot",
    "/trending",
    "/explore",
]
SOLO_QUALITY_RISK_PATTERNS = (
    "显示出巨大的潜力",
    "显示出市场对其技术实力和前景的高度认可",
    "可能进一步推动相关需求增长",
    "预计未来几年内有望达到数十亿美元",
    "这是一项满足AI需求的关键封装技术",
    "随着AI应用的不断扩展，对高性能存储和计算的需求也在持续增长",
)


def _load_rules_overrides() -> dict[str, object]:
    if not RULES_CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(RULES_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[rules] 读取配置失败: {exc}", file=sys.stderr)
        return {}
    return payload if isinstance(payload, dict) else {}


_RULES_OVERRIDES = _load_rules_overrides()


def _override_string_list(name: str, defaults: list[str]) -> list[str]:
    values = _RULES_OVERRIDES.get(name)
    if not isinstance(values, list):
        return defaults
    return [str(value).strip() for value in values if str(value).strip()]


SOLO_LOW_SIGNAL_PATTERNS = _override_string_list("solo_low_signal_patterns", SOLO_LOW_SIGNAL_PATTERNS)
SOLO_LOW_SIGNAL_DOMAINS = _override_string_list("solo_low_signal_domains", SOLO_LOW_SIGNAL_DOMAINS)
SOLO_GENERIC_PAGE_PATTERNS = _override_string_list("solo_generic_page_patterns", SOLO_GENERIC_PAGE_PATTERNS)
SOLO_GENERIC_PAGE_URL_PARTS = _override_string_list("solo_generic_page_url_parts", SOLO_GENERIC_PAGE_URL_PARTS)

SOLO_INLINE_URL_PATTERNS = (
    re.compile(r"https?://\S+", flags=re.I),
    re.compile(r"\bwww\.\S+\b", flags=re.I),
    re.compile(
        r"\b(?:t\.co|x\.com|twitter\.com|github\.com|huggingface\.co|arxiv\.org|youtu\.be|youtube\.com|bit\.ly|tinyurl\.com|ow\.ly|buff\.ly)/\S*\b",
        flags=re.I,
    ),
)


def _solo_quality_guardrails() -> str:
    return (
        "质量红线：\n"
        "1. 不要写空洞评价、趋势拔高或未来展望，除非卡片里已经给出明确事实支撑。\n"
        "2. 每一条新闻必须先写清楚主体和动作，再补充数字、背景或意义；如果做不到，就缩成一句事实播报。\n"
        "3. 不要为了过渡而补写泛泛背景句，不要写没有主体的行业总结句。\n"
        "4. 如果某条新闻只有有限事实，只保留最硬的信息，不要补推测，不要补市场评价。\n"
        "5. 严禁把原始 URL、平台短链、链接口令直接写进口播稿。\n"
        "6. 开场日期只报一次，不要出现“今天/今日/于今日/时间为”这类重复日期表述。\n"
        "7. 片尾只能保留一条固定结束语，不能再追加第二套感谢词、订阅引导或祝福句。\n"
        "8. 严禁出现这些高风险句式或同义改写："
        + "；".join(f"「{text}」" for text in SOLO_QUALITY_RISK_PATTERNS)
    )

SOLO_TITLE_VERB_PATTERNS = (
    "launch",
    "release",
    "announce",
    "introduc",
    "expand",
    "deploy",
    "raise",
    "list",
    "trading",
    "price",
    "production",
    "integrat",
    "roll out",
)
SOLO_ACTION_KEYWORDS = [
    "launch",
    "release",
    "debut",
    "deploy",
    "expand",
    "introduc",
    "announce",
    "roll out",
    "raise",
    "funding",
    "nasdaq",
    "github",
    "product hunt",
    "paper",
    "arxiv",
    "agent",
    "ai",
    "model",
    "chip",
    "memory",
    "gpu",
    "quantum",
    "robot",
]
SOLO_SCRIPT_TARGET_MIN = 1200
SOLO_SCRIPT_ESTIMATED_CHARS_PER_MINUTE = 260
SOLO_SCRIPT_MAX_DURATION_MINUTES = 20
SOLO_SCRIPT_TARGET_MAX = SOLO_SCRIPT_ESTIMATED_CHARS_PER_MINUTE * SOLO_SCRIPT_MAX_DURATION_MINUTES
SOLO_COVER_RETRY_DELAY_SECONDS = 3600
SOLO_STRUCTURED_EXTRACT_BATCH_LIMIT = 12


@dataclass
class SoloStructuredFact:
    entity: str = ""
    action: str = ""
    natural_title: str = ""
    fact_metrics: str = ""
    why_it_matters: str = ""
    topic: str = ""
    is_valuable: bool = True
    discard_reason: str = ""
    confidence: float = 0.0


_SOLO_FACT_CACHE: dict[str, dict[str, str | float | bool]] = {}


def _clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = text.replace("**", "").replace("__", "")
    for pattern in SOLO_INLINE_URL_PATTERNS:
        text = pattern.sub("", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([，。！？、,.!?;:：；])", r"\1", text)
    return text.strip()


def _contains_inline_url(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in SOLO_INLINE_URL_PATTERNS)


def _strip_source_suffix(title: str) -> str:
    title = _clean_text(title)
    if " - " in title:
        left, right = title.rsplit(" - ", 1)
        if 1 <= len(right.split()) <= 6:
            return left.strip()
    return title.strip()


def _format_money(match: re.Match[str]) -> str:
    amount = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit in {"b", "billion"}:
        return f"{amount * 10:g}亿美元"
    if unit in {"m", "million"}:
        return f"{amount * 100:g}万美元"
    if unit in {"t", "trillion"}:
        return f"{amount:g}万亿美元"
    return match.group(0)


def _localize_numbers(text: str) -> str:
    return re.sub(r"\$?\s*(\d+(?:\.\d+)?)\s*(billion|million|trillion|[BMT])\b", _format_money, text, flags=re.I)


def _period_label() -> str:
    """使用当天实际日期生成月份标签，避免写错月份。"""
    return now_local().strftime("%Y年%-m月")


def _split_script_paragraphs(script_text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n{2,}", str(script_text or "").strip()) if part.strip()]


def _join_script_paragraphs(paragraphs: list[str]) -> str:
    return "\n\n".join(part.strip() for part in paragraphs if str(part).strip()).strip()


def _is_fixed_opening_paragraph(paragraph: str) -> bool:
    compact = re.sub(r"\s+", "", paragraph or "")
    if not compact:
        return False
    has_date = bool(re.search(r"\d{4}年\d{1,2}月\d{1,2}日", compact))
    has_hint = any(token in compact for token in SOLO_FIXED_OPENING_HINTS)
    return has_date and has_hint


def _is_fixed_closing_paragraph(paragraph: str) -> bool:
    compact = re.sub(r"\s+", "", paragraph or "")
    if not compact:
        return False
    return any(token in compact for token in SOLO_FIXED_CLOSING_HINTS)


def _extract_script_quality_segments(script_text: str) -> dict[str, str]:
    paragraphs = _split_script_paragraphs(script_text)
    intro = ""
    outro = ""

    if paragraphs and _is_fixed_opening_paragraph(paragraphs[0]):
        intro = paragraphs.pop(0)
    if paragraphs and _is_fixed_closing_paragraph(paragraphs[-1]):
        outro = paragraphs.pop()

    return {
        "intro": intro,
        "body": _join_script_paragraphs(paragraphs),
        "outro": outro,
    }


def _rebuild_script_from_quality_segments(intro: str, body: str, outro: str) -> str:
    parts = [part for part in (intro.strip(), body.strip(), outro.strip()) if part]
    return _join_script_paragraphs(parts)


def _title_visible_length(text: str) -> int:
    length = 0
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            length += 2
        elif ch.strip():
            length += 1
    return length


def _title_has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def _trim_title_body_for_publish(body: str, limit: int = SOLO_TITLE_BODY_VISIBLE_LIMIT) -> str:
    body = re.sub(r"\s+", "", body or "")
    body = re.sub(r"[，、,;；]+(?:[A-Za-z].*)?$", "", body)
    body = re.sub(r"(?:[-–—]\s*)?[A-Za-z][A-Za-z0-9 .&'/:+-]*$", "", body).strip(" ，、,:：;；-")
    if _title_visible_length(body) <= limit:
        return body

    for separator in ["，", "、", ",", "；", ";", " ", "·", "/", "|"]:
        if separator not in body:
            continue
        candidate = body.split(separator, 1)[0].strip(" ，、,:：;；-/|")
        if candidate and _title_visible_length(candidate) <= limit:
            return candidate

    result: list[str] = []
    score = 0
    for ch in body:
        if not ch.strip():
            continue
        ch_score = 2 if "\u4e00" <= ch <= "\u9fff" else 1
        if score + ch_score > limit:
            break
        result.append(ch)
        score += ch_score
    return "".join(result).strip(" ，、,:：;；-/|")


def _normalize_publish_safe_title(title_text: str) -> str:
    title = (title_text or "").strip("。 ").strip()
    if not title:
        return title
    if _title_has_cjk(title) and _title_visible_length(title) <= SOLO_TITLE_VISIBLE_LIMIT:
        return title

    normalized = title.replace("AI日报", "AI").replace("AI 日报", "AI")
    normalized = normalized.replace("： ", "：").replace(": ", "：")
    normalized = re.sub(r"\s*[|｜-]\s*[^:：]+$", "", normalized)

    prefix = ""
    body = normalized
    if "：" in normalized:
        prefix, body = normalized.split("：", 1)
    elif ":" in normalized:
        prefix, body = normalized.split(":", 1)

    if re.match(r"^\d{4}年\d{1,2}月\d{1,2}日AI$", prefix):
        prefix = re.sub(r"^(\d{4}年\d{1,2}月)\d{1,2}日AI$", r"\1AI", prefix)
    elif not prefix and re.match(r"^\d{4}年\d{1,2}月\d{1,2}日AI", body):
        matched_prefix = re.match(r"^(\d{4}年\d{1,2}月)\d{1,2}日AI[:：]?(.*)$", body)
        if matched_prefix:
            prefix = f"{matched_prefix.group(1)}AI"
            body = matched_prefix.group(2)

    body = _trim_title_body_for_publish(body)
    candidate = f"{prefix}：{body}" if prefix and body else (body or prefix or normalized)
    return candidate.strip(" ，、,:：;；-/|")


def _date_str_cn() -> str:
    """当天日期，格式: 2026年04月01日。"""
    return now_local().strftime("%Y年%m月%d日")


def _normalize_for_dedupe(title: str, url: str) -> str:
    normalized_title = normalize_title_key(title)
    url_key = re.sub(r"#.*$", "", (url or "").strip().lower())
    return f"{normalized_title}|{url_key}"


def _entity_looks_generic(entity: str) -> bool:
    lowered = (entity or "").strip().lower()
    if not lowered:
        return True
    generic_prefixes = (
        "everyone",
        "former",
        "what we can learn from",
        "why ",
        "how ",
        "ai systems",
        "the ai",
        "states",
        "gut health",
        "industry report",
        "news",
    )
    if any(lowered.startswith(prefix) for prefix in generic_prefixes):
        return True
    if len(lowered.split()) > 5:
        return True
    if any(token in lowered for token in ("hint", "highlights", "redefining", "unknowns")):
        return True
    return False


def _display_entity(entity: str) -> str:
    alias_map = {
        "Alibaba": "阿里巴巴",
        "Agentic AI Foundation": "Agentic AI Foundation",
        "Microsoft": "微软",
    }
    return alias_map.get((entity or "").strip(), (entity or "").strip())


def _clean_optional_text(value: object, *, limit: int = 120) -> str:
    text = _clean_text(str(value or ""))
    if text.lower() in {"null", "none", "n/a", "unknown"}:
        return ""
    return _trim_text(text, limit=limit)


def _normalize_structured_topic(value: object, fallback: str) -> str:
    topic = _clean_optional_text(value, limit=24)
    if topic:
        return topic
    return fallback


def _structured_cache_key(item: dict[str, str]) -> str:
    return _normalize_for_dedupe(item.get("title", ""), item.get("url", ""))


def _looks_like_banned_structured_fact(text: str) -> bool:
    lowered = _clean_text(text).lower()
    if not lowered:
        return False
    banned_markers = (
        "artificial intelligence automation agency",
        "automation agency",
        "daily ai agent news",
        "虽然具体细节不多",
        "if teams don",
    )
    return any(marker in lowered for marker in banned_markers)


def _trim_text(text: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip(" ，、,:：;；-") + "..."


def _is_commentary_or_transition_paragraph(paragraph: str) -> bool:
    """识别 codex 风格的判断段/边界段/承接段。这些段不是独立新闻条目。"""
    compact = re.sub(r"\s+", "", paragraph or "")
    if not compact:
        return False
    return any(compact.startswith(lead) for lead in SOLO_SCRIPT_COMMENTARY_LEADS)


def _count_script_news_paragraphs(script_text: str) -> int:
    """统计脚本里真正的新闻段落数（剔除开场/结尾/过渡/判断段）。"""
    if not script_text:
        return 0
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", script_text) if p.strip()]
    count = 0
    for para in paragraphs:
        if _is_fixed_opening_paragraph(para) or _is_fixed_closing_paragraph(para):
            continue
        if any(marker in para for marker in (
            "嗨，早上好", "早上好，今天是", "以上就是今天", "欢迎订阅",
            "评论区", "今天的播报到这里结束", "我们明天", "好，我们一条一条来看",
            "真正跑赢这一轮", "如果今天这些新闻必须浓缩成一句话",
        )):
            continue
        if _is_commentary_or_transition_paragraph(para):
            continue
        # 过短的过渡句不算
        if len(re.sub(r"\s+", "", para)) < 20:
            continue
        count += 1
    return count


__all__ = [name for name in globals() if not name.startswith("__")]
