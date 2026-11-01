#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import copy
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT_DIR = Path(os.environ.get("PODCAST_ROOT_DIR") or Path(__file__).resolve().parent)
FOLLOW_BUILDER_DIR = Path(os.environ.get("FOLLOW_BUILDER_DIR") or (ROOT_DIR / "external" / "follow-builders"))
FOLLOW_BUILDER_ARTIFACTS_DIR = FOLLOW_BUILDER_DIR / "artifacts"
FOLLOW_BUILDER_PREPARE_DIGEST_SCRIPT = FOLLOW_BUILDER_DIR / "scripts" / "prepare-digest.js"
FOLLOW_BUILDER_FEED_X_PATH = FOLLOW_BUILDER_DIR / "feed-x.json"
FOLLOW_BUILDER_FEED_BLOGS_PATH = FOLLOW_BUILDER_DIR / "feed-blogs.json"
FOLLOW_BUILDER_FEED_PODCASTS_PATH = FOLLOW_BUILDER_DIR / "feed-podcasts.json"
FOLLOW_BUILDER_FEED_BUNDLE_PATH = FOLLOW_BUILDER_ARTIFACTS_DIR / "latest-feed-bundle.json"
FOLLOW_BUILDER_DIGEST_ARTIFACT_PATH = FOLLOW_BUILDER_ARTIFACTS_DIR / "latest-digest.json"
FOLLOW_BUILDER_REFRESH_TIMEOUT_SECONDS = 90
FOLLOW_BUILDER_TRANSCRIPT_CHAR_LIMIT = 1800
RULES_CONFIG_PATH = ROOT_DIR / "news_pipeline_rules.json"
RECORD_CSV = ROOT_DIR / "content_records.csv"
DAILY_SEARCH_CACHE_DIR = ROOT_DIR / ".daily_search_cache"
LISTENHUB_API_BASE = "https://api.marswave.ai/openapi/v1"
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
MINIMAX_IMAGE_MODEL = "image-01"
GEMINI_VENV_PYTHON = ROOT_DIR / ".venv_gemini_test" / "bin" / "python"
PENDING_DOWNLOAD_FILE = ROOT_DIR / ".listenhub_pending_download.json"
DEFAULT_COVER_PROMPT_TEMPLATE_PATH = ROOT_DIR / "cover_prompt_template.txt"
RECENT_SIMILARITY_LOOKBACK_DAYS = 5
RECENT_SIMILARITY_MAX_EPISODES = 6
SEARCH_BACKLOG_LOOKBACK_DAYS = 5
SEARCH_BACKLOG_MAX_EPISODES = 10
SEARCH_BACKLOG_TARGET_PER_BUCKET = 12
SEARCH_BACKLOG_MAX_ADDITIONS_PER_BUCKET = 6
MAX_COVER_GENERATION_ATTEMPTS = 2
# MiniMax / Qwen-Wanx 是当前唯一可直连的文生图兜底（Gemini 图片 API 有地域限制，
# 无代理环境直接 400 FAILED_PRECONDITION），且都易生成海报伪文字被反文字守卫拒绝，
# 因此单独提高它们的重试上限，多刷几次拿到一张干净图的概率更高。
MAX_MINIMAX_COVER_ATTEMPTS = 4
MAX_QWEN_COVER_ATTEMPTS = 3
CODEX_COVER_PROVIDER = "codex_image"
CODEX_COVER_CANDIDATE_NAME = "cover_codex_candidate_1.png"
PODCAST_HOOK_TEMPLATES = [
    "这两天 AI 圈最值得看的，不是某家公司又放了个大招，而是几条分散的新闻正在拼成同一张产业地图。",
    "如果把今天的 AI 新闻并排放在一起看，你会发现行业重点已经悄悄换了方向。",
    "今天这期不想报流水账，因为真正值得看的，不是一条新闻本身，而是这些变化正在一起改写 AI 的竞争逻辑。",
    "最近最有意思的，不是哪家模型又涨了几分，而是 AI 产业链上几个关键位置正在同时发生位移。",
    "今天我们想聊的不是单条热点，而是这些看似分散的进展，为什么会一起指向同一个行业拐点。",
]
BANNED_PODCAST_OPENERS = (
    "最近 AI 新闻又开始密集刷屏",
    "又开始密集刷屏",
)


def assert_solo_flow_type_file(episode_dir: Path) -> None:
    flow_path = episode_dir / "flow_type.txt"
    if not flow_path.exists():
        raise RuntimeError(f"{episode_dir.name} 缺少 flow_type.txt，拒绝写入 solo Codex 封面")
    flow_type = flow_path.read_text(encoding="utf-8").strip()
    if flow_type != "solo":
        raise RuntimeError(f"{episode_dir.name} flow_type.txt={flow_type or '<empty>'}，拒绝写入 solo Codex 封面")
TITLE_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "will",
    "this",
    "that",
    "later",
    "year",
    "today",
    "latest",
    "first",
    "launch",
    "launches",
    "new",
    "its",
    "their",
    "your",
    "after",
    "about",
    "amid",
    "over",
    "than",
    "ever",
}
GENERIC_TOPIC_TOKENS = {
    "latest",
    "today",
    "march",
    "april",
    "2026",
    "news",
    "update",
    "launch",
    "release",
    "product",
    "products",
    "agent",
    "agents",
    "model",
    "models",
    "chip",
    "chips",
    "tool",
    "tools",
    "app",
    "apps",
    "desktop",
    "browser",
    "coding",
    "productivity",
    "ai",
}
GENERIC_TITLE_KEYS = {
    "artificial intelligence",
    "ai news",
    "ai update",
    "latest ai news",
}
LOW_SIGNAL_TITLE_KEYWORDS = {
    "erotic chatbot",
    "erotic",
    "adult chatbot",
    "sexual chatbot",
    "conference announcements summary",
    "pre-event",
    "trillion dollar race",
    "smartest minds",
}
LOW_SIGNAL_TITLE_PREFIXES = ("how ", "why ", "what ")
OBVIOUSLY_IRRELEVANT_TITLE_KEYWORDS = {
    "nfl",
    "nba",
    "mlb",
    "nhl",
    "ncaa",
    "soccer",
    "golf",
    "tennis",
    "lacrosse",
    "fantasy baseball",
    "fantasy basketball",
    "free agents",
    "season",
    "scores",
    "weather",
    "forecast",
    "lottery",
    "immigration",
    "white house",
    "iran war",
    "gravitational wave",
    "playstation",
    "xbox",
    "nintendo",
    "steam",
    "ps5",
    "ps4",
    "switch 2",
    "game pass",
    "gaming",
    "gamer",
    "gameplay",
    "pokemon",
    "tomb raider",
}
OBVIOUSLY_IRRELEVANT_DOMAINS = {
    "espn.com",
    "cbssports.com",
    "sportingnews.com",
    "newsweek.com",
    "nhl.com",
    "usatoday.com",
}
SEARCH_HARD_REJECT_DOMAINS = {
    "espn.com",
    "cbssports.com",
    "sportingnews.com",
    "nhl.com",
    "nba.com",
    "mlb.com",
    "foxsports.com",
    "bleedinggreennation.com",
    "sports.yahoo.com",
    "atozsports.com",
    "deadspin.com",
    "pushsquare.com",
    "nintendolife.com",
    "ign.com",
    "gamespot.com",
    "kotaku.com",
    "polygon.com",
    "pcgamer.com",
    "nypost.com",
}
SEARCH_HARD_REJECT_URL_PARTS = (
    "/sports/",
    "/fantasy/",
    "/game/",
    "/watch/video/",
    "/mock-draft",
    "/draft/",
    "/next-weather/",
    "/weather/",
)
SEARCH_HARD_REJECT_TEXT_KEYWORDS = {
    "fantasy basketball",
    "fantasy baseball",
    "fantasy football",
    "fantasy hockey",
    "sports betting",
    "training camp",
    "waiver wire",
    "playoff spot",
    "opening weekend",
    "goalie tiers",
    "injury report",
    "mock draft",
    "weather forecast",
    "next weather",
    "wordle",
    "russia-ukraine conflict",
    "best airpods",
    "best headphones",
    "noise cancelling earbuds",
    "wireless earbuds",
    "bluetooth speaker",
    "best soundbar",
    "audio deals",
    "most popular news",
    "scores schedule",
    "league pass",
    "mlb.tv",
    "statcast",
    "playstation",
    "xbox",
    "nintendo switch",
    "game pass",
    "ps plus",
    "ps5",
    "ps4",
    "steam deck",
    "steam",
    "gaming",
    "gamer",
    "gameplay",
    "esports",
    "console",
    "pokemon",
    "tomb raider",
    "resident evil",
    "rockstar games",
    "game development",
    "game developer",
}
DOC_PAGE_URL_PARTS = (
    "/help/",
    "/docs/",
    "/documentation/",
    "/knowledge-base/",
    "/kb/",
    "/product-versions",
    "/release-notes",
    "/changelog",
    "/api/",
)
DOC_PAGE_TITLE_KEYWORDS = {
    "documentation",
    "product versions",
    "knowledge base",
    "help center",
    "release notes",
    "api reference",
    "api documentation",
    "developer documentation",
}
LOW_VALUE_MARKETING_URL_PARTS = (
    "/features/copilot",
    "/copilot/",
    "/trending",
    "/explore",
)
LOW_VALUE_SUPPORT_DOMAINS = {
    "support.microsoft.com",
    "community.openai.com",
}
LOW_VALUE_FAQ_PREFIXES = (
    "how do i ",
    "what is included in ",
    "which plan includes ",
    "what are the differences between ",
    "can i ",
)
OFFICIAL_ANNOUNCEMENT_HOSTS = {
    "openai.com",
    "anthropic.com",
    "blog.google",
    "about.fb.com",
    "x.ai",
    "qwenlm.github.io",
    "alibabacloud.com",
}
UNDATED_OFFICIAL_TITLE_PREFIXES = (
    "introducing ",
    "announcing ",
)
FAST_MOVING_NEWS_KEYWORDS = {
    "openai",
    "chatgpt",
    "codex",
    "anthropic",
    "claude",
    "gemini",
    "copilot",
    "llama",
    "grok",
    "qwen",
    "deepseek",
    "browser",
    "desktop",
    "agent",
    "agents",
    "github",
    "launch",
    "release",
    "update",
    "this week",
}
FRESHNESS_QUERY_MARKERS = (
    "today",
    "latest",
    "recent",
    "this week",
    "this month",
    "this year",
    "yesterday",
    "launch",
    "launches",
    "launched",
    "release",
    "releases",
    "released",
    "announce",
    "announcing",
    "announced",
    "update",
    "updated",
)
FRESHNESS_QUERY_MONTH_MARKERS = (
    "jan",
    "january",
    "feb",
    "february",
    "mar",
    "march",
    "apr",
    "april",
    "may",
    "jun",
    "june",
    "jul",
    "july",
    "aug",
    "august",
    "sep",
    "sept",
    "september",
    "oct",
    "october",
    "nov",
    "november",
    "dec",
    "December",
)
SEARCH_MAX_ITEM_AGE_DAYS = 21
SEARCH_MAX_FAST_MOVING_ITEM_AGE_DAYS = 10
SEARCH_GENERIC_PAGE_MARKERS = (
    "tech shows",
    "one more thing",
    "all things mobile",
    "what the future",
    "expert vs. ai",
    "our latest fantasy",
    "market movers",
    "what to read next",
)
CYRILLIC_CHAR_RE = re.compile(r"[\u0400-\u04ff]")
AI_SIGNAL_PATTERNS = [
    r"\bai\b",
    r"artificial intelligence",
    r"openai",
    r"chatgpt",
    r"\bgpt[- ]?\d",
    r"anthropic",
    r"claude",
    r"gemini",
    r"copilot",
    r"agentic",
    r"ai agent",
    r"workflow automation",
    r"\bautomation\b",
    r"\bmodel\b",
    r"\bmodels\b",
    r"\bllm\b",
    r"drug discovery",
    r"\brobot\b",
    r"robotics",
    r"\bchip\b",
    r"\bchips\b",
    r"\bgpu\b",
    r"semiconductor",
    r"inference",
    r"model training",
    r"training cluster",
    r"\bserver\b",
    r"data center",
    r"\bworkflow\b",
    r"developer tool",
    r"\bcoding\b",
]
HIGH_SIGNAL_TITLE_KEYWORDS = {
    "rolls out",
    "launch",
    "launches",
    "release",
    "releases",
    "announces",
    "opens",
    "picks",
    "pause",
    "pauses",
    "pulls the plug",
    "scraps",
    "builds",
    "writes down",
    "accelerates",
}
HIGH_QUALITY_DOMAINS = {
    "reuters.com": 5,
    "ft.com": 5,
    "theverge.com": 5,
    "wsj.com": 5,
    "techcrunch.com": 4,
    "cnbc.com": 4,
    "arxiv.org": 5,
    "github.com": 5,
    "openai.com": 5,
    "anthropic.com": 5,
    "google.com": 4,
    "deepmind.google": 5,
    "research.google": 5,
    "blog.google": 4,
    "microsoft.com": 4,
    "nvidia.com": 4,
    "amd.com": 4,
    "intel.com": 4,
    "skhynix.com": 4,
    "samsung.com": 4,
    "micron.com": 4,
    "asml.com": 4,
    "about.fb.com": 4,
    "meta.com": 4,
    "x.com": 3,
    "huggingface.co": 4,
    "devpost.com": 3,
    "lablab.ai": 3,
    "xiaohongshu.com": 2,
}
LOW_QUALITY_DOMAINS = {
    "tipranks.com": -3,
    "stocktitan.net": -2,
    "stocktitan.com": -2,
    "investing.com": -2,
    "mlq.ai": -1,
    "qoo10.co.id": -3,
    "manilastandard.net": -2,
    "manilatimes.net": -2,
    "rhea-ai.com": -2,
    "90-3kazu.org": -1,
    "geeky-gadgets.com": -2,
    "letsdatascience.com": -2,
    "fool.com": -2,
    "motleyfool.com": -2,
    "thestreet.com": -2,
    "irishtechnews.ie": -2,
}
CONTENT_HARD_STOP_MARKERS = (
    "### information you can trust",
    "our standards:",
    "purchase licensing rights",
    "go to homepage",
    "skip to main content",
    "main menu",
    "more in entertainment",
    "terms | privacy policy",
    "cancel reply",
    "submit a comment",
    "manage cookies",
    "cookie preferences",
    "digital accessibility",
    "download the app",
    "our journalismworksfor you",
    "what to read next",
    "market movers",
    "our latest fantasy",
    "tech shows",
)
CONTENT_SKIP_LINE_PATTERNS = (
    r"^advertisement$",
    r"^follow us$",
    r"^main menu$",
    r"^more news$",
    r"^more in entertainment$",
    r"^our standards:?$",
    r"^purchase licensing rights$",
    r"^related topics:?$",
    r"^suggested topics:?$",
    r"^download the app(?: \(.+\))?$",
    r"^newsletters$",
    r"^subscribe(?: now)?$",
    r"^go to homepage$",
    r"^submit a tip$",
    r"^site feedback$",
    r"^terms(?: & conditions)?$",
    r"^privacy(?: statement| policy)?$",
    r"^manage cookies$",
    r"^cookie preferences$",
    r"^all rights reserved\.?$",
    r"^powered by\b.*$",
    r"^skip to main content$",
    r"^cancel reply$",
    r"^submit a comment$",
    r"^follow us$",
    r"^up next$",
    r"^don't miss$",
    r"^continue reading$",
    r"^play now$",
    r"^rankings$",
    r"^avg\. draft positions$",
    r"^depth charts$",
    r"^player news$",
    r"^injury report$",
    r"^one more thing$",
    r"^all things mobile$",
    r"^what the future$",
    r"^expert vs\. ai$",
)
LOW_VALUE_CONTENT_MARKERS = (
    "cookies, opens new tab",
    "opens new tab",
    "privacy statement",
    "terms & conditions",
    "digital accessibility",
    "newsletter",
    "follow us",
    "image ",
    "copyright",
    "all rights reserved",
    "free trial",
    "sign-up for",
    "sign up for",
    "want to continue reading",
    "unlock these benefits",
    "subscribe now",
    "subscribe to",
    "gain a competitive edge",
    "exclusive data visualization",
    "7-day free trial",
    "7 day free trial",
    "premium content",
    "paywall",
    "market movers",
    "most popular articles",
    "what to read next",
)
DYNAMIC_ENTITY_ALLOWLIST = (
    "OpenAI",
    "Anthropic",
    "Google Gemini",
    "Google",
    "Amazon",
    "AWS",
    "Apple",
    "Microsoft Copilot",
    "Microsoft",
    "Nvidia",
    "AMD",
    "Broadcom",
    "Oracle",
    "TSMC",
    "SK Hynix",
    "Samsung",
    "Micron",
    "Meta",
    "Llama",
    "xAI",
    "Grok",
    "Mistral",
    "DeepSeek",
    "Qwen",
    "Baidu",
    "Alibaba",
    "GitHub Copilot",
    "GitHub",
    "TurboQuant",
    "Copilot Cowork",
    "Ollama",
    "MLX",
    "Perplexity",
    "Cursor",
    "SpaceX",
    "OpenClaw",
    "HBM4",
    "Rubin",
    "Sora",
)
DYNAMIC_TERM_STOPWORDS = {
    "today",
    "latest",
    "news",
    "update",
    "updates",
    "launch",
    "release",
    "reuters",
    "techcrunch",
    "forbes",
    "open",
    "project",
    "exclusive",
    "information",
    "trust",
}
SOURCE_QUERY_MATCH_STOPWORDS = DYNAMIC_TERM_STOPWORDS | {
    "launches",
    "launching",
    "release",
    "releases",
    "product",
    "products",
    "model",
    "models",
    "app",
    "apps",
    "tool",
    "tools",
    "project",
    "projects",
    "developer",
    "developers",
    "ide",
    "code",
    "week",
    "weekly",
    "hottest",
    "ranking",
    "rankings",
    "reviews",
    "enterprise",
    "assistant",
    "policy",
    "regulation",
    "regulatory",
    "funding",
    "startup",
    "startups",
    "venture",
    "capex",
    "earnings",
    "workforce",
    "hiring",
    "layoffs",
    "talent",
    "digital",
    "markets",
    "services",
    "executive",
    "order",
    "export",
    "control",
    "data",
    "center",
    "open",
    "source",
    "chinese",
    "launch",
    "update",
}
TAVILY_ENABLE_DYNAMIC_AI_QUERIES = False
TAVILY_ENABLE_DYNAMIC_PRODUCT_QUERIES = False
BRAVE_ENABLE_DYNAMIC_AI_QUERIES = False
BRAVE_ENABLE_DYNAMIC_PRODUCT_QUERIES = False
CONTENT_STOPWORDS = TITLE_STOPWORDS | GENERIC_TOPIC_TOKENS | {
    "advertisement",
    "continue",
    "reuters",
    "forbes",
    "gizmodo",
    "verge",
    "cnbc",
    "news",
    "article",
    "story",
    "stories",
    "company",
    "companies",
    "market",
    "annual",
    "revenue",
    "stock",
    "shares",
    "business",
    "analyst",
    "analysts",
    "report",
    "reported",
    "reporting",
    "published",
    "march",
    "year",
    "years",
    "today",
    "发生了什么",
    "为什么重要",
    "意味着什么",
}
AI_NEWS_TAVILY_QUERY_TEMPLATES = [
    "OpenAI Anthropic Gemini Codex AI release update this week {month_year}",
    "Meta Llama xAI Grok Qwen DeepSeek AI model release open source {month_year}",
    "AI silicon photonics CPO optical interconnect data center news {month_year}",
    "HBM sk hynix samsung micron AI memory server news {month_year}",
    "AI startup funding enterprise deployment this week {month_year}",
    "AI regulation policy White House EU export control executive order {month_year}",
    "AI acquisition merger M&A earnings Microsoft Google Amazon Nvidia {month_year}",
    "AI layoffs workforce hiring talent war OpenAI Anthropic Meta {month_year}",
    "Amazon Microsoft Google Meta AI capex data center cloud spending {month_year}",
    "EU AI Act DMA DSA digital regulation antitrust Big Tech {month_year}",
    "AI jobs workforce hiring layoffs automation concern {month_year}",
    "European venture report startup funding PitchBook Atomico {month_year}",
]
PRODUCT_TAVILY_QUERY_TEMPLATES: list[str] = []
AI_NEWS_BRAVE_QUERY_TEMPLATES = [
    "site:openai.com OR site:anthropic.com OR site:blog.google ChatGPT Claude Gemini Codex model update {month_year}",
    "site:about.fb.com OR site:x.ai OR site:qwenlm.github.io OR site:alibabacloud.com Llama Grok Qwen DeepSeek release {month_year}",
    "site:reuters.com OR site:lightreading.com OR site:fierce-network.com AI silicon photonics CPO optical interconnect {month_year}",
    "site:reuters.com OR site:tomshardware.com OR site:digitimes.com HBM sk hynix micron samsung AI memory server {month_year}",
    "site:reuters.com OR site:techcrunch.com AI startup funding enterprise deployment {month_year}",
    "site:openai.com OR site:anthropic.com Claude Code Codex desktop browser update {month_year}",
    "site:reuters.com OR site:cnbc.com OR site:ft.com OR site:aboutamazon.com Amazon Microsoft Google Meta AI capex data center spending {month_year}",
    "site:reuters.com OR site:commission.europa.eu OR site:ec.europa.eu OR site:politico.eu EU AI Act DMA DSA digital regulation big tech {month_year}",
    "site:pitchbook.com OR site:atomico.com OR site:sifted.eu OR site:tech.eu European venture report startup funding {month_year}",
    "site:reuters.com OR site:ft.com OR site:cnbc.com AI jobs workforce hiring layoffs automation {month_year}",
]
PRODUCT_BRAVE_QUERY_TEMPLATES = [
    "site:producthunt.com/products AI coding agent productivity launch {month_year}",
    "site:github.com AI agent framework open source release {month_year}",
    "site:jetbrains.com AI assistant VS Code release {month_year}",
    "site:microsoft.com/en-us/microsoft-copilot/blog Copilot Studio multi-agent update {month_year}",
    "site:openai.com Codex ChatGPT desktop app update {month_year}",
    "site:anthropic.com/news Claude Code checkpoints sdk update {month_year}",
]
AI_NEWS_BACKFILL_BRAVE_QUERY_TEMPLATES = {
    "tech": [
        "site:reuters.com OR site:lightreading.com OR site:fierce-network.com AI data center networking silicon photonics CPO {month_year}",
        "site:reuters.com OR site:tomshardware.com OR site:digitimes.com HBM memory sk hynix micron samsung AI server {month_year}",
        "site:reuters.com OR site:cnbc.com OR site:ft.com Amazon Microsoft Google Meta AI capex data center {month_year}",
    ],
    "model": [
        "site:openai.com OR site:anthropic.com OR site:blog.google AI model release update {month_year}",
        "site:about.fb.com OR site:x.ai OR site:qwenlm.github.io OR site:alibabacloud.com open source model release {month_year}",
    ],
    "ai_product": [
        "site:openai.com OR site:anthropic.com Codex Claude Code app update {month_year}",
        "site:microsoft.com/en-us/microsoft-copilot/blog OR site:jetbrains.com AI coding tool desktop browser update {month_year}",
    ],
    "business": [
        "site:reuters.com AI startup funding enterprise adoption capex data center {month_year}",
        "site:techcrunch.com AI startup funding enterprise adoption this week {month_year}",
        "site:reuters.com OR site:ft.com OR site:cnbc.com AI jobs workforce hiring layoffs {month_year}",
        "site:pitchbook.com OR site:atomico.com OR site:sifted.eu European venture report startup funding {month_year}",
    ],
    "diversity": [
        "site:reuters.com OR site:techcrunch.com OR site:theverge.com AI industry update {month_year}",
        "site:reuters.com OR site:commission.europa.eu OR site:ec.europa.eu OR site:politico.eu EU AI Act DMA DSA tech regulation {month_year}",
    ],
}
AI_NEWS_PRIMARY_CATEGORY_TARGETS = {
    "tech": 3,
    "model": 2,
    "ai_product": 2,
    "business": 2,
}
AI_NEWS_BACKFILL_PRIORITY = ("tech", "model", "ai_product", "business")
MAX_AI_NEWS_BACKFILL_QUERIES = 4
PRODUCT_COMMUNITY_EXPERT_WATCH_ACCOUNTS = [
    {"handle": "karpathy", "keywords": "AI coding model agent latest post"},
    {"handle": "zarazhangrui", "keywords": "AI product agent workflow latest post"},
]
PRODUCT_COMMUNITY_PROJECT_WATCH_SOURCES = [
    {
        "site_query": "site:huggingface.co/spaces",
        "keywords": "AI space demo launch this week",
    },
    {
        "site_query": "site:github.com/releases",
        "keywords": "AI open source project release this week",
    },
]
PRODUCT_COMMUNITY_SUPPLEMENT_BRAVE_QUERY_TEMPLATES = {
    "github_rising": [
        "site:github.com/releases AI repo trending this week {month_year}",
        "site:github.com/releases AI coding agent open source release {month_year}",
    ],
    "hackathon": [
        "site:devpost.com OR site:lablab.ai AI hackathon winner project {month_year}",
        "site:devpost.com generative AI hackathon winning project this week {month_year}",
    ],
    "xiaohongshu": [
        "site:xiaohongshu.com AI 智能体 热门 讨论 最近",
        "site:xiaohongshu.com AI 编程 工作流 热门 讨论 最近",
    ],
}
PRODUCT_COMMUNITY_SUPPLEMENT_PRIORITY = (
    "expert_watch",
    "hackathon",
    "project_watch",
    "github_rising",
    "xiaohongshu",
)
MAX_PRODUCT_COMMUNITY_SUPPLEMENT_QUERIES = 3
PRODUCT_COMMUNITY_EXTERNAL_PROVIDER_PRIORITY = (
    "x_watch_apify",
    "github_newstars_api",
    "hf_trending_api",
)
MAX_PRODUCT_COMMUNITY_EXTERNAL_PROVIDERS = 3
PRODUCT_COMMUNITY_GITHUB_RECENT_WINDOW_DAYS = 14
PRODUCT_COMMUNITY_GITHUB_RECENT_MIN_STAR_VELOCITY = 8.0
PRODUCT_COMMUNITY_GITHUB_SEARCHES = [
    {
        "label": "ai-agent",
        "query": "topic:ai-agent created:>={since_date} stars:>=80 archived:false is:public",
        "source_query": "github recent stars ai agent open source repo",
    },
    {
        "label": "llm-tooling",
        "query": "(topic:llm OR topic:ai OR topic:machine-learning) created:>={since_date} stars:>=100 archived:false is:public",
        "source_query": "github recent stars llm ai model tool repo",
    },
]
PRODUCT_COMMUNITY_HF_RECENT_WINDOW_DAYS = 14
PRODUCT_COMMUNITY_HF_MIN_LIKES = 8
PRODUCT_COMMUNITY_HF_TARGETS = (
    {"kind": "models", "label": "huggingface trending model"},
    {"kind": "spaces", "label": "huggingface trending space"},
)
PRODUCT_COMMUNITY_APIFY_X_MAX_ITEMS = 8
MIN_OVERALL_RELEVANT_TOTAL = 14
MIN_PRODUCT_COMMUNITY_RELEVANT_TOTAL = 5
SECTOR_PRIORITY = [
    "supply_chain",
    "model",
    "agent_app",
    "research",
    "open_source",
    "compute",
    "other",
]


def current_query_month_year() -> str:
    return now_local().strftime("%B %Y")


def materialize_queries(templates: list[str]) -> list[str]:
    month_year = current_query_month_year()
    return [template.format(month_year=month_year) for template in templates]


def materialize_query_groups(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    month_year = current_query_month_year()
    return {
        key: [template.format(month_year=month_year) for template in templates]
        for key, templates in groups.items()
    }


def build_product_community_supplement_query_groups() -> dict[str, list[str]]:
    month_year = current_query_month_year()
    groups = materialize_query_groups(PRODUCT_COMMUNITY_SUPPLEMENT_BRAVE_QUERY_TEMPLATES)
    groups["expert_watch"] = [
        clean_text(f"site:x.com/{item['handle']} {item['handle']} {item['keywords']} {month_year}")
        for item in PRODUCT_COMMUNITY_EXPERT_WATCH_ACCOUNTS
        if item.get("handle") and item.get("keywords")
    ]
    groups["project_watch"] = [
        clean_text(f"{item['site_query']} {item['keywords']} {month_year}")
        for item in PRODUCT_COMMUNITY_PROJECT_WATCH_SOURCES
        if item.get("site_query") and item.get("keywords")
    ]
    return groups


SECTOR_KEYWORDS = {
    "supply_chain": {
        "optical",
        "photonic",
        "photonics",
        "interconnect",
        "network",
        "networking",
        "rack",
        "data center",
        "datacenter",
        "csp",
        "server",
        "hbm",
        "lithography",
        "asml",
        "foundry",
        "fabrication",
        "fab",
        "wafer",
        "asic",
        "supply chain",
    },
    "model": {
        "openai",
        "chatgpt",
        "sora",
        "anthropic",
        "claude",
        "gemini",
        "copilot",
        "llm",
        "model",
        "models",
        "video generation",
        "video app",
    },
    "research": {
        "paper",
        "arxiv",
        "research",
        "breakthrough",
        "optimization",
        "residual",
        "reasoning",
        "benchmark",
        "world model",
        "humanoid",
        "robot",
        "robotics",
        "policy",
        "simulation",
        "moonshot",
        "kimi",
    },
    "open_source": {
        "github",
        "repo",
        "repository",
        "open source",
        "stars",
        "starred",
        "framework",
        "sdk",
        "cli",
        "developer tool",
        "developer tools",
    },
    "agent_app": {
        "agent",
        "agents",
        "browser",
        "desktop",
        "workflow",
        "codex",
        "cowork",
        "computer",
        "application",
        "applications",
        "assistant",
        "tool",
        "tools",
        "productivity",
        "app",
    },
    "compute": {
        "chip",
        "chips",
        "cpu",
        "gpu",
        "lpu",
        "inference",
        "training",
        "arm",
        "nvidia",
        "intel",
        "amd",
        "semiconductor",
        "compute",
    },
}
ANCHOR_KEYWORDS = [
    "sk hynix",
    "hynix",
    "micron",
    "samsung",
    "sora",
    "codex",
    "gemma",
    "sycamore",
    "agent-framework",
    "agent framework",
    "openai",
    "claude",
    "anthropic",
    "gemini",
    "copilot",
    "nvidia",
    "intel",
    "arm",
    "amd",
    "asml",
    "meta",
    "google",
    "microsoft",
    "alibaba",
    "spacex",
    "turboquant",
    "copilot cowork",
    "github copilot",
    "ollama",
    "kimi",
    "moonshot",
    "github",
    "robot",
    "humanoid",
    "optic",
    "photonic",
    "hbm",
    "asic",
]
BUSINESS_SIGNAL_KEYWORDS = {
    "earnings",
    "revenue",
    "sales",
    "market",
    "stock",
    "shares",
    "funding",
    "raises",
    "raised",
    "raise",
    "valuation",
    "valued",
    "acquisition",
    "acquire",
    "acquires",
    "merger",
    "profit",
    "loss",
    "invest",
    "investment",
    "investor",
    "bookings",
    "orders",
    "capex",
    "capital expenditure",
    "spending",
    "spend",
    "buildout",
    "ipo",
    "startup",
    "startups",
    "venture",
    "vc",
    "hiring",
    "workforce",
    "layoff",
    "layoffs",
}
STRONG_BUSINESS_SIGNAL_KEYWORDS = {
    "earnings",
    "revenue",
    "funding",
    "valuation",
    "acquisition",
    "merger",
    "stock",
    "shares",
    "profit",
    "loss",
    "capex",
    "capital expenditure",
    "ipo",
}
BROAD_TECH_COMPANY_KEYWORDS = {
    "amazon",
    "aws",
    "apple",
    "microsoft",
    "google",
    "alphabet",
    "meta",
    "nvidia",
    "amd",
    "intel",
    "broadcom",
    "oracle",
    "openai",
    "anthropic",
    "xai",
    "tesla",
    "tsmc",
    "asml",
    "sk hynix",
    "samsung",
    "micron",
    "pitchbook",
    "atomico",
}
BROAD_TECH_POLICY_KEYWORDS = {
    "digital markets act",
    "digital services act",
    "ai act",
    "antitrust",
    "competition",
    "regulator",
    "regulation",
    "regulatory",
    "policy",
    "export control",
    "european commission",
    "dma",
    "dsa",
}
BROAD_TECH_WORKFORCE_KEYWORDS = {
    "workforce",
    "hiring",
    "layoff",
    "layoffs",
    "jobs",
    "job cuts",
    "headcount",
    "talent",
    "labor",
    "automation",
    "productivity",
}
BROAD_TECH_VENTURE_KEYWORDS = {
    "venture report",
    "venture capital",
    "startup",
    "startups",
    "funding",
    "fundraise",
    "pitchbook",
    "atomico",
    "seed",
    "series a",
    "series b",
    "vc",
    "ipo",
}
BROAD_TECH_BUSINESS_KEYWORDS = {
    "capex",
    "capital expenditure",
    "spending spree",
    "spending",
    "buildout",
    "data center",
    "datacenter",
    "cloud",
    "earnings",
    "revenue",
    "guidance",
    "orders",
    "investment",
    "funding",
    "valuation",
    "acquisition",
    "merger",
    "ipo",
}
MACRO_CONTEXT_SOURCE_DOMAINS = {
    "reuters.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "cnbc.com",
    "theinformation.com",
    "economist.com",
    "politico.com",
}
MACRO_CONTEXT_FEED_CATEGORIES = {
    "business",
    "analysis",
    "policy",
}
MACRO_CONTEXT_STRONG_KEYWORDS = {
    "降息",
    "加息",
    "利率决议",
    "大选",
    "选举",
    "并购",
    "收购",
    "兼并",
    "财报",
    "业绩",
    "营收",
    "亏损",
    "盈利",
    "上市",
    "ipo",
    "earnings",
    "revenue",
    "guidance",
    "rate cut",
    "rate cuts",
    "rate hike",
    "rate hikes",
    "interest rate",
    "interest rates",
    "merger",
    "acquisition",
    "takeover",
    "tariff",
    "tariffs",
    "sanction",
    "sanctions",
    "bankruptcy",
    "restructuring",
}
MACRO_CONTEXT_KEYWORDS = MACRO_CONTEXT_STRONG_KEYWORDS | {
    "fed",
    "federal reserve",
    "ecb",
    "boj",
    "央行",
    "美联储",
    "关税",
    "制裁",
    "刺激计划",
    "财政刺激",
    "trade war",
    "trade deal",
    "white house",
    "congress",
    "parliament",
    "ballot",
    "vote",
    "voting",
    "m&a",
    "deal talks",
    "quarterly results",
    "quarter results",
    "quarterly revenue",
    "forecast",
    "outlook",
    "layoff",
    "layoffs",
    "裁员",
    "破产",
    "重组",
}
GAMING_CONTEXT_KEYWORDS = {
    "playstation",
    "xbox",
    "nintendo",
    "steam",
    "ps5",
    "ps4",
    "switch 2",
    "game pass",
    "gaming",
    "gamer",
    "gameplay",
    "console",
    "pokemon",
    "tomb raider",
    "resident evil",
    "fps",
}
GAMING_REVIEW_MARKERS = {
    "review",
    "hands-on",
    "hands on",
    "benchmark",
    "benchmarks",
    "motherboard",
    "mainboard",
    "graphics card",
    "gpu review",
    "cpu review",
    "gaming laptop",
    "gaming pc",
    "frame rate",
    "overclock",
}
SEARCH_RESULT_KEYS = ("tavily_results", "brave_results", "backlog_results", "rss_results")


def _load_rules_config_overrides() -> dict[str, Any]:
    if not RULES_CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(RULES_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[rules] 读取配置失败: {exc}", file=sys.stderr)
        return {}
    return payload if isinstance(payload, dict) else {}


_RULES_CONFIG_OVERRIDES = _load_rules_config_overrides()


def _config_string_set(name: str, defaults: set[str]) -> set[str]:
    values = _RULES_CONFIG_OVERRIDES.get(name)
    if not isinstance(values, list):
        return defaults
    return {str(value).strip() for value in values if str(value).strip()}


def _config_string_tuple(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    values = _RULES_CONFIG_OVERRIDES.get(name)
    if not isinstance(values, list):
        return defaults
    return tuple(str(value).strip() for value in values if str(value).strip())


def _config_domain_scores(name: str, defaults: dict[str, int]) -> dict[str, int]:
    values = _RULES_CONFIG_OVERRIDES.get(name)
    if not isinstance(values, dict):
        return defaults
    configured: dict[str, int] = {}
    for key, value in values.items():
        domain = str(key).strip()
        if not domain:
            continue
        try:
            configured[domain] = int(value)
        except (TypeError, ValueError):
            continue
    return configured or defaults


def _config_string_list_map(name: str, defaults: dict[str, list[str]]) -> dict[str, list[str]]:
    values = _RULES_CONFIG_OVERRIDES.get(name)
    if not isinstance(values, dict):
        return defaults
    configured: dict[str, list[str]] = {}
    for key, items in values.items():
        if not isinstance(items, list):
            continue
        cleaned_items = [str(item).strip() for item in items if str(item).strip()]
        map_key = str(key).strip()
        if map_key and cleaned_items:
            configured[map_key] = cleaned_items
    return configured or defaults


def _config_int(name: str, default: int) -> int:
    value = _RULES_CONFIG_OVERRIDES.get(name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_bool(name: str, default: bool) -> bool:
    value = _RULES_CONFIG_OVERRIDES.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


SEARCH_HARD_REJECT_DOMAINS = _config_string_set("search_hard_reject_domains", SEARCH_HARD_REJECT_DOMAINS)
SEARCH_HARD_REJECT_URL_PARTS = _config_string_tuple("search_hard_reject_url_parts", SEARCH_HARD_REJECT_URL_PARTS)
SEARCH_HARD_REJECT_TEXT_KEYWORDS = _config_string_set(
    "search_hard_reject_text_keywords",
    SEARCH_HARD_REJECT_TEXT_KEYWORDS,
)
OBVIOUSLY_IRRELEVANT_TITLE_KEYWORDS = _config_string_set(
    "obviously_irrelevant_title_keywords",
    OBVIOUSLY_IRRELEVANT_TITLE_KEYWORDS,
)
OBVIOUSLY_IRRELEVANT_DOMAINS = _config_string_set("obviously_irrelevant_domains", OBVIOUSLY_IRRELEVANT_DOMAINS)
DOC_PAGE_URL_PARTS = _config_string_tuple("doc_page_url_parts", DOC_PAGE_URL_PARTS)
DOC_PAGE_TITLE_KEYWORDS = _config_string_set("doc_page_title_keywords", DOC_PAGE_TITLE_KEYWORDS)
LOW_VALUE_MARKETING_URL_PARTS = _config_string_tuple("low_value_marketing_url_parts", LOW_VALUE_MARKETING_URL_PARTS)
LOW_VALUE_SUPPORT_DOMAINS = _config_string_set("low_value_support_domains", LOW_VALUE_SUPPORT_DOMAINS)
LOW_VALUE_FAQ_PREFIXES = _config_string_tuple("low_value_faq_prefixes", LOW_VALUE_FAQ_PREFIXES)
OFFICIAL_ANNOUNCEMENT_HOSTS = _config_string_set("official_announcement_hosts", OFFICIAL_ANNOUNCEMENT_HOSTS)
UNDATED_OFFICIAL_TITLE_PREFIXES = _config_string_tuple(
    "undated_official_title_prefixes",
    UNDATED_OFFICIAL_TITLE_PREFIXES,
)
FAST_MOVING_NEWS_KEYWORDS = _config_string_set("fast_moving_news_keywords", FAST_MOVING_NEWS_KEYWORDS)
SEARCH_GENERIC_PAGE_MARKERS = _config_string_tuple("search_generic_page_markers", SEARCH_GENERIC_PAGE_MARKERS)
AI_SIGNAL_PATTERNS = list(_config_string_tuple("ai_signal_patterns", tuple(AI_SIGNAL_PATTERNS)))
AI_NEWS_TAVILY_QUERY_TEMPLATES = list(
    _config_string_tuple("ai_news_tavily_query_templates", tuple(AI_NEWS_TAVILY_QUERY_TEMPLATES))
)
# 每条模板每天一次 advanced 搜索（2 credits），条数失控会直接推高 Tavily 账单；
# 周维护新词总是追加在尾部，超限时截尾可保住核心覆盖并锁死费用上限。
AI_NEWS_TAVILY_QUERY_CAP = _config_int("ai_news_tavily_query_cap", 35)
if len(AI_NEWS_TAVILY_QUERY_TEMPLATES) > AI_NEWS_TAVILY_QUERY_CAP:
    print(
        f"[news-rules] ai_news_tavily_query_templates {len(AI_NEWS_TAVILY_QUERY_TEMPLATES)} 条"
        f"超出上限 {AI_NEWS_TAVILY_QUERY_CAP}，已截断尾部多余条目",
        file=sys.stderr,
    )
    AI_NEWS_TAVILY_QUERY_TEMPLATES = AI_NEWS_TAVILY_QUERY_TEMPLATES[:AI_NEWS_TAVILY_QUERY_CAP]
AI_NEWS_BRAVE_QUERY_TEMPLATES = list(
    _config_string_tuple("ai_news_brave_query_templates", tuple(AI_NEWS_BRAVE_QUERY_TEMPLATES))
)
AI_NEWS_BACKFILL_BRAVE_QUERY_TEMPLATES = _config_string_list_map(
    "ai_news_backfill_brave_query_templates",
    AI_NEWS_BACKFILL_BRAVE_QUERY_TEMPLATES,
)
HIGH_QUALITY_DOMAINS = _config_domain_scores("high_quality_domains", HIGH_QUALITY_DOMAINS)
LOW_QUALITY_DOMAINS = _config_domain_scores("low_quality_domains", LOW_QUALITY_DOMAINS)
GAMING_CONTEXT_KEYWORDS = _config_string_set("gaming_context_keywords", GAMING_CONTEXT_KEYWORDS)
GAMING_REVIEW_MARKERS = _config_string_set("gaming_review_markers", GAMING_REVIEW_MARKERS)
DYNAMIC_ENTITY_ALLOWLIST = _config_string_tuple("dynamic_entity_allowlist", DYNAMIC_ENTITY_ALLOWLIST)
DYNAMIC_TERM_STOPWORDS = DYNAMIC_TERM_STOPWORDS | _config_string_set("dynamic_term_stopwords", set())
SOURCE_QUERY_MATCH_STOPWORDS = SOURCE_QUERY_MATCH_STOPWORDS | _config_string_set(
    "source_query_match_stopwords",
    set(),
)
SEARCH_REQUEST_MAX_WORKERS = max(1, int(_RULES_CONFIG_OVERRIDES.get("search_request_max_workers", 6)))
AI_NEWS_LLM_FILTER_ENABLED = _config_bool("ai_news_llm_filter_enabled", True)
AI_NEWS_LLM_FILTER_MIN_SIGNAL = max(1, _config_int("ai_news_llm_filter_min_signal", 2))
AI_NEWS_LLM_FILTER_MAX_SIGNAL = max(AI_NEWS_LLM_FILTER_MIN_SIGNAL, _config_int("ai_news_llm_filter_max_signal", 4))


@dataclass(slots=True)
class NewsItem:
    title: str = ""
    url: str = ""
    content: str = ""
    description: str = ""
    published_date: str = ""
    score: float = 0.0
    source_query: str = ""
    extra_snippets: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, item: Mapping[str, Any] | None) -> "NewsItem":
        if not isinstance(item, Mapping):
            return cls()
        core_keys = {
            "title",
            "url",
            "content",
            "description",
            "published_date",
            "score",
            "source_query",
            "extra_snippets",
        }
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        extra_snippets = item.get("extra_snippets", [])
        snippets = [str(value or "").strip() for value in extra_snippets] if isinstance(extra_snippets, list) else []
        return cls(
            title=str(item.get("title") or "").strip(),
            url=str(item.get("url") or "").strip(),
            content=str(item.get("content") or "").strip(),
            description=str(item.get("description") or "").strip(),
            published_date=str(item.get("published_date") or "").strip(),
            score=score,
            source_query=str(item.get("source_query") or "").strip(),
            extra_snippets=[value for value in snippets if value],
            extra={key: value for key, value in item.items() if key not in core_keys},
        )

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.extra)
        payload.update(
            {
                "title": self.title,
                "url": self.url,
                "content": self.content,
                "description": self.description,
                "published_date": self.published_date,
                "score": self.score,
                "source_query": self.source_query,
            }
        )
        if self.extra_snippets:
            payload["extra_snippets"] = list(self.extra_snippets)
        return compact_dict(payload)


@dataclass(slots=True)
class QueryFetchResult:
    query: str
    payload: dict[str, Any] | None = None
    error: str = ""


def now_local() -> dt.datetime:
    return dt.datetime.now()


def next_episode_id() -> str:
    base = now_local().strftime("%Y%m%d")
    episodes_dir = ROOT_DIR / "episodes"
    existing = sorted(path.name for path in episodes_dir.glob(f"{base}E*") if path.is_dir())
    suffixes = []
    for name in existing:
        try:
            suffixes.append(int(name.split("E", 1)[1]))
        except (IndexError, ValueError):
            continue
    next_index = max(suffixes, default=0) + 1
    return f"{base}E{next_index}"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def daily_search_cache_path(date_key: str | None = None) -> Path:
    key = clean_text(date_key or "") or now_local().strftime("%Y%m%d")
    return DAILY_SEARCH_CACHE_DIR / f"{key}_search_results.json"


def load_daily_search_cache() -> dict[str, Any] | None:
    path = daily_search_cache_path()
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except Exception:
        return None
    cache_meta = dict(payload.get("cache_meta", {}))
    cache_meta.update(
        {
            "status": "hit",
            "path": str(path),
            "loaded_at": now_local().isoformat(),
        }
    )
    payload["cache_meta"] = cache_meta
    return payload


def save_daily_search_cache(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_dir(DAILY_SEARCH_CACHE_DIR)
    path = daily_search_cache_path()
    cached_payload = dict(payload)
    cache_meta = dict(cached_payload.get("cache_meta", {}))
    cache_meta.update(
        {
            "status": "fresh",
            "path": str(path),
            "saved_at": now_local().isoformat(),
            "date": now_local().strftime("%Y-%m-%d"),
        }
    )
    cached_payload["cache_meta"] = cache_meta
    write_json(path, cached_payload)
    return cached_payload


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def compact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in ("", None, [], {})}


def append_record_csv(row: dict[str, Any]) -> None:
    headers = list(row.keys())
    exists = RECORD_CSV.exists()
    with RECORD_CSV.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def write_pending_download(payload: dict[str, Any]) -> None:
    write_json(PENDING_DOWNLOAD_FILE, payload)


def clear_pending_download() -> None:
    PENDING_DOWNLOAD_FILE.unlink(missing_ok=True)


def read_optional_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {}


def existing_listenhub_episode_id(episode_dir: Path) -> str:
    for path in [
        episode_dir / "listenhub_episode_result.json",
        episode_dir / "listenhub_create_result.json",
        episode_dir / "record.json",
    ]:
        payload = read_optional_json(path)
        episode_id = str(payload.get("episodeId", "")).strip()
        if episode_id:
            return episode_id
        episode_url = str(
            payload.get("ListenHub作品链接")
            or payload.get("episodeUrl")
            or payload.get("href")
            or ""
        ).strip()
        match = re.search(r"/episode/([A-Za-z0-9]+)", episode_url)
        if match:
            return match.group(1)
    episode_url_path = episode_dir / "episode_url.txt"
    if episode_url_path.exists():
        match = re.search(r"/episode/([A-Za-z0-9]+)", episode_url_path.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    return ""


def select_podcast_hook(seed_text: str) -> str:
    normalized = clean_text(seed_text)
    if not normalized:
        return PODCAST_HOOK_TEMPLATES[0]
    score = sum(ord(ch) for ch in normalized)
    return PODCAST_HOOK_TEMPLATES[score % len(PODCAST_HOOK_TEMPLATES)]


TRANSIENT_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
# Tavily 在 key 配额耗尽 / 限流时返回的状态码：432=用量超限，433=plan 限制，429=限流。
# 命中这些码时，免费 key 视为暂时不可用，切换到付费兜底 key 重试同一次查询。
TAVILY_QUOTA_HTTP_CODES = {429, 432, 433}
HTTP_RETRY_ATTEMPTS = 3
HTTP_RETRY_BASE_DELAY_SECONDS = 2.0


def _request_label(req: urllib.request.Request) -> str:
    return f"{req.get_method()} {req.full_url}"


def _sleep_before_http_retry(req: urllib.request.Request, attempt: int, total_attempts: int, exc: Exception) -> None:
    delay = HTTP_RETRY_BASE_DELAY_SECONDS * attempt
    print(
        f"[http] { _request_label(req) } 失败 ({attempt}/{total_attempts}): {exc}; {delay:.1f}s 后重试。",
        file=sys.stderr,
    )
    time.sleep(delay)


def _http_read_with_retry(req: urllib.request.Request, *, timeout: float) -> bytes:
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_STATUS_CODES or attempt >= HTTP_RETRY_ATTEMPTS:
                raise
            _sleep_before_http_retry(req, attempt, HTTP_RETRY_ATTEMPTS, exc)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= HTTP_RETRY_ATTEMPTS:
                raise
            _sleep_before_http_retry(req, attempt, HTTP_RETRY_ATTEMPTS, exc)
    raise RuntimeError(f"HTTP 请求重试后仍失败: {_request_label(req)}")


def http_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    timeout: float = 90,
) -> dict[str, Any]:
    body = None
    request_method = method
    request_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_method = request_method or "POST"
    req = urllib.request.Request(url, data=body, headers=request_headers, method=request_method)
    return json.loads(_http_read_with_retry(req, timeout=timeout).decode("utf-8"))


def http_text(url: str, *, headers: dict[str, str] | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    return _http_read_with_retry(req, timeout=90).decode("utf-8")


def http_bytes(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> bytes:
    body = None
    request_method = method
    request_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_method = request_method or "POST"
    req = urllib.request.Request(url, data=body, headers=request_headers, method=request_method)
    return _http_read_with_retry(req, timeout=180)


def _extract_json_fragment(text: str) -> str:
    cleaned = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", cleaned, flags=re.S)
    if fenced:
        return fenced.group(1).strip()
    start = min([idx for idx in (cleaned.find("{"), cleaned.find("[")) if idx != -1], default=-1)
    if start == -1:
        raise ValueError("未找到 JSON 起始位置")
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if end <= start:
        raise ValueError("未找到 JSON 结束位置")
    return cleaned[start : end + 1].strip()


def _openai_compatible_json(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_content: str,
    label: str,
    no_proxy: bool = False,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
    }
    body = None
    request_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers=request_headers,
        method="POST",
    )

    saved_proxy: dict[str, str] = {}
    opener = None
    if no_proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if key in os.environ:
                saved_proxy[key] = os.environ.pop(key)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        if opener:
            response = opener.open(req, timeout=90)
        else:
            response = urllib.request.urlopen(req, timeout=90)
        raw = json.loads(response.read())
        response.close()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"{label}: HTTP {exc.code} {detail}") from exc
    finally:
        for key, value in saved_proxy.items():
            os.environ[key] = value

    choices = raw.get("choices", [])
    if not choices:
        raise RuntimeError(f"{label}: 无返回 choices")
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError(f"{label}: 空文本")
    return json.loads(_extract_json_fragment(text))


def _openai_compatible_text(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_content: str,
    label: str,
    max_tokens: int = 4000,
    no_proxy: bool = False,
) -> str:
    """Call an OpenAI-compatible chat endpoint and return plain text (not JSON)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers=request_headers,
        method="POST",
    )
    saved_proxy: dict[str, str] = {}
    opener = None
    if no_proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if key in os.environ:
                saved_proxy[key] = os.environ.pop(key)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        if opener:
            response = opener.open(req, timeout=120)
        else:
            response = urllib.request.urlopen(req, timeout=120)
        raw = json.loads(response.read())
        response.close()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"{label}: HTTP {exc.code} {detail}") from exc
    finally:
        for key, value in saved_proxy.items():
            os.environ[key] = value
    choices = raw.get("choices", [])
    if not choices:
        raise RuntimeError(f"{label}: no choices returned")
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError(f"{label}: empty response")
    return text



def _llm_relevance_verdict(
    *,
    title: str,
    content: str,
    url: str,
    source_query: str,
) -> bool:
    system_prompt = (
        "你是 AI 科技新闻过滤器。"
        "请判断输入条目是否属于『适合每日 AI 商业/产品/大模型/开源项目播客播报的严肃新闻』。"
        "体育、游戏、泛教程、FAQ、帮助文档、营销页、行业空话、周报盘点、与 AI 仅弱相关的泛科技内容都应该判 false。"
        "只返回 JSON：{\"is_relevant\": true/false, \"reason\": \"...\"}"
    )
    user_content = json.dumps(
        {
            "title": clean_text(title),
            "content": clean_text(content)[:900],
            "url": url,
            "source_query": clean_text(source_query),
        },
        ensure_ascii=False,
    )
    runners: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    qwen_key = os.getenv("QWEN_API_KEY", "").strip()
    if deepseek_key:
        runners.append(
            (
                "DeepSeek",
                lambda: _openai_compatible_json(
                    api_key=deepseek_key,
                    base_url="https://api.deepseek.com/v1",
                    model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                    system_prompt=system_prompt,
                    user_content=user_content,
                    label="deepseek relevance",
                ),
            )
        )
    if qwen_key:
        runners.append(
            (
                "Qwen-Turbo",
                lambda: _openai_compatible_json(
                    api_key=qwen_key,
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model=os.getenv("QWEN_MODEL", "qwen-turbo"),
                    system_prompt=system_prompt,
                    user_content=user_content,
                    label="qwen relevance",
                    no_proxy=True,
                ),
            )
        )
    if not runners:
        return True
    last_error = ""
    for name, runner in runners:
        try:
            payload = runner()
            return bool(payload.get("is_relevant", True))
        except Exception as exc:
            last_error = f"{name}: {exc}"
            print(f"[filter] {name} 边缘新闻判别失败: {exc}", file=sys.stderr)
    if last_error:
        print(f"[filter] 回退规则过滤: {last_error}", file=sys.stderr)
    return True


_AI_RELEVANCE_LLM_CACHE: dict[str, bool] = {}


def listenhub_api_headers() -> dict[str, str]:
    api_key = os.getenv("LISTENHUB_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 LISTENHUB_API_KEY。")
    return {"Authorization": f"Bearer {api_key}"}


def listenhub_get(path: str) -> dict[str, Any]:
    return http_json(f"{LISTENHUB_API_BASE}{path}", headers=listenhub_api_headers(), method="GET")


def listenhub_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return http_json(f"{LISTENHUB_API_BASE}{path}", payload=payload, headers=listenhub_api_headers())


def fetch_tavily_news(
    query: str,
    api_key: str,
    max_results: int = 8,
    *,
    fallback_key: str | None = None,
) -> dict[str, Any]:
    # 免费 key 优先；额度耗尽（432/433/429）时自动切到付费兜底 key 重试同一次查询。
    keys = [api_key]
    if fallback_key and fallback_key != api_key:
        keys.append(fallback_key)
    last_error: urllib.error.HTTPError | None = None
    for idx, key in enumerate(keys):
        try:
            return http_json(
                "https://api.tavily.com/search",
                payload={
                    "api_key": key,
                    "query": query,
                    "search_depth": "advanced",
                    "topic": "news",
                    "max_results": max_results,
                    "include_answer": True,
                },
            )
        except urllib.error.HTTPError as exc:
            has_fallback_left = idx + 1 < len(keys)
            if exc.code in TAVILY_QUOTA_HTTP_CODES and has_fallback_left:
                print(
                    f"[tavily] key#{idx} 配额受限(HTTP {exc.code})，切换付费兜底 key 重试: {query}",
                    file=sys.stderr,
                )
                last_error = exc
                continue
            raise
    if last_error is not None:
        raise last_error
    raise RuntimeError("Tavily 请求失败：无可用 key")


def parse_result_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    for parser in (dt.datetime.fromisoformat,):
        try:
            parsed = parser(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value, fmt).replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    relative_match = re.search(
        r"(?P<count>\d+)\s+(?P<unit>minute|hour|day|week|month|year)s?\s+ago",
        value,
        flags=re.I,
    )
    if relative_match:
        count = int(relative_match.group("count"))
        unit = relative_match.group("unit").lower()
        delta_map = {
            "minute": dt.timedelta(minutes=count),
            "hour": dt.timedelta(hours=count),
            "day": dt.timedelta(days=count),
            "week": dt.timedelta(weeks=count),
            "month": dt.timedelta(days=30 * count),
            "year": dt.timedelta(days=365 * count),
        }
        delta = delta_map.get(unit)
        if delta:
            return dt.datetime.now(dt.timezone.utc) - delta
    return None


EMBEDDED_DATE_PATTERNS = (
    re.compile(r"update on\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", flags=re.I),
    re.compile(r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})"),
    re.compile(r"(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?)"),
)


def extract_embedded_datetime(value: str) -> dt.datetime | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    direct = parse_result_datetime(cleaned)
    if direct:
        return direct
    for pattern in EMBEDDED_DATE_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue
        parsed = parse_result_datetime(match.group(1))
        if parsed:
            return parsed
    return None


def search_item_datetime(item: dict[str, Any]) -> dt.datetime | None:
    primary_candidates: list[dt.datetime] = []
    for key in ("published_date", "publishedAt", "date", "page_age", "age"):
        parsed = parse_result_datetime(str(item.get(key) or ""))
        if parsed:
            primary_candidates.append(parsed)
    if primary_candidates:
        return max(primary_candidates)

    for snippet in item.get("extra_snippets", []) or []:
        parsed = extract_embedded_datetime(str(snippet or ""))
        if parsed:
            return parsed

    for key in ("content", "description"):
        parsed = extract_embedded_datetime(str(item.get(key) or ""))
        if parsed:
            return parsed
    return None


def item_title(item: dict[str, Any]) -> str:
    return clean_text(item.get("title", "") or item.get("description", ""))


def result_sort_key(item: dict[str, Any]) -> tuple[float, float]:
    published = search_item_datetime(item)
    timestamp = published.timestamp() if published else 0.0
    score = float(item.get("score") or 0.0)
    return (timestamp, score)


def items_represent_same_story(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_url = canonicalize_url(left.get("url", ""))
    right_url = canonicalize_url(right.get("url", ""))
    if left_url and right_url and left_url == right_url:
        return True
    left_title = item_title(left)
    right_title = item_title(right)
    if not left_title or not right_title:
        return False
    if normalize_title_key(left_title) == normalize_title_key(right_title):
        return True
    if title_similarity(left_title, right_title) >= 0.8:
        return True
    overlap_count, overlap_coverage = title_overlap_coverage(left_title, right_title)
    return overlap_count >= 3 and overlap_coverage >= 0.5


def dedupe_search_items(items: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    ranked = sorted(items, key=result_sort_key, reverse=True)
    kept: list[dict[str, Any]] = []
    for item in ranked:
        if any(items_represent_same_story(item, existing) for existing in kept):
            continue
        kept.append(item)
        if len(kept) >= limit:
            break
    return kept


def bucket_result_items(bucket_payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in SEARCH_RESULT_KEYS:
        values = bucket_payload.get(key, [])
        if isinstance(values, list):
            items.extend(item for item in values if isinstance(item, dict))
    return items


def run_parallel_query_fetches(
    queries: list[str],
    fetcher: Callable[[str], dict[str, Any]],
    *,
    max_workers: int | None = None,
) -> list[QueryFetchResult]:
    if not queries:
        return []
    positions = {query: idx for idx, query in enumerate(queries)}
    worker_count = max(1, min(max_workers or SEARCH_REQUEST_MAX_WORKERS, len(queries)))
    if worker_count == 1:
        results: list[QueryFetchResult] = []
        for query in queries:
            try:
                results.append(QueryFetchResult(query=query, payload=fetcher(query)))
            except Exception as exc:
                results.append(QueryFetchResult(query=query, error=str(exc)))
        return results

    results: list[QueryFetchResult] = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="search-fetch") as executor:
        future_map = {executor.submit(fetcher, query): query for query in queries}
        for future in as_completed(future_map):
            query = future_map[future]
            try:
                results.append(QueryFetchResult(query=query, payload=future.result()))
            except Exception as exc:
                results.append(QueryFetchResult(query=query, error=str(exc)))
    return sorted(results, key=lambda item: positions.get(item.query, 0))


def fetch_tavily_batch(
    queries: list[str],
    api_key: str,
    *,
    max_results_per_query: int = 6,
    merged_limit: int = 12,
    fallback_key: str | None = None,
) -> dict[str, Any]:
    merged_results: list[dict[str, Any]] = []
    answers: list[str] = []
    filtered_out: list[dict[str, str]] = []
    errors: list[str] = []
    fetch_results = run_parallel_query_fetches(
        queries,
        lambda query: fetch_tavily_news(
            query, api_key, max_results=max_results_per_query, fallback_key=fallback_key
        ),
    )
    for result_item in fetch_results:
        query = result_item.query
        if result_item.error:
            errors.append(f"{query}: {result_item.error}")
            continue
        payload = result_item.payload or {}
        answer = clean_text(payload.get("answer", ""))
        if answer:
            answers.append(f"{query}: {answer}")
        for item in payload.get("results", []):
            sanitized = sanitize_search_item({**item, "source_query": query})
            if sanitized.get("title") or sanitized.get("content"):
                reason = search_item_rejection_reason(
                    sanitized.get("title", ""),
                    sanitized.get("content", "") or sanitized.get("description", ""),
                    sanitized.get("url", ""),
                    source_query=query,
                    item=sanitized,
                )
                if reason:
                    filtered_out.append(
                        compact_dict(
                            {
                                "reason": reason,
                                "title": sanitized.get("title", ""),
                                "url": sanitized.get("url", ""),
                                "source_query": query,
                            }
                        )
                    )
                    continue
                merged_results.append(sanitized)
    result = {
        "generated_at": now_local().isoformat(),
        "tavily_answer": "\n".join(answers[:4]),
        "tavily_results": dedupe_search_items(merged_results, limit=merged_limit),
        "queries": queries,
    }
    if errors:
        result["errors"] = errors[:6]
    if filtered_out:
        result["filtered_out"] = filtered_out[:20]
        result["filtered_counts"] = summarize_filtered_reasons(filtered_out)
    return result


def fetch_brave_search(query: str, api_key: str, count: int = 8, timeout: float = 25) -> dict[str, Any]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": count, "search_lang": "zh-hans", "country": "US"}
    )
    return http_json(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
        method="GET",
        timeout=timeout,
    )


def fetch_brave_batch(
    queries: list[str], api_key: str, *, count_per_query: int = 6, merged_limit: int = 12
) -> dict[str, Any]:
    merged_results: list[dict[str, Any]] = []
    errors: list[str] = []
    filtered_out: list[dict[str, str]] = []
    fetch_results = run_parallel_query_fetches(
        queries,
        lambda query: fetch_brave_search(query, api_key, count=count_per_query),
    )
    for result_item in fetch_results:
        query = result_item.query
        if result_item.error:
            errors.append(f"{query}: {result_item.error}")
            continue
        payload = result_item.payload or {}
        for item in payload.get("web", {}).get("results", []):
            sanitized = sanitize_search_item({**item, "source_query": query})
            if sanitized.get("title") or sanitized.get("content") or sanitized.get("description"):
                reason = search_item_rejection_reason(
                    sanitized.get("title", ""),
                    sanitized.get("content", "") or sanitized.get("description", ""),
                    sanitized.get("url", ""),
                    source_query=query,
                    item=sanitized,
                )
                if reason:
                    filtered_out.append(
                        compact_dict(
                            {
                                "reason": reason,
                                "title": sanitized.get("title", ""),
                                "url": sanitized.get("url", ""),
                                "source_query": query,
                            }
                        )
                    )
                    continue
                merged_results.append(sanitized)
    result = {
        "web": {
            "results": dedupe_search_items(merged_results, limit=merged_limit),
        },
        "queries": queries,
    }
    if errors:
        result["errors"] = errors[:6]
    if filtered_out:
        result["filtered_out"] = filtered_out[:20]
        result["filtered_counts"] = summarize_filtered_reasons(filtered_out)
    return result


# ===========================================================================
# A1+A2: RSS 直订阅（聚合源 + 一手 newsroom）
# ===========================================================================

RSS_FEEDS_CONFIG_PATH = ROOT_DIR / "rss_feeds.json"
RSS_DEFAULT_MAX_AGE_HOURS = 36
RSS_DEFAULT_MAX_PER_FEED = 8
RSS_DEFAULT_TIMEOUT = 20
RSS_DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) ai-observer-rss/1.0"

# Hacker News 官方 Firebase API（免费、无需 key）。AI 发布 / Show HN / 讨论常在这里最早冒头。
HACKERNEWS_API_BASE = "https://hacker-news.firebaseio.com/v0"
HACKERNEWS_MAX_AGE_HOURS = 48
HACKERNEWS_SCAN_TOP = 150  # 扫描 topstories 前 N
HACKERNEWS_SCAN_NEW = 150  # 扫描 newstories 前 N
HACKERNEWS_MAX_KEEP = 15  # 最多保留多少条 AI 相关
HACKERNEWS_MIN_SCORE = 15  # HN points 门槛，过滤低关注度噪声
HACKERNEWS_FETCH_WORKERS = 12
# AI 主题标题预过滤（HN 是泛科技源，需先筛掉非 AI 条目，避免大量无关 item 进 bucket）。
# 短词用 \b 词边界，避免 "ai" 命中 "again/email/rain" 这类误匹配。
HACKERNEWS_AI_TITLE_PATTERN = re.compile(
    r"\b("
    r"a\.?i|ml|llms?|gpt|rag|gpus?|agentic|agents?|"
    r"openai|anthropic|claude|chatgpt|gemini|deepmind|mistral|llama|qwen|deepseek|"
    r"copilot|cursor|perplexity|hugging\s?face|"
    r"machine learning|deep learning|neural network|transformer|diffusion|multimodal|"
    r"language model|foundation model|generative ai|fine-?tun(?:e|ing)|inference|"
    r"nvidia|tensor|mcp|humanoid|robots?|self-driving|autonomous|computer vision|"
    r"reasoning model|open-?weight"
    r")\b",
    re.I,
)


def _load_rss_feeds_config() -> dict[str, Any]:
    """读取 rss_feeds.json，返回 {feeds, max_age_hours, max_per_feed, request_timeout}。"""
    if not RSS_FEEDS_CONFIG_PATH.exists():
        return {"feeds": [], "max_age_hours": RSS_DEFAULT_MAX_AGE_HOURS,
                "max_per_feed": RSS_DEFAULT_MAX_PER_FEED, "request_timeout": RSS_DEFAULT_TIMEOUT}
    try:
        raw = read_json(RSS_FEEDS_CONFIG_PATH)
    except Exception as exc:
        print(f"[RSS] 配置文件解析失败: {exc}", file=sys.stderr)
        return {"feeds": [], "max_age_hours": RSS_DEFAULT_MAX_AGE_HOURS,
                "max_per_feed": RSS_DEFAULT_MAX_PER_FEED, "request_timeout": RSS_DEFAULT_TIMEOUT}
    feeds = [f for f in raw.get("feeds", []) if isinstance(f, dict) and f.get("url")]
    return {
        "feeds": feeds,
        "max_age_hours": float(raw.get("max_age_hours") or RSS_DEFAULT_MAX_AGE_HOURS),
        "max_per_feed": int(raw.get("max_per_feed") or RSS_DEFAULT_MAX_PER_FEED),
        "request_timeout": int(raw.get("request_timeout") or RSS_DEFAULT_TIMEOUT),
    }


def _entry_published_dt(entry: Any) -> dt.datetime | None:
    """从 feedparser entry 提取发布时间，统一返回带 UTC tzinfo 的 datetime。"""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        struct = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if struct:
            try:
                return dt.datetime(*struct[:6], tzinfo=dt.timezone.utc)
            except Exception:
                continue
    for key in ("published", "updated", "created"):
        raw = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if raw:
            parsed = parse_result_datetime(str(raw))
            if parsed:
                return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    return None


def _entry_text(entry: Any, key: str) -> str:
    val = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else "")
    if isinstance(val, list) and val:
        val = val[0]
    if isinstance(val, dict):
        val = val.get("value", "")
    return str(val or "").strip()


def _strip_html(text: str) -> str:
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    no_entities = re.sub(r"&[a-z#0-9]+;", " ", no_tags)
    return re.sub(r"\s+", " ", no_entities).strip()


def fetch_single_rss_feed(
    feed_config: dict[str, Any],
    *,
    max_age_hours: float,
    max_per_feed: int,
    timeout: int,
) -> dict[str, Any]:
    """抓取单个 RSS 源，返回 {items: [...], errors: []}。"""
    try:
        import feedparser
    except ImportError:
        return {"items": [], "errors": ["feedparser not installed"]}

    url = feed_config["url"]
    name = feed_config.get("name", url)
    bucket = feed_config.get("bucket", "ai_news")
    lang = feed_config.get("lang", "en")
    weight = float(feed_config.get("weight") or 5.0)
    category = feed_config.get("category", "aggregator")

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    now_utc = dt.datetime.now(dt.timezone.utc)
    cutoff = now_utc - dt.timedelta(hours=max_age_hours)

    # feedparser 支持 request_headers + 内置 socket 超时
    import socket
    import time as _time

    socket.setdefaulttimeout(timeout)
    parsed = None
    last_err = ""
    # 退避重试：新闻站 RSS 在 cron 时段常出现瞬时 SSL 握手失败/超时，
    # 单次抓取会让整源当天归零，这里给 3 次机会。
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        try:
            parsed = feedparser.parse(url, agent=RSS_DEFAULT_USER_AGENT)
            if not parsed.bozo or parsed.entries:
                break  # 拿到可用条目即成功（bozo 但有 entries 也算）
            last_err = f"bozo {parsed.bozo_exception}"
        except Exception as exc:
            last_err = str(exc)
        if attempt < HTTP_RETRY_ATTEMPTS:
            _time.sleep(HTTP_RETRY_BASE_DELAY_SECONDS * attempt)
    if parsed is None or (parsed.bozo and not parsed.entries):
        return {"items": [], "errors": [f"{name}: {last_err}"], "bucket": bucket}

    for entry in (parsed.entries or [])[: max_per_feed * 3]:  # 取 3 倍冗余，按时间过滤后截断
        title = _entry_text(entry, "title")
        link = _entry_text(entry, "link") or _entry_text(entry, "id")
        if not title or not link:
            continue
        published_dt = _entry_published_dt(entry)
        if published_dt is None:
            # 无日期的条目跳过（RSS 应该都有 published）
            continue
        if published_dt < cutoff:
            continue

        summary = _strip_html(_entry_text(entry, "summary") or _entry_text(entry, "description"))
        content_raw = ""
        contents = getattr(entry, "content", None)
        if isinstance(contents, list) and contents:
            content_raw = _strip_html(str(contents[0].get("value", "") if isinstance(contents[0], dict) else contents[0]))
        body = content_raw or summary

        item = {
            "title": title,
            "url": link,
            "content": body[:1200],
            "description": summary[:600],
            "published_date": published_dt.isoformat(),
            "score": weight,
            "source_query": f"rss:{name}",
            "feed_name": name,
            "feed_lang": lang,
            "feed_category": category,
            "provider": "rss",
        }
        items.append(item)
        if len(items) >= max_per_feed:
            break

    return {"items": items, "errors": errors, "bucket": bucket}


def fetch_rss_batch(
    *,
    feeds: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """并发抓取所有 RSS 源，按 bucket 拆分返回:
        {
          "ai_news": {"web": {"results": [...]}, "queries": [...], "errors": [...], "rss_meta": {...}},
          "product_hunt": {...同上},
        }
    返回结构与 fetch_brave_batch 兼容，可直接进 merge_brave_provider_payloads。
    """
    cfg = config or _load_rss_feeds_config()
    feed_list = feeds if feeds is not None else cfg.get("feeds", [])
    if not feed_list:
        return {}

    max_age_hours = cfg.get("max_age_hours", RSS_DEFAULT_MAX_AGE_HOURS)
    max_per_feed = cfg.get("max_per_feed", RSS_DEFAULT_MAX_PER_FEED)
    timeout = cfg.get("request_timeout", RSS_DEFAULT_TIMEOUT)

    by_bucket: dict[str, dict[str, Any]] = {}
    all_errors: list[str] = []
    feed_stats: dict[str, dict[str, Any]] = {}

    worker_count = min(8, max(1, len(feed_list)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rss-fetch") as executor:
        future_map = {
            executor.submit(
                fetch_single_rss_feed,
                feed,
                max_age_hours=max_age_hours,
                max_per_feed=max_per_feed,
                timeout=timeout,
            ): feed
            for feed in feed_list
        }
        for future in as_completed(future_map):
            feed = future_map[future]
            name = feed.get("name", feed.get("url", ""))
            try:
                result = future.result()
            except Exception as exc:
                all_errors.append(f"{name}: {exc}")
                feed_stats[name] = {"count": 0, "error": str(exc)}
                continue
            bucket = result.get("bucket", "ai_news")
            items = result.get("items", [])
            errs = result.get("errors", [])
            if errs:
                all_errors.extend(errs)
            feed_stats[name] = {
                "count": len(items),
                "bucket": bucket,
                "category": feed.get("category", "aggregator"),
                "weight": feed.get("weight", 5),
            }
            bucket_payload = by_bucket.setdefault(
                bucket,
                {"web": {"results": []}, "queries": [], "errors": [], "rss_meta": {"feeds": []}},
            )
            bucket_payload["web"]["results"].extend(items)
            bucket_payload["queries"].append(f"rss:{name}")
            bucket_payload["rss_meta"]["feeds"].append({"name": name, "count": len(items)})

    if all_errors:
        for bucket_payload in by_bucket.values():
            bucket_payload["errors"] = (bucket_payload.get("errors") or []) + all_errors[:6]

    # 终端摘要
    total = sum(s.get("count", 0) for s in feed_stats.values())
    failures = [n for n, s in feed_stats.items() if s.get("error") or s.get("count") == 0]
    print(
        f"\033[1;36m[RSS] 抓取完成：{len(feed_list)} 源, "
        f"成功 {len(feed_list) - len(failures)} 源, "
        f"共 {total} 条候选, 失败/空 {len(failures)} 源\033[0m",
        file=sys.stderr,
    )
    if failures:
        print(
            f"\033[33m[RSS] 失败/空源: {', '.join(failures[:8])}{'...' if len(failures) > 8 else ''}\033[0m",
            file=sys.stderr,
        )

    return by_bucket


def _fetch_hackernews_item(item_id: int) -> dict[str, Any] | None:
    """抓单条 HN item，失败返回 None。"""
    try:
        return http_json(f"{HACKERNEWS_API_BASE}/item/{item_id}.json", method="GET", timeout=15)
    except Exception:
        return None


def fetch_hackernews_batch(
    *,
    max_age_hours: float = HACKERNEWS_MAX_AGE_HOURS,
    max_keep: int = HACKERNEWS_MAX_KEEP,
    min_score: int = HACKERNEWS_MIN_SCORE,
) -> dict[str, Any]:
    """抓取 Hacker News top/new stories，按 AI 主题 + 时间窗 + 点赞门槛筛选。
    返回 {items: [...], errors: [...], scanned: N, kept: N}，items 结构与 RSS 条目一致，
    可直接并入 ai_news bucket 的 rss_results 链路。"""
    errors: list[str] = []
    now_utc = dt.datetime.now(dt.timezone.utc)
    cutoff = now_utc - dt.timedelta(hours=max_age_hours)

    ids: list[int] = []
    seen_ids: set[int] = set()
    for endpoint, limit in (("topstories", HACKERNEWS_SCAN_TOP), ("newstories", HACKERNEWS_SCAN_NEW)):
        try:
            raw = http_json(f"{HACKERNEWS_API_BASE}/{endpoint}.json", method="GET", timeout=15)
        except Exception as exc:
            errors.append(f"hackernews:{endpoint}: {exc}")
            continue
        for sid in (raw or [])[:limit]:
            if isinstance(sid, int) and sid not in seen_ids:
                seen_ids.add(sid)
                ids.append(sid)

    if not ids:
        return {"items": [], "errors": errors or ["hackernews: 未取到 story id 列表"], "scanned": 0, "kept": 0}

    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    with ThreadPoolExecutor(max_workers=HACKERNEWS_FETCH_WORKERS, thread_name_prefix="hn-fetch") as executor:
        for story in executor.map(_fetch_hackernews_item, ids):
            if not isinstance(story, dict):
                continue
            if story.get("type") != "story" or story.get("dead") or story.get("deleted"):
                continue
            title = clean_text(story.get("title", ""))
            url = (story.get("url") or "").strip()
            if not title or not url:
                continue  # 只要带外链的 story（纯讨论帖跳过）
            if int(story.get("score", 0) or 0) < min_score:
                continue
            if not HACKERNEWS_AI_TITLE_PATTERN.search(title):
                continue
            ts = story.get("time")
            if not isinstance(ts, (int, float)):
                continue
            published_dt = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
            if published_dt < cutoff:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            items.append(
                {
                    "title": title,
                    "url": url,
                    "content": title[:1200],
                    "description": "",
                    "published_date": published_dt.isoformat(),
                    "score": 6.0,  # 与中档 RSS 源同权重，不让 HN 高点赞数干扰排序
                    "hn_points": int(story.get("score", 0) or 0),
                    "hn_comments": int(story.get("descendants", 0) or 0),
                    "source_query": f"hackernews:{story.get('id')}",
                    "feed_name": "Hacker News",
                    "feed_lang": "en",
                    "feed_category": "aggregator",
                    "provider": "hackernews",
                }
            )

    # 高点赞优先，截断到 max_keep
    items.sort(key=lambda it: it.get("hn_points", 0), reverse=True)
    items = items[:max_keep]

    print(
        f"\033[1;36m[HN] 扫描 {len(ids)} 条 story, AI 相关且达标 {len(items)} 条"
        f"(>= {min_score} pts, <= {max_age_hours:g}h)\033[0m",
        file=sys.stderr,
    )
    return {"items": items, "errors": errors, "scanned": len(ids), "kept": len(items)}


def filter_provider_results(
    raw_items: list[dict[str, Any]],
    *,
    queries: list[str],
    merged_limit: int = 12,
) -> dict[str, Any]:
    merged_results: list[dict[str, Any]] = []
    filtered_out: list[dict[str, str]] = []
    for item in raw_items:
        sanitized = sanitize_search_item(item)
        if not (sanitized.get("title") or sanitized.get("content") or sanitized.get("description")):
            continue
        source_query = clean_text(sanitized.get("source_query", ""))
        reason = search_item_rejection_reason(
            sanitized.get("title", ""),
            sanitized.get("content", "") or sanitized.get("description", ""),
            sanitized.get("url", ""),
            source_query=source_query,
            item=sanitized,
        )
        if reason:
            filtered_out.append(
                compact_dict(
                    {
                        "reason": reason,
                        "title": sanitized.get("title", ""),
                        "url": sanitized.get("url", ""),
                        "source_query": source_query,
                    }
                )
            )
            continue
        merged_results.append(sanitized)

    result = {
        "web": {
            "results": dedupe_search_items(merged_results, limit=merged_limit),
        },
        "queries": [clean_text(query) for query in queries if clean_text(query)],
    }
    if filtered_out:
        result["filtered_out"] = filtered_out[:20]
        result["filtered_counts"] = summarize_filtered_reasons(filtered_out)
    return result


def fetch_github_recent_star_repos(
    *,
    api_token: str = "",
    merged_limit: int = 10,
    per_query_limit: int = 6,
) -> dict[str, Any]:
    since_date = (now_local() - dt.timedelta(days=PRODUCT_COMMUNITY_GITHUB_RECENT_WINDOW_DAYS)).date().isoformat()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-observer-bot/1.0",
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    items: list[dict[str, Any]] = []
    errors: list[str] = []
    queries: list[str] = []
    now_utc = dt.datetime.now(dt.timezone.utc)

    for search in PRODUCT_COMMUNITY_GITHUB_SEARCHES:
        query = clean_text(str(search.get("query", "")).format(since_date=since_date))
        source_query = clean_text(str(search.get("source_query", "")))
        if not query or not source_query:
            continue
        queries.append(source_query)
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": per_query_limit,
            }
        )
        try:
            payload = http_json(url, headers=headers, method="GET", timeout=45)
        except Exception as exc:
            errors.append(f"github:{search.get('label', 'recent-stars')}: {exc}")
            continue
        for repo in payload.get("items", []) or []:
            created_raw = str(repo.get("created_at") or "")
            created_dt = parse_result_datetime(created_raw)
            if not created_dt:
                continue
            age_days = max((now_utc - created_dt.astimezone(dt.timezone.utc)).total_seconds() / 86400.0, 0.5)
            if age_days > PRODUCT_COMMUNITY_GITHUB_RECENT_WINDOW_DAYS + 1:
                continue
            stars = int(repo.get("stargazers_count") or 0)
            star_velocity = stars / age_days
            if star_velocity < PRODUCT_COMMUNITY_GITHUB_RECENT_MIN_STAR_VELOCITY:
                continue
            topics = [clean_text(str(topic or "")) for topic in repo.get("topics", []) or [] if topic]
            description = clean_text(repo.get("description", ""))
            full_name = clean_text(repo.get("full_name", ""))
            repo_haystack = f"{full_name} {description}".lower()
            if "openclaw" in repo_haystack:
                continue
            if any(
                token in repo_haystack
                for token in (
                    "awesome-",
                    " awesome ",
                    " handbook",
                    " tutorial",
                    " course",
                    " cheat sheet",
                    " source code",
                    "source-code",
                    "from scratch",
                    " mirror",
                    " archive",
                )
            ):
                continue
            if (
                full_name.lower().endswith(("-book", "_book"))
                or "/awesome" in full_name.lower()
                or full_name.lower().endswith("/book")
                or full_name.lower().endswith(("/mirror", "/archive"))
            ):
                continue
            language = clean_text(repo.get("language", ""))
            content_bits = [
                f"GitHub 新星开源项目 {full_name}",
                f"仓库创建于 {created_raw[:10]}",
                f"当前 star {stars}",
                f"近 {round(age_days)} 天平均每天新增约 {round(star_velocity)} star",
            ]
            if description:
                content_bits.append(description)
            if topics:
                content_bits.append("Topics: " + ", ".join(topics[:6]))
            if language:
                content_bits.append(f"主要语言 {language}")
            items.append(
                compact_dict(
                    {
                        "title": f"GitHub 新星项目 {full_name}",
                        "url": repo.get("html_url", ""),
                        "content": "；".join(content_bits),
                        "description": description,
                        "published_date": created_raw,
                        "score": round(star_velocity, 3),
                        "source_query": source_query,
                    }
                )
            )

    result = filter_provider_results(items, queries=queries, merged_limit=merged_limit)
    if errors:
        result["errors"] = errors[:6]
    return result


def fetch_huggingface_trending_items(
    *,
    api_token: str = "",
    merged_limit: int = 10,
    per_kind_limit: int = 6,
) -> dict[str, Any]:
    headers = {"User-Agent": "ai-observer-bot/1.0"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    cutoff = now_local() - dt.timedelta(days=PRODUCT_COMMUNITY_HF_RECENT_WINDOW_DAYS)
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    queries: list[str] = []

    for target in PRODUCT_COMMUNITY_HF_TARGETS:
        kind = target["kind"]
        label = clean_text(target["label"])
        queries.append(label)
        endpoint = f"https://huggingface.co/api/{kind}"
        params = {
            "limit": str(per_kind_limit),
            "sort": "trendingScore" if kind == "models" else "likes",
            "direction": "-1",
        }
        url = endpoint + "?" + urllib.parse.urlencode(params)
        try:
            payload = json.loads(http_bytes(url, headers=headers, method="GET").decode("utf-8"))
        except Exception as exc:
            errors.append(f"huggingface:{kind}: {exc}")
            continue
        if not isinstance(payload, list):
            errors.append(f"huggingface:{kind}: unexpected payload")
            continue
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            repo_id = clean_text(entry.get("id", ""))
            if not repo_id:
                continue
            created_raw = str(entry.get("createdAt") or entry.get("created_at") or "")
            updated_raw = str(entry.get("lastModified") or entry.get("last_modified") or entry.get("updatedAt") or "")
            created_dt = parse_result_datetime(created_raw)
            updated_dt = parse_result_datetime(updated_raw)
            freshest = updated_dt or created_dt
            if freshest and freshest < cutoff.astimezone(freshest.tzinfo):
                continue
            likes = int(entry.get("likes") or 0)
            if likes < PRODUCT_COMMUNITY_HF_MIN_LIKES:
                continue
            description = clean_text(entry.get("description", ""))
            tags = [clean_text(str(tag or "")) for tag in entry.get("tags", []) or [] if tag]
            downloads = int(entry.get("downloads") or 0)
            pipeline_tag = clean_text(entry.get("pipeline_tag", "") or entry.get("sdk", ""))
            url = f"https://huggingface.co/{repo_id}" if kind == "models" else f"https://huggingface.co/spaces/{repo_id}"
            content_bits = [
                f"Hugging Face 热门{ '模型' if kind == 'models' else 'Space' } {repo_id}",
                f"likes {likes}",
            ]
            if downloads:
                content_bits.append(f"downloads {downloads}")
            if pipeline_tag:
                content_bits.append(f"类型 {pipeline_tag}")
            if description:
                content_bits.append(description)
            if tags:
                content_bits.append("Tags: " + ", ".join(tags[:6]))
            items.append(
                compact_dict(
                    {
                        "title": f"Hugging Face 热门{'模型' if kind == 'models' else 'Space'} {repo_id}",
                        "url": url,
                        "content": "；".join(content_bits),
                        "description": description,
                        "published_date": updated_raw or created_raw,
                        "score": likes,
                        "source_query": label,
                    }
                )
            )

    result = filter_provider_results(items, queries=queries, merged_limit=merged_limit)
    if errors:
        result["errors"] = errors[:6]
    return result


def _x_topic_label(text: str) -> str:
    lowered = clean_text(text).lower()
    if any(token in lowered for token in ("agent", "agents", "workflow")):
        return "AI Agent"
    if any(token in lowered for token in ("code", "coding", "developer")):
        return "AI 编程"
    if any(token in lowered for token in ("open source", "repo", "github")):
        return "开源项目"
    if any(token in lowered for token in ("model", "llm", "reasoning", "multimodal")):
        return "大模型"
    return "AI 动态"


def _normalize_apify_actor_id(actor_id: str) -> str:
    value = clean_text(actor_id).strip()
    if not value:
        return ""
    if "~" in value:
        return value
    if "/" in value:
        owner, name = value.split("/", 1)
        owner = clean_text(owner).strip()
        name = clean_text(name).strip()
        if owner and name:
            return f"{owner}~{name}"
    return value


def fetch_apify_x_watch_batch(
    *,
    api_token: str,
    actor_id: str,
    merged_limit: int = 10,
) -> dict[str, Any]:
    if not api_token or not actor_id:
        return {"web": {"results": []}, "queries": []}

    normalized_actor_id = _normalize_apify_actor_id(actor_id)
    if not normalized_actor_id:
        return {"web": {"results": []}, "queries": []}

    since_date = (now_local() - dt.timedelta(days=2)).date().isoformat()
    handles = [
        clean_text(str(item.get("handle", ""))).lstrip("@")
        for item in PRODUCT_COMMUNITY_EXPERT_WATCH_ACCOUNTS
        if item.get("handle")
    ]
    handles = [handle for handle in handles if handle]
    if not handles:
        return {"web": {"results": []}, "queries": []}

    payload = {
        "searchTerms": [clean_text(f"from:{handle} -filter:retweets") for handle in handles],
        "sort": "Latest",
        "maxItems": PRODUCT_COMMUNITY_APIFY_X_MAX_ITEMS,
    }
    try:
        run_url = (
            f"https://api.apify.com/v2/acts/{urllib.parse.quote(normalized_actor_id, safe='~')}/runs"
            + "?"
            + urllib.parse.urlencode({"token": api_token, "waitForFinish": "120"})
        )
        run_body = json.loads(http_bytes(run_url, payload=payload, method="POST").decode("utf-8"))
        dataset_id = str((run_body.get("data") or {}).get("defaultDatasetId") or "").strip()
        if not dataset_id:
            return {
                "web": {"results": []},
                "queries": handles,
                "errors": ["apify:x-watch: missing defaultDatasetId"],
            }

        items_url = (
            f"https://api.apify.com/v2/datasets/{urllib.parse.quote(dataset_id, safe='')}/items"
            + "?"
            + urllib.parse.urlencode({"token": api_token, "format": "json", "clean": "true"})
        )
        raw = json.loads(http_bytes(items_url).decode("utf-8"))
    except Exception as exc:
        return {
            "web": {"results": []},
            "queries": handles,
            "errors": [f"apify:x-watch: {exc}"],
        }

    items: list[dict[str, Any]] = []
    placeholder_count = 0
    if not isinstance(raw, list):
        return {
            "web": {"results": []},
            "queries": handles,
            "errors": ["apify:x-watch: unexpected payload"],
        }

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("noResults") is True:
            placeholder_count += 1
            continue
        text = clean_text(
            entry.get("full_text")
            or entry.get("fullText")
            or entry.get("text")
            or entry.get("body")
            or entry.get("tweetText")
            or ""
        )
        if not text:
            continue
        author = entry.get("author") or {}
        handle = clean_text(
            (author.get("userName") if isinstance(author, dict) else "")
            or entry.get("author_username")
            or entry.get("authorUsername")
            or entry.get("screen_name")
            or entry.get("userName")
            or ""
        ).lstrip("@")
        display_name = clean_text(
            (author.get("name") if isinstance(author, dict) else "")
            or entry.get("authorName")
            or entry.get("name")
            or handle
        )
        topic = _x_topic_label(text)
        like_count = int(entry.get("favorite_count") or entry.get("likeCount") or entry.get("likes") or 0)
        reply_count = int(entry.get("replyCount") or entry.get("reply_count") or entry.get("replies") or 0)
        post_url = clean_text(entry.get("url", "") or entry.get("twitterUrl", ""))
        if not post_url and handle:
            tweet_id = clean_text(str(entry.get("id") or entry.get("tweetId") or ""))
            if tweet_id:
                post_url = f"https://x.com/{handle}/status/{tweet_id}"
        published_date = clean_text(entry.get("createdAt") or entry.get("created_at") or "")
        published_dt = search_item_datetime({"published_date": published_date}) if published_date else None
        if published_dt and published_dt.date().isoformat() < since_date:
            continue
        content_bits = [text]
        if like_count:
            content_bits.append(f"likes {like_count}")
        if reply_count:
            content_bits.append(f"replies {reply_count}")
        items.append(
            compact_dict(
                {
                    "title": f"{display_name or handle} 在 X 讨论{topic}",
                    "url": post_url,
                    "content": "；".join(content_bits),
                    "description": text[:220],
                    "published_date": published_date,
                    "score": like_count,
                    "source_query": f"x apify watch {handle} ai",
                }
            )
        )

    result = filter_provider_results(items, queries=handles, merged_limit=merged_limit)
    if not items and placeholder_count:
        result["errors"] = [f"apify:x-watch: actor returned {placeholder_count} noResults placeholders"]
    return result


def merge_brave_provider_payloads(*payloads: dict[str, Any], merged_limit: int = 18) -> dict[str, Any]:
    merged_results: list[dict[str, Any]] = []
    queries: list[str] = []
    errors: list[str] = []
    filtered_out: list[dict[str, str]] = []
    for payload in payloads:
        if not payload:
            continue
        for query in payload.get("queries", []):
            query = clean_text(query)
            if query and query not in queries:
                queries.append(query)
        errors.extend(payload.get("errors", []) or [])
        filtered_out.extend(payload.get("filtered_out", []) or [])
        merged_results.extend(payload.get("web", {}).get("results", []) or payload.get("results", []) or [])
    result = {
        "web": {
            "results": dedupe_search_items(merged_results, limit=merged_limit),
        },
        "queries": queries,
    }
    if errors:
        result["errors"] = errors[:8]
    if filtered_out:
        result["filtered_out"] = filtered_out[:20]
        result["filtered_counts"] = summarize_filtered_reasons(filtered_out)
    return result


def merge_tavily_provider_payloads(*payloads: dict[str, Any], merged_limit: int = 18) -> dict[str, Any]:
    merged_results: list[dict[str, Any]] = []
    answers: list[str] = []
    queries: list[str] = []
    filtered_out: list[dict[str, str]] = []
    for payload in payloads:
        if not payload:
            continue
        answer = clean_text(payload.get("tavily_answer", "") or payload.get("answer", ""))
        if answer:
            answers.append(answer)
        for query in payload.get("queries", []):
            query = clean_text(query)
            if query and query not in queries:
                queries.append(query)
        merged_results.extend(payload.get("tavily_results", []) or payload.get("results", []) or [])
        filtered_out.extend(payload.get("filtered_out", []) or [])
    result = {
        "generated_at": now_local().isoformat(),
        "tavily_answer": "\n".join(answers[:8]),
        "tavily_results": dedupe_search_items(merged_results, limit=merged_limit),
        "queries": queries,
    }
    if filtered_out:
        result["filtered_out"] = filtered_out[:20]
        result["filtered_counts"] = summarize_filtered_reasons(filtered_out)
    return result


def extract_dynamic_query_terms(bucket_payload: dict[str, Any]) -> list[str]:
    scores: dict[str, int] = {}
    casing: dict[str, str] = {}
    items = bucket_result_items(bucket_payload)[:24]
    answers = clean_text(bucket_payload.get("tavily_answer", ""))
    answer_lower = answers.lower()

    for phrase in DYNAMIC_ENTITY_ALLOWLIST:
        lowered_phrase = phrase.lower()
        if lowered_phrase in answer_lower:
            scores[lowered_phrase] = scores.get(lowered_phrase, 0) + 6
            casing[lowered_phrase] = phrase

    for item in items:
        title = clean_text(item.get("title", ""))
        content = clean_text(item.get("content", "") or item.get("description", ""))
        url = item.get("url", "")
        if not title or not is_relevant_ai_result(title, content, url, item=item):
            continue
        if is_macro_context_result(title, content, url, item=item) and count_ai_signals(f"{title} {content}") < 2:
            continue

        title_head = re.split(r"\s+(?:[-|—:])\s+", title, maxsplit=1)[0].strip()
        phrases = re.findall(
            r"[A-Z][A-Za-z0-9.+/-]*(?:\s+[A-Z][A-Za-z0-9.+/-]*){0,2}",
            title_head,
        )
        item_score = max(source_quality_score(url), 1) + count_ai_signals(f"{title} {content}")
        for phrase in phrases:
            candidate = clean_text(phrase).strip(" .,:;|")
            lowered = candidate.lower()
            if not candidate or len(candidate) < 3:
                continue
            if lowered in DYNAMIC_TERM_STOPWORDS:
                continue
            if all(part.lower() in DYNAMIC_TERM_STOPWORDS for part in lowered.split()):
                continue
            scores[lowered] = scores.get(lowered, 0) + item_score
            casing[lowered] = candidate

    ranked_terms = sorted(
        scores.items(),
        key=lambda item: (item[1], len(item[0])),
        reverse=True,
    )
    terms: list[str] = []
    for lowered, _score in ranked_terms:
        term = casing.get(lowered, lowered)
        if term in terms:
            continue
        if any(lowered in existing.lower() or existing.lower() in lowered for existing in terms):
            continue
        terms.append(term)
        if len(terms) >= 6:
            break
    return terms


def build_dynamic_tavily_queries(bucket_payload: dict[str, Any], *, bucket_name: str) -> list[str]:
    month_year = current_query_month_year()
    terms = extract_dynamic_query_terms(bucket_payload)
    if not terms:
        return []

    queries: list[str] = []
    chunk_size = 2 if len(terms) > 3 else 3
    for start in range(0, min(len(terms), 6), chunk_size):
        chunk = terms[start:start + chunk_size]
        if not chunk:
            continue
        joined_terms = " ".join(chunk)
        if bucket_name == "product_hunt":
            query = f"latest {joined_terms} AI product launch Product Hunt GitHub workflow news today {month_year}"
        else:
            query = f"latest {joined_terms} AI launch release infrastructure model agent news today {month_year}"
        queries.append(clean_text(query))
        if len(queries) >= 3:
            break
    return queries


def openai_response(prompt: str, data: dict[str, Any], *, model: str = "gpt-5-mini") -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY。")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": prompt}]},
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
            },
        ],
    }
    response = http_json(
        "https://api.openai.com/v1/responses",
        payload=payload,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    chunks: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                chunks.append(content.get("text", ""))
    return "".join(chunks).strip()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def sanitize_search_text(value: str, *, url: str = "") -> str:
    text = str(value or "").replace("\xa0", " ")
    if not text:
        return ""

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[\]\([^)]*\)", " ", text)
    text = re.sub(r"Image\s+\d+:[^\n]*", " ", text, flags=re.I)
    text = re.sub(r"Image\s+\d+(?:\s+Image\s+\d+)+", " ", text, flags=re.I)
    text = re.sub(r"\bopens new tab\b", " ", text, flags=re.I)
    text = re.sub(r"[ \t]+", " ", text)

    lowered = text.lower()
    cutoff = len(text)
    for marker in CONTENT_HARD_STOP_MARKERS:
        idx = lowered.find(marker)
        if idx >= 120:
            cutoff = min(cutoff, idx)
    text = text[:cutoff]

    kept: list[str] = []
    seen_lines: set[str] = set()
    for raw_line in text.splitlines():
        line = clean_text(raw_line)
        if len(line) < 3:
            continue
        lowered_line = line.lower()
        if lowered_line in seen_lines:
            continue
        if any(re.match(pattern, lowered_line) for pattern in CONTENT_SKIP_LINE_PATTERNS):
            continue
        if lowered_line.startswith("©") or lowered_line.startswith("copyright"):
            continue
        if lowered_line.count("image ") >= 2:
            continue
        if "[]" in line or line.count("|") >= 4:
            continue
        seen_lines.add(lowered_line)
        kept.append(line)

    cleaned = clean_text(" ".join(kept))
    if not cleaned:
        return ""

    if sum(marker in cleaned.lower() for marker in LOW_VALUE_CONTENT_MARKERS) >= 3 and len(cleaned) < 220:
        return ""

    return cleaned[:900].rstrip()


def sanitize_search_item(item: dict[str, Any]) -> dict[str, Any]:
    news_item = NewsItem.from_mapping(item)
    news_item.title = sanitize_search_text(news_item.title, url=news_item.url)
    news_item.content = sanitize_search_text(news_item.content or news_item.description, url=news_item.url)
    if "description" in news_item.extra or news_item.description:
        news_item.description = sanitize_search_text(news_item.description, url=news_item.url)
    if news_item.extra_snippets:
        snippets = [
            sanitize_search_text(str(value or ""), url=news_item.url)
            for value in news_item.extra_snippets
        ]
        news_item.extra_snippets = [snippet for snippet in snippets if snippet][:4]
    return news_item.to_dict()


def ordered_title_tokens(title: str) -> list[str]:
    normalized = normalize_title_key(title)
    raw_tokens = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", normalized)
    ordered: list[str] = []
    for token in raw_tokens:
        if token.isascii():
            token = token.rstrip("s")
        if len(token) <= 2 or token in TITLE_STOPWORDS:
            continue
        ordered.append(token)
    return ordered


def primary_topic_token(title: str) -> str:
    for token in ordered_title_tokens(title):
        if token not in GENERIC_TOPIC_TOKENS:
            return token
    return ""


def classify_item_sector(title: str, content: str) -> str:
    title_lower = title.lower()
    content_lower = content.lower()
    scores = {sector: 0 for sector in SECTOR_PRIORITY}
    for sector, keywords in SECTOR_KEYWORDS.items():
        for keyword in keywords:
            if keyword in title_lower:
                scores[sector] += 3
            elif keyword in content_lower:
                scores[sector] += 1
    if scores["supply_chain"] >= 3:
        return "supply_chain"
    if scores["agent_app"] >= 3 and scores["agent_app"] >= scores["model"]:
        return "agent_app"
    if scores["model"] >= 3:
        return "model"
    best_sector = max(scores, key=scores.get)
    return best_sector if scores[best_sector] > 0 else "other"


def item_anchor_token(title: str, content: str) -> str:
    title_lower = title.lower()
    for keyword in ANCHOR_KEYWORDS:
        if keyword in title_lower:
            return keyword
    content_lower = content.lower()
    for keyword in ANCHOR_KEYWORDS:
        if keyword in content_lower:
            return keyword
    return primary_topic_token(title)


def editorial_title_score(title: str) -> int:
    lowered = title.lower().strip()
    score = 0
    if lowered.startswith(LOW_SIGNAL_TITLE_PREFIXES):
        score -= 2
    if any(keyword in lowered for keyword in LOW_SIGNAL_TITLE_KEYWORDS):
        score -= 2
    if any(keyword in lowered for keyword in HIGH_SIGNAL_TITLE_KEYWORDS):
        score += 2
    if ":" in title:
        score += 1
    return score


def source_quality_score(url: str) -> int:
    host = urllib.parse.urlsplit(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, score in HIGH_QUALITY_DOMAINS.items():
        if host == domain or host.endswith(f".{domain}"):
            return score
    for domain, score in LOW_QUALITY_DOMAINS.items():
        if host == domain or host.endswith(f".{domain}"):
            return score
    return 0


def host_for_url(url: str) -> str:
    host = urllib.parse.urlsplit(url or "").netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def item_feed_category(item: dict[str, Any] | None = None) -> str:
    item = item or {}
    return clean_text(str(item.get("feed_category", "") or "")).lower()


def count_ai_signals(text: str) -> int:
    lowered = clean_text(text).lower()
    return sum(1 for pattern in AI_SIGNAL_PATTERNS if re.search(pattern, lowered))


def keyword_match_count(text: str, keywords: set[str]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def broad_tech_story_profile(title: str, content: str, url: str) -> dict[str, Any]:
    title_lower = clean_text(title).lower()
    content_lower = clean_text(content).lower()
    combined_lower = clean_text(f"{title} {content}").lower()
    quality_score = source_quality_score(url)
    sector = classify_item_sector(title_lower, content_lower)
    ai_signal_count = count_ai_signals(combined_lower)
    company_hits = keyword_match_count(combined_lower, BROAD_TECH_COMPANY_KEYWORDS)
    policy_hits = keyword_match_count(combined_lower, BROAD_TECH_POLICY_KEYWORDS)
    workforce_hits = keyword_match_count(combined_lower, BROAD_TECH_WORKFORCE_KEYWORDS)
    venture_hits = keyword_match_count(combined_lower, BROAD_TECH_VENTURE_KEYWORDS)
    business_hits = keyword_match_count(combined_lower, BROAD_TECH_BUSINESS_KEYWORDS)
    explicit_policy_story = any(
        keyword in combined_lower for keyword in ("digital markets act", "digital services act", "ai act")
    )

    is_broad_tech = False
    if ai_signal_count >= 1 and (business_hits >= 1 or workforce_hits >= 1 or policy_hits >= 1 or venture_hits >= 1):
        is_broad_tech = True
    elif company_hits >= 1 and (
        business_hits >= 1
        or workforce_hits >= 1
        or policy_hits >= 1
        or sector in {"supply_chain", "compute", "model", "agent_app", "open_source", "research"}
    ):
        is_broad_tech = True
    elif quality_score >= 4 and venture_hits >= 2:
        is_broad_tech = True
    elif quality_score >= 4 and policy_hits >= 2 and (company_hits >= 1 or explicit_policy_story):
        is_broad_tech = True
    elif quality_score >= 4 and sector in {"supply_chain", "compute"} and (business_hits >= 1 or policy_hits >= 1):
        is_broad_tech = True

    category = "tech"
    if is_broad_tech and (policy_hits >= 2 or workforce_hits >= 1 or venture_hits >= 1 or business_hits >= 1):
        category = "business"

    return {
        "is_broad_tech": is_broad_tech,
        "category": category,
        "quality_score": quality_score,
    }


def summarize_filtered_reasons(items: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        reason = clean_text(item.get("reason", ""))
        if not reason:
            continue
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _host_matches(url: str, domains: set[str]) -> bool:
    host = host_for_url(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def is_macro_context_source(url: str, item: dict[str, Any] | None = None) -> bool:
    if _host_matches(url, MACRO_CONTEXT_SOURCE_DOMAINS):
        return True
    return item_feed_category(item) in MACRO_CONTEXT_FEED_CATEGORIES and source_quality_score(url) >= 4


def is_major_macro_story(title: str, content: str) -> bool:
    title_lower = clean_text(title).lower()
    combined_lower = clean_text(f"{title} {content}").lower()
    strong_hits = sum(1 for keyword in MACRO_CONTEXT_STRONG_KEYWORDS if keyword in combined_lower)
    broad_hits = sum(1 for keyword in MACRO_CONTEXT_KEYWORDS if keyword in combined_lower)
    title_hits = sum(1 for keyword in MACRO_CONTEXT_KEYWORDS if keyword in title_lower)
    return strong_hits >= 1 or title_hits >= 1 or broad_hits >= 2


def is_macro_context_result(title: str, content: str, url: str, item: dict[str, Any] | None = None) -> bool:
    return is_macro_context_source(url, item=item) and is_major_macro_story(title, content)


def macro_context_bonus_score(title: str, content: str, url: str, item: dict[str, Any] | None = None) -> int:
    if not is_macro_context_result(title, content, url, item=item):
        return 0
    signal_count = count_ai_signals(f"{title} {content}")
    if signal_count == 0:
        return 8
    if signal_count < 2:
        return 4
    return 0


def is_gaming_review_noise(combined_lower: str, url_lower: str) -> bool:
    gaming_hits = sum(1 for keyword in GAMING_CONTEXT_KEYWORDS if keyword in combined_lower or keyword in url_lower)
    review_hits = sum(1 for keyword in GAMING_REVIEW_MARKERS if keyword in combined_lower or keyword in url_lower)
    return gaming_hits >= 1 and review_hits >= 2


def is_doc_like_page(title_lower: str, combined_lower: str, url_lower: str) -> bool:
    if any(keyword in title_lower or keyword in combined_lower for keyword in DOC_PAGE_TITLE_KEYWORDS):
        return True
    if any(part in url_lower for part in DOC_PAGE_URL_PARTS):
        return not any(keyword in title_lower for keyword in HIGH_SIGNAL_TITLE_KEYWORDS)
    return False


def is_low_value_marketing_page(
    title_lower: str,
    combined_lower: str,
    url: str,
    item: dict[str, Any] | None = None,
) -> bool:
    item = item or {}
    host = host_for_url(url)
    path = urllib.parse.urlsplit(url or "").path.lower()
    question = clean_text(str(item.get("question") or "")).lower()
    answer = clean_text(str(item.get("answer") or "")).lower()

    if host in LOW_VALUE_SUPPORT_DOMAINS:
        return True
    if host == "github.com" and path.startswith("/features/"):
        return True
    if host == "producthunt.com" and "/categories/" in path:
        return True
    if any(part in path for part in LOW_VALUE_MARKETING_URL_PARTS) and host in {"github.com"}:
        return True
    if "your ai pair programmer" in title_lower:
        return True
    if host == "producthunt.com" and title_lower.startswith(("the best ", "best ")):
        return True
    if question and answer and any(question.startswith(prefix) for prefix in LOW_VALUE_FAQ_PREFIXES):
        return True
    if any(marker in combined_lower for marker in ("free users are limited to", "which plan includes", "managed by your github administrator")):
        return True
    return False


def is_low_value_roundup_page(
    title_lower: str,
    combined_lower: str,
    url: str,
    item: dict[str, Any] | None = None,
) -> bool:
    item = item or {}
    subtype = clean_text(str(item.get("subtype") or "")).lower()
    host = host_for_url(url)
    generic_title_markers = (
        "daily ai agent news",
        "last 7 days",
        "this week",
        ": 2026 guide",
        " guide",
    )
    if "artificial intelligence automation agency" in title_lower:
        return True
    if any(marker in title_lower for marker in generic_title_markers):
        if subtype in {"faq", "generic"}:
            return True
        if host in {"aiagentstore.ai", "ai.exoticaitsolutions.com"}:
            return True
    if host in {"aiagentstore.ai", "ai.exoticaitsolutions.com"} and subtype in {"faq", "generic"}:
        return True
    if host in {"openpr.com", "markets.financialcontent.com"} and "workflow platform" in combined_lower:
        return True
    return False


def is_undated_official_announcement(
    title_lower: str,
    url: str,
    item: dict[str, Any] | None = None,
) -> bool:
    item = item or {}
    if search_item_datetime(item):
        return False
    if not any(title_lower.startswith(prefix) for prefix in UNDATED_OFFICIAL_TITLE_PREFIXES):
        return False
    host = host_for_url(url)
    if host not in OFFICIAL_ANNOUNCEMENT_HOSTS:
        return False
    path = urllib.parse.urlsplit(url or "").path.lower()
    if not any(part in path for part in ("/news/", "/index/", "/blog/")):
        return False
    return any(keyword in title_lower for keyword in FAST_MOVING_NEWS_KEYWORDS)


def item_max_age_days(title: str, content: str, url: str, source_query: str) -> int:
    haystack = clean_text(" ".join(part for part in (title, content, url, source_query) if part)).lower()
    if any(keyword in haystack for keyword in FAST_MOVING_NEWS_KEYWORDS):
        return SEARCH_MAX_FAST_MOVING_ITEM_AGE_DAYS
    return SEARCH_MAX_ITEM_AGE_DAYS


def source_query_demands_freshness(source_query: str) -> bool:
    lowered = clean_text(source_query).lower()
    if not lowered:
        return False
    if any(marker in lowered for marker in FRESHNESS_QUERY_MARKERS):
        return True
    return bool(re.search(r"\b20\d{2}\b", lowered) and any(month in lowered for month in FRESHNESS_QUERY_MONTH_MARKERS))


def is_undated_time_sensitive_announcement(
    title_lower: str,
    combined_lower: str,
    url: str,
    source_query: str,
    *,
    item: dict[str, Any] | None = None,
) -> bool:
    item = item or {}
    if search_item_datetime(item):
        return False
    if not source_query_demands_freshness(source_query):
        return False
    host = host_for_url(url)
    if host not in OFFICIAL_ANNOUNCEMENT_HOSTS:
        return False
    path = urllib.parse.urlsplit(url or "").path.lower()
    if not any(part in path for part in ("/blog/", "/news/", "/release", "/releases", "/updates", "/announcement", "/announcements", "/index/")):
        return False
    haystack = " ".join(part for part in (title_lower, combined_lower, path) if part)
    return any(keyword in haystack for keyword in FAST_MOVING_NEWS_KEYWORDS)


def stale_item_reason(
    title: str,
    content: str,
    url: str,
    source_query: str,
    item: dict[str, Any] | None = None,
) -> str:
    item = item or {}
    published = search_item_datetime(item)
    if not published:
        return ""
    age_days = max((dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 86400.0, 0.0)
    if age_days > item_max_age_days(title, content, url, source_query):
        return "stale_news"
    return ""


def query_term_in_haystack(term: str, haystack: str) -> bool:
    if " " not in term and re.fullmatch(r"[a-z0-9.+/-]+", term):
        pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
        return re.search(pattern, haystack) is not None
    return term in haystack


def is_generic_repo_homepage(url: str, source_query: str = "") -> bool:
    host = host_for_url(url)
    if host != "github.com":
        return False
    path = urllib.parse.urlsplit(url or "").path.rstrip("/").lower()
    if not re.fullmatch(r"/[^/]+/[^/]+", path):
        return False
    lowered_query = clean_text(source_query).lower()
    if any(token in lowered_query for token in ("release", "releases", "trending", "stars", "star")):
        return False
    return True


def source_query_match_terms(source_query: str) -> list[str]:
    cleaned = clean_text(source_query).lower()
    if not cleaned:
        return []

    phrases: list[str] = []
    for phrase in sorted(DYNAMIC_ENTITY_ALLOWLIST, key=len, reverse=True):
        lowered_phrase = phrase.lower()
        if lowered_phrase in cleaned and lowered_phrase not in phrases:
            phrases.append(lowered_phrase)

    terms: list[str] = []
    for token in re.findall(r"[a-z0-9.+/-]+", cleaned):
        token = token.strip(" .,:;/+-")
        if len(token) < 3 or token.isdigit():
            continue
        if token in SOURCE_QUERY_MATCH_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)

    merged_terms = phrases + [term for term in terms if term not in phrases]
    return merged_terms[:8]


def matches_source_query_context(title: str, content: str, url: str, source_query: str) -> bool:
    terms = source_query_match_terms(source_query)
    if len(terms) < 2:
        return True
    parsed_url = urllib.parse.urlsplit(url or "")
    haystack = clean_text(
        " ".join(
            part
            for part in (
                title,
                content,
                parsed_url.netloc,
                parsed_url.path.replace("/", " "),
            )
            if part
        )
    ).lower()
    matched_terms = {term for term in terms if query_term_in_haystack(term, haystack)}
    required_matches = 2 if len(terms) >= 6 else 1
    return len(matched_terms) >= required_matches


def should_run_llm_relevance_gate(
    *,
    title: str,
    content: str,
    url: str,
    source_query: str,
    signal_count: int,
    quality_score: int,
) -> bool:
    if not AI_NEWS_LLM_FILTER_ENABLED:
        return False
    if signal_count < AI_NEWS_LLM_FILTER_MIN_SIGNAL or signal_count > AI_NEWS_LLM_FILTER_MAX_SIGNAL:
        return False
    if broad_tech_story_profile(title, content, url)["is_broad_tech"]:
        return False
    host = host_for_url(url)
    if quality_score >= 7:
        return False
    if host in OFFICIAL_ANNOUNCEMENT_HOSTS or host in HIGH_QUALITY_DOMAINS:
        return False
    title_lower = clean_text(title).lower()
    content_lower = clean_text(content).lower()
    query_lower = clean_text(source_query).lower()
    ambiguous_markers = (
        "guide",
        "this week",
        "last 7 days",
        "best ",
        "review",
        "platform",
        "agency",
        "watch",
        "how to",
        "vs ",
    )
    if any(marker in title_lower for marker in ambiguous_markers):
        return True
    if "github.com" in url.lower() and "/releases" not in url.lower():
        return True
    if any(marker in content_lower for marker in ("faq", "help center", "pricing", "support")):
        return True
    if query_lower and not matches_source_query_context(title, content, url, source_query):
        return True
    return quality_score <= 1 and signal_count <= 3


def llm_relevance_gate_passes(
    *,
    title: str,
    content: str,
    url: str,
    source_query: str,
) -> bool:
    cache_key = normalize_title_key(title) + "|" + re.sub(r"#.*$", "", (url or "").strip().lower())
    if cache_key in _AI_RELEVANCE_LLM_CACHE:
        return _AI_RELEVANCE_LLM_CACHE[cache_key]
    verdict = _llm_relevance_verdict(title=title, content=content, url=url, source_query=source_query)
    _AI_RELEVANCE_LLM_CACHE[cache_key] = verdict
    return verdict


def search_item_rejection_reason(
    title: str,
    content: str,
    url: str,
    *,
    source_query: str = "",
    item: dict[str, Any] | None = None,
) -> str:
    title_clean = clean_text(title)
    content_clean = clean_text(content)
    url_lower = (url or "").lower()
    combined = f"{title_clean} {content_clean}".strip()
    combined_lower = combined.lower()
    signal_count = count_ai_signals(combined)

    if not title_clean and not content_clean:
        return "empty"
    if CYRILLIC_CHAR_RE.search(combined) and signal_count < 3:
        return "cyrillic_noise"
    if _host_matches(url, SEARCH_HARD_REJECT_DOMAINS):
        return "sports_domain"
    if any(part in url_lower for part in SEARCH_HARD_REJECT_URL_PARTS):
        return "irrelevant_path"
    if is_low_value_marketing_page(title_clean.lower(), combined_lower, url, item=item):
        return "marketing_page"
    if is_low_value_roundup_page(title_clean.lower(), combined_lower, url, item=item):
        return "generic_roundup"
    if is_generic_repo_homepage(url, source_query):
        return "repo_homepage"
    if is_doc_like_page(title_clean.lower(), combined_lower, url_lower):
        return "doc_page"
    if is_undated_time_sensitive_announcement(title_clean.lower(), combined_lower, url, source_query, item=item):
        return "undated_time_sensitive"
    quality_score = source_quality_score(url)
    broad_tech_profile = broad_tech_story_profile(title_clean, content_clean, url)
    broad_tech_story = broad_tech_profile["is_broad_tech"]
    if quality_score <= -2 and (signal_count < 4 or editorial_title_score(title_clean) <= 0):
        return "low_quality_source"
    if is_gaming_review_noise(combined_lower, url_lower):
        return "gaming_review"
    if any(keyword in combined_lower or keyword in url_lower for keyword in SEARCH_HARD_REJECT_TEXT_KEYWORDS):
        return "irrelevant_topic"
    if is_undated_official_announcement(title_clean.lower(), url, item=item):
        return "undated_announcement"
    freshness_reason = stale_item_reason(title_clean, content_clean, url, source_query, item=item)
    if freshness_reason:
        return freshness_reason
    if source_query and not matches_source_query_context(title_clean, content_clean, url, source_query):
        # follow-builder 注入的条目不检查 query_mismatch
        is_follow_builder = item.get("_follow_builder", False) if item else False
        # 高质量来源，或至少不是低质源的强 AI/科技业务新闻，不因 query_mismatch 被误杀
        if (
            is_follow_builder
            or (quality_score >= 4 and (signal_count >= 2 or broad_tech_story))
            or (broad_tech_story and quality_score > -2 and signal_count >= 1)
        ):
            pass  # 保留高质量+高AI信号的条目或follow-builder条目
        else:
            return "query_mismatch"
    if sum(marker in combined_lower for marker in SEARCH_GENERIC_PAGE_MARKERS) >= 2 and signal_count < 3:
        return "generic_page"
    if not is_relevant_ai_result(title_clean, content_clean, url, item=item):
        return "non_ai"
    if should_run_llm_relevance_gate(
        title=title_clean,
        content=content_clean,
        url=url,
        source_query=source_query,
        signal_count=signal_count,
        quality_score=quality_score,
    ) and not llm_relevance_gate_passes(
        title=title_clean,
        content=content_clean,
        url=url,
        source_query=source_query,
    ):
        return "llm_non_ai"
    return ""


def relevant_bucket_items(bucket_payload: dict[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    relevant: list[dict[str, Any]] = []
    for item in bucket_result_items(bucket_payload):
        title = clean_text(item.get("title", ""))
        content = clean_text(item.get("content", "") or item.get("description", ""))
        url = item.get("url", "")
        source_query = clean_text(item.get("source_query", ""))
        if search_item_rejection_reason(title, content, url, source_query=source_query, item=item):
            continue
        relevant.append(item)
    return dedupe_search_items(relevant, limit=limit)


def bucket_dynamic_reason(bucket_payload: dict[str, Any], *, bucket_name: str) -> str:
    relevant = relevant_bucket_items(bucket_payload, limit=12)
    min_relevant = 6 if bucket_name == "ai_news" else 4
    unique_hosts = {host_for_url(item.get("url", "")) for item in relevant if item.get("url")}
    if len(relevant) < min_relevant:
        return f"relevant<{min_relevant}"
    if len(unique_hosts) < min(4, min_relevant) and len(relevant) < (min_relevant + 2):
        return "low_source_diversity"
    return ""


def quality_category_for_item(item: dict[str, Any]) -> str:
    title = clean_text(item.get("title", ""))
    content = clean_text(item.get("content", "") or item.get("description", ""))
    url = item.get("url", "")
    source_query = clean_text(item.get("source_query", ""))
    if search_item_rejection_reason(title, content, url, source_query=source_query, item=item):
        return "irrelevant"

    combined = f"{title} {content}".strip()
    lowered = combined.lower()
    if is_macro_context_result(title, content, url, item=item):
        return "business"
    broad_tech_profile = broad_tech_story_profile(title, content, url)
    if broad_tech_profile["is_broad_tech"]:
        return str(broad_tech_profile["category"])
    business_hits = sum(1 for keyword in BUSINESS_SIGNAL_KEYWORDS if keyword in lowered)
    if any(keyword in lowered for keyword in STRONG_BUSINESS_SIGNAL_KEYWORDS) or business_hits >= 2:
        return "business"

    sector = classify_item_sector(title, content)
    if sector in {"agent_app", "open_source"}:
        return "ai_product"
    if sector in {"model", "research"}:
        return "model"
    if sector in {"supply_chain", "compute"}:
        return "tech"
    if count_ai_signals(combined) >= 2:
        return "tech"
    return "irrelevant"


def summarize_search_quality(search_payload: dict[str, Any]) -> dict[str, int]:
    summary = {
        "tech": 0,
        "business": 0,
        "ai_product": 0,
        "model": 0,
        "kept_total": 0,
        "filtered_total": 0,
        "kept_irrelevant": 0,
    }
    for bucket in ("ai_news", "product_hunt"):
        bucket_payload = search_payload.get(bucket, {})
        for item in bucket_result_items(bucket_payload):
            category = quality_category_for_item(item)
            summary["kept_total"] += 1
            if category == "irrelevant":
                summary["kept_irrelevant"] += 1
            else:
                summary[category] += 1
        filtered_counts = bucket_payload.get("filtered_counts", {})
        if isinstance(filtered_counts, dict) and filtered_counts:
            summary["filtered_total"] += sum(int(value or 0) for value in filtered_counts.values())
        else:
            summary["filtered_total"] += len(bucket_payload.get("filtered_out", []) or [])
    summary["relevant_total"] = max(summary["kept_total"] - summary["kept_irrelevant"], 0)
    summary["irrelevant_total"] = summary["kept_irrelevant"] + summary["filtered_total"]
    return summary


def bucket_quality_counts(bucket_payload: dict[str, Any], *, limit: int = 24) -> dict[str, int]:
    counts = {
        "tech": 0,
        "business": 0,
        "ai_product": 0,
        "model": 0,
        "relevant_total": 0,
        "unique_hosts": 0,
    }
    relevant = relevant_bucket_items(bucket_payload, limit=limit)
    counts["relevant_total"] = len(relevant)
    counts["unique_hosts"] = len({host_for_url(item.get("url", "")) for item in relevant if item.get("url")})
    for item in relevant:
        category = quality_category_for_item(item)
        if category in counts:
            counts[category] += 1
    return counts


def select_ai_news_backfill_queries(bucket_payload: dict[str, Any]) -> dict[str, Any]:
    counts = bucket_quality_counts(bucket_payload, limit=24)
    gap_categories: list[str] = []
    for category in AI_NEWS_BACKFILL_PRIORITY:
        target = AI_NEWS_PRIMARY_CATEGORY_TARGETS.get(category, 0)
        if counts.get(category, 0) < target:
            gap_categories.append(category)

    if counts["relevant_total"] < 8 or counts["unique_hosts"] < 5:
        if "diversity" not in gap_categories:
            gap_categories.append("diversity")

    materialized_groups = materialize_query_groups(AI_NEWS_BACKFILL_BRAVE_QUERY_TEMPLATES)
    brave_queries: list[str] = []
    selected_categories: list[str] = []
    template_index = 0
    while len(brave_queries) < MAX_AI_NEWS_BACKFILL_QUERIES:
        added_in_round = False
        for category in gap_categories:
            queries = materialized_groups.get(category, [])
            if template_index >= len(queries):
                continue
            query = clean_text(queries[template_index])
            if not query or query in brave_queries:
                continue
            brave_queries.append(query)
            added_in_round = True
            if category not in selected_categories:
                selected_categories.append(category)
            if len(brave_queries) >= MAX_AI_NEWS_BACKFILL_QUERIES:
                break
        if not added_in_round:
            break
        template_index += 1

    return {
        "counts": counts,
        "gap_categories": selected_categories,
        "brave_queries": brave_queries[:MAX_AI_NEWS_BACKFILL_QUERIES],
    }


def select_product_community_supplement_queries(search_payload: dict[str, Any]) -> dict[str, Any]:
    overall = summarize_search_quality(search_payload)
    product_counts = bucket_quality_counts(search_payload.get("product_hunt", {}), limit=24)
    gap_categories: list[str] = []
    provider_categories: list[str] = []
    low_product_volume = product_counts.get("relevant_total", 0) < max(MIN_PRODUCT_COMMUNITY_RELEVANT_TOTAL, 6)
    low_product_diversity = product_counts.get("unique_hosts", 0) < 6
    missing_product_breadth = product_counts.get("tech", 0) == 0 or product_counts.get("business", 0) == 0

    if (
        overall.get("relevant_total", 0) < MIN_OVERALL_RELEVANT_TOTAL
        or low_product_volume
        or low_product_diversity
        or product_counts.get("ai_product", 0) < 4
    ):
        gap_categories.append("github_rising")
        provider_categories.append("github_newstars_api")
    if (
        overall.get("relevant_total", 0) < MIN_OVERALL_RELEVANT_TOTAL
        or product_counts.get("ai_product", 0) < 4
        or low_product_diversity
    ):
        gap_categories.append("expert_watch")
        provider_categories.append("x_watch_apify")
    if (
        overall.get("relevant_total", 0) < MIN_OVERALL_RELEVANT_TOTAL
        or low_product_volume
        or missing_product_breadth
    ):
        gap_categories.append("project_watch")
        if "hf_trending_api" not in provider_categories:
            provider_categories.append("hf_trending_api")
    if overall.get("relevant_total", 0) < 12 or low_product_volume:
        gap_categories.append("hackathon")
    if overall.get("relevant_total", 0) < 9 or (
        (overall.get("relevant_total", 0) < MIN_OVERALL_RELEVANT_TOTAL or low_product_diversity)
        and product_counts.get("unique_hosts", 0) < 5
    ):
        gap_categories.append("xiaohongshu")

    materialized_groups = build_product_community_supplement_query_groups()
    brave_queries: list[str] = []
    selected_categories: list[str] = []
    for category in PRODUCT_COMMUNITY_SUPPLEMENT_PRIORITY:
        if category not in gap_categories:
            continue
        for query in materialized_groups.get(category, []):
            query = clean_text(query)
            if not query or query in brave_queries:
                continue
            brave_queries.append(query)
            selected_categories.append(category)
            break
        if len(brave_queries) >= MAX_PRODUCT_COMMUNITY_SUPPLEMENT_QUERIES:
            break

    selected_provider_categories: list[str] = []
    for category in PRODUCT_COMMUNITY_EXTERNAL_PROVIDER_PRIORITY:
        if category not in provider_categories or category in selected_provider_categories:
            continue
        selected_provider_categories.append(category)
        if len(selected_provider_categories) >= MAX_PRODUCT_COMMUNITY_EXTERNAL_PROVIDERS:
            break

    return {
        "overall_counts": overall,
        "product_counts": product_counts,
        "gap_categories": selected_categories,
        "brave_queries": brave_queries[:MAX_PRODUCT_COMMUNITY_SUPPLEMENT_QUERIES],
        "provider_categories": selected_provider_categories,
    }


def is_relevant_ai_result(title: str, content: str, url: str, item: dict[str, Any] | None = None) -> bool:
    lowered_title = clean_text(title).lower()
    lowered_content = clean_text(content).lower()
    combined = f"{lowered_title} {lowered_content}".strip()
    url_lower = (url or "").lower()
    host = host_for_url(url)

    title_signal_count = count_ai_signals(lowered_title)
    combined_signal_count = count_ai_signals(combined)
    macro_context_story = is_macro_context_result(title, content, url, item=item)
    broad_tech_profile = broad_tech_story_profile(title, content, url)
    broad_tech_story = broad_tech_profile["is_broad_tech"]

    if CYRILLIC_CHAR_RE.search(f"{title} {content}") and combined_signal_count < 3:
        return False
    if any(keyword in lowered_title for keyword in OBVIOUSLY_IRRELEVANT_TITLE_KEYWORDS):
        return False
    if any(keyword in combined for keyword in SEARCH_HARD_REJECT_TEXT_KEYWORDS):
        return False
    if any(part in url_lower for part in SEARCH_HARD_REJECT_URL_PARTS):
        return False
    if any(marker in lowered_title for marker in LOW_VALUE_CONTENT_MARKERS):
        return False
    if sum(marker in lowered_content for marker in LOW_VALUE_CONTENT_MARKERS) >= 2:
        return False
    if sum(marker in combined for marker in SEARCH_GENERIC_PAGE_MARKERS) >= 2 and combined_signal_count < 3:
        return False
    if (
        title_signal_count == 0
        and source_quality_score(url) <= 0
        and combined_signal_count < 4
        and not macro_context_story
        and not broad_tech_story
    ):
        return False
    if macro_context_story and combined_signal_count < 2:
        return True
    if broad_tech_story:
        return True

    if host in OBVIOUSLY_IRRELEVANT_DOMAINS:
        return title_signal_count >= 1 and combined_signal_count >= 2

    return title_signal_count >= 1 or combined_signal_count >= 2


def extract_item_rank(item: dict[str, Any]) -> int:
    title = clean_text(item.get("title", ""))
    content = clean_text(item.get("content", "") or item.get("description", ""))
    url = item.get("url", "")
    source_query = clean_text(item.get("source_query", ""))
    score = source_quality_score(url) * 10 + editorial_title_score(title) * 4
    score += macro_context_bonus_score(title, content, url, item=item)
    source_episode = str(item.get("source_episode", "") or "").strip()
    source_bucket = str(item.get("source_bucket", "") or "").strip()
    if source_episode:
        score -= 24
        if source_bucket == "product_hunt":
            score -= 8
    published = search_item_datetime(item)
    if published:
        age_days = max((dt.datetime.now(dt.timezone.utc) - published).total_seconds() / 86400.0, 0.0)
        if age_days <= 1.5:
            score += 12
        elif age_days <= 3:
            score += 6
        elif age_days > item_max_age_days(title, content, url, source_query):
            score -= 80
        elif age_days > 14:
            score -= 40
        elif age_days > 7:
            score -= 18
    return score


def extract_items(results: list[dict[str, Any]], limit: int = 6, *, diversify_sectors: bool = False) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    used_topic_sector_pairs: set[tuple[str, str]] = set()
    for result in results:
        title = clean_text(result.get("title", ""))
        content = clean_text(result.get("content", "") or result.get("description", ""))
        url = result.get("url", "")
        source_query = clean_text(result.get("source_query", ""))
        if not title:
            continue
        if len(title) > 120:
            continue
        if not is_relevant_ai_result(title, content, url, item=result):
            continue
        if search_item_rejection_reason(title, content, url, source_query=source_query, item=result):
            continue
        if normalize_title_key(title) in GENERIC_TITLE_KEYS:
            continue
        lowered_title = title.lower()
        if any(keyword in lowered_title for keyword in LOW_SIGNAL_TITLE_KEYWORDS):
            continue
        sector = classify_item_sector(title, content)
        anchor = item_anchor_token(title, content)
        if anchor and (anchor, sector) in used_topic_sector_pairs:
            continue
        rank = extract_item_rank(result)
        if rank < 0:
            continue
        candidates.append(
            {
                "title": title,
                "content": content,
                "url": url,
                "sector": sector,
                "anchor": anchor,
                "published_date": (
                    result.get("published_date")
                    or result.get("publishedAt")
                    or result.get("date")
                    or result.get("page_age")
                    or result.get("age")
                    or ""
                ),
                "source_query": source_query,
                "source_episode": str(result.get("source_episode", "") or "").strip(),
                "source_bucket": str(result.get("source_bucket", "") or "").strip(),
                "feed_category": str(result.get("feed_category", "") or "").strip(),
                "feed_name": str(result.get("feed_name", "") or "").strip(),
                "provider": str(result.get("provider", "") or "").strip(),
                "_rank": rank,
            }
        )
        if anchor:
            used_topic_sector_pairs.add((anchor, sector))
    if not diversify_sectors:
        candidates.sort(
            key=lambda item: (
                int(item.get("_rank", 0)),
                source_quality_score(item["url"]),
                editorial_title_score(item["title"]),
            ),
            reverse=True,
        )
        return candidates[:limit]

    selected: list[dict[str, str]] = []
    used_sectors: set[str] = set()
    candidates.sort(
        key=lambda item: (
            int(item.get("_rank", 0)),
            source_quality_score(item["url"]),
            editorial_title_score(item["title"]),
        ),
        reverse=True,
    )
    for sector in SECTOR_PRIORITY:
        for item in candidates:
            if item["sector"] != sector or sector in used_sectors:
                continue
            selected.append(item)
            used_sectors.add(sector)
            break
        if len(selected) >= limit:
            break
    for item in candidates:
        if item in selected:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def summarize_sector_mix(items: list[dict[str, str]]) -> str:
    period_label = now_local().strftime("%Y年%-m月")
    sectors = [item.get("sector", "other") for item in items]
    if "supply_chain" in sectors and ("model" in sectors or "research" in sectors) and (
        "agent_app" in sectors or "open_source" in sectors
    ):
        return f"{period_label}：AI全产业链重新排位"
    if "research" in sectors and ("model" in sectors or "agent_app" in sectors):
        return f"{period_label}：AI研究突破走向产业化"
    if "model" in sectors and "agent_app" in sectors:
        return f"{period_label}：AI产品到智能体加速重组"
    if "supply_chain" in sectors and "compute" in sectors:
        return f"{period_label}：AI基础设施继续上强度"
    return f"{period_label}：AI关键变化"


def select_trend_items(search_payload: dict[str, Any], limit: int = 3) -> list[dict[str, str]]:
    ai_all = bucket_result_items(search_payload["ai_news"])
    product_all = bucket_result_items(search_payload["product_hunt"])
    ai_candidates = extract_items(ai_all, limit=12, diversify_sectors=True)
    product_candidates = extract_items(product_all, limit=12, diversify_sectors=False)
    selected: list[dict[str, str]] = []

    def already_selected(candidate: dict[str, str]) -> bool:
        return any(
            existing.get("anchor") == candidate.get("anchor") and existing.get("sector") == candidate.get("sector")
            for existing in selected
        )

    for sector in SECTOR_PRIORITY:
        pool_order = (
            (product_candidates, ai_candidates)
            if sector in {"agent_app", "open_source"}
            else (ai_candidates, product_candidates)
        )
        for pool in pool_order:
            match = next(
                (
                    candidate
                    for candidate in pool
                    if candidate.get("sector") == sector and not already_selected(candidate)
                ),
                None,
            )
            if match is not None:
                selected.append(match)
                break
        if len(selected) >= limit:
            return selected

    for pool in (ai_candidates, product_candidates):
        for candidate in pool:
            if already_selected(candidate):
                continue
            selected.append(candidate)
            if len(selected) >= limit:
                return selected
    return selected


def build_fallback_headline(ai_items: list[dict[str, str]], product_items: list[dict[str, str]]) -> str:
    period_label = now_local().strftime("%Y年%-m月")
    ai_titles = [item["title"] for item in ai_items[:3]]
    product_titles = [item["title"] for item in product_items[:2]]
    catalog = "；".join(ai_titles + product_titles)
    sector_mix_headline = summarize_sector_mix(ai_items)
    if sector_mix_headline != f"{period_label}：AI关键变化":
        return sector_mix_headline
    patterns = [
        ("hbm", f"{period_label}：AI算力供应链升温"),
        ("hynix", f"{period_label}：AI算力供应链升温"),
        ("github", f"{period_label}：AI开源生态加速爆发"),
        ("robot", f"{period_label}：AI机器人进入新阶段"),
        ("humanoid", f"{period_label}：AI机器人进入新阶段"),
        ("kimi", f"{period_label}：AI研究突破走向产业化"),
        ("sora", f"{period_label}：AI视频产品急转弯"),
        ("claude", f"{period_label}：AI智能体走向实操"),
        ("optical", f"{period_label}：AI基础设施继续上强度"),
        ("photonic", f"{period_label}：AI基础设施继续上强度"),
        ("openai", f"{period_label}：AI产品进入重组期"),
        ("anthropic", f"{period_label}：AI智能体走向实操"),
        ("nvidia", f"{period_label}：AI推理基础设施升温"),
        ("intel", f"{period_label}：AI算力新变量浮现"),
    ]
    lowered = catalog.lower()
    for needle, headline in patterns:
        if needle in lowered:
            return headline
    return summarize_sector_mix(ai_items)


def fallback_research_markdown(search_payload: dict[str, Any]) -> str:
    ai_items = select_trend_items(search_payload, limit=5)
    ph_all = (search_payload["product_hunt"].get("tavily_results", []) +
              search_payload["product_hunt"].get("brave_results", []))
    ph_items = extract_items(ph_all, limit=6, diversify_sectors=False)
    ai_titles = [item["title"] for item in ai_items[:3]]
    product_titles = [item["title"] for item in ph_items[:3]]
    title = f"# {build_fallback_headline(ai_items, ph_items)}"
    trend_lines = []
    for index, item in enumerate(ai_items[:3], start=1):
        trend_lines.append(
            "\n".join(
                [
                    f"### 趋势{index}：{item['title']}",
                    "",
                    "发生了什么：",
                    item["content"] or "今天这一条新闻反映出 AI 产业链正在继续向前推进。",
                    "",
                    "为什么重要：",
                    "这条变化说明竞争正在从单点模型能力，转向更完整的产品、基础设施和产业配套。",
                    "",
                    "意味着什么：",
                    "对创业者来说，真正的机会不只是追模型热点，而是找到能承接这些变化的场景和工作流。",
                ]
            )
        )
    product_lines = []
    for item in ph_items[:3]:
        product_lines.append(
            "\n".join(
                [
                    f"### {item['title']}",
                    "",
                    f"- 定位：{item['title']}",
                    f"- 亮点：{item['content'] or '它能在当前热潮里被用户主动关注，说明产品表达和场景切入足够直接。'}",
                    "- 局限：Product Hunt 热度能说明关注度，但不一定等于长期留存和商业化能力。",
                    "- 可借鉴点：现在的 AI 产品越来越需要对结果负责，而不是只展示功能。",
                ]
            )
        )
    sources = []
    for item in ai_items + ph_items:
        if item["url"]:
            sources.append(f"- {item['title']}：<{item['url']}>")
    deep_research = textwrap.dedent(
        f"""
        ## 深度研究

        如果把今天这些新闻放在一起看，会发现一个很清晰的方向：AI 行业已经不再满足于“模型又提升了一点”，而是在朝着真正可交付、可规模化、可持续运转的系统演进。

        第一层变化，是新闻热点本身正在从抽象模型能力，转向更具体的产业抓手。今天最值得注意的几条动态，分别落在 { "、".join(ai_titles[:3]) if ai_titles else "模型、产品和基础设施" } 这些方向上。它们看起来分散，但共同点都很明确，就是谁能把 AI 变成一个真正稳定、持续、可放大的生产系统，谁才更接近下一阶段的主导权。

        第二层变化，是 Product Hunt 这类产品榜单越来越像一个应用层风向标。{ "、".join(product_titles[:3]) if product_titles else "这些上榜产品" } 之所以有热度，不只是因为它们沾上了 AI，而是因为它们更像“能帮你完成任务”的产品。这说明用户不再只想试试模型，而是更愿意为明确结果买单。

        第三层变化，是内容行业本身也要跟着升级。你给我的历史脚本其实已经说明了一个很重要的方向：用户不缺新闻条目，缺的是把新闻拼成图景、再把图景翻译成判断的能力。未来 AI 播客最有价值的部分，不是“我比别人更快发出消息”，而是“我能帮用户更快形成理解”。

        所以今天这期内容真正想传达的是：AI 竞争的重点，已经从模型单点突破，转向结果交付、产业协同和现实场景落地。模型很重要，但模型只是引擎。工作流、基础设施和用户真实使用，才是这一轮竞争的胜负手。
        """
    ).strip()
    return "\n\n".join(
        [
            title,
            f"日期：{now_local().strftime('%Y 年 %m 月 %d 日')}",
            "## 今日结论\n\n1. AI 热点正在从单一模型升级，转向产品、基础设施和真实使用场景的联动。\n2. Product Hunt 上的热门产品说明，用户更愿意为结果负责型 AI 产品买单。\n3. 内容生产的核心价值，正在从搬运信息转向提炼判断。",
            "## 三大趋势\n\n" + "\n\n".join(trend_lines),
            "## Product Hunt 产品洞察\n\n" + "\n\n".join(product_lines),
            deep_research,
            "## 信息源\n\n" + "\n".join(sources),
        ]
    )


def episode_date_from_name(name: str) -> dt.date | None:
    match = re.match(r"^(\d{8})E\d+$", name)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s)>]+", text or "")


def canonicalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def normalize_title_key(title: str) -> str:
    head = (title or "").strip().split(" - ", 1)[0]
    head = head.lower().replace("’", "").replace("'", "")
    head = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", head)
    return re.sub(r"\s+", " ", head).strip()


def title_tokens(title: str) -> set[str]:
    normalized = normalize_title_key(title)
    tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", normalized))
    compact: set[str] = set()
    for token in tokens:
        if token.isascii():
            token = token.rstrip("s")
        if len(token) <= 2 or token in TITLE_STOPWORDS:
            continue
        compact.add(token)
    return compact


def title_similarity(left: str, right: str) -> float:
    left_key = normalize_title_key(left)
    right_key = normalize_title_key(right)
    if not left_key or not right_key:
        return 0.0
    if left_key == right_key or left_key in right_key or right_key in left_key:
        return 1.0
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(overlap) / len(union) if union else 0.0


def title_overlap_coverage(left: str, right: str) -> tuple[int, float]:
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0, 0.0
    overlap = left_tokens & right_tokens
    return len(overlap), len(overlap) / min(len(left_tokens), len(right_tokens))


def text_signature_tokens(text: str, *, limit: int = 12) -> set[str]:
    tokens: list[str] = []
    for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", clean_text(text).lower()):
        if token.isascii():
            token = token.rstrip("s")
        if len(token) <= 2 or token in CONTENT_STOPWORDS:
            continue
        tokens.append(token)
    ranked: list[str] = []
    for token in tokens:
        if token not in ranked:
            ranked.append(token)
        if len(ranked) >= limit:
            break
    return set(ranked)


def story_signature_tokens(title: str, body: str = "") -> set[str]:
    return title_tokens(title) | text_signature_tokens(body)


def extract_trend_sections(markdown: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current_title = ""
    current_lines: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("### 趋势"):
            if current_title:
                sections.append({"title": current_title, "body": "\n".join(current_lines).strip()})
            current_title = line.lstrip("# ").strip()
            current_lines = []
            continue
        if current_title and line.startswith("### "):
            sections.append({"title": current_title, "body": "\n".join(current_lines).strip()})
            current_title = ""
            current_lines = []
            continue
        if current_title and (line.startswith("## ") or line.startswith("# ")):
            sections.append({"title": current_title, "body": "\n".join(current_lines).strip()})
            current_title = ""
            current_lines = []
            continue
        if current_title:
            current_lines.append(line)
    if current_title:
        sections.append({"title": current_title, "body": "\n".join(current_lines).strip()})
    return sections


def load_recent_coverage_context(
    *,
    lookback_days: int = RECENT_SIMILARITY_LOOKBACK_DAYS,
    max_episodes: int = RECENT_SIMILARITY_MAX_EPISODES,
    exclude_episode: str = "",
) -> dict[str, Any]:
    today = now_local().date()
    episodes: list[dict[str, Any]] = []
    trend_entries: list[dict[str, Any]] = []
    scan_dirs = [ROOT_DIR, ROOT_DIR / "episodes"]
    for scan_root in scan_dirs:
     for path in (scan_root.iterdir() if scan_root.exists() else []):
        if not path.is_dir():
            continue
        if exclude_episode and path.name == exclude_episode:
            continue
        episode_date = episode_date_from_name(path.name)
        if not episode_date:
            continue
        age_days = (today - episode_date).days
        if age_days < 0 or age_days > lookback_days:
            continue
        record_path = path / "record.json"
        research_path = path / "research.md"
        listenhub_episode_path = path / "listenhub_episode_result.json"
        xiaoyuzhou_result_path = path / "xiaoyuzhou_publish_result.json"
        if not record_path.exists() and not research_path.exists():
            continue
        record = read_json(record_path) if record_path.exists() else {}
        research_markdown = research_path.read_text(encoding="utf-8") if research_path.exists() else ""
        listenhub_episode = read_json(listenhub_episode_path) if listenhub_episode_path.exists() else {}
        xiaoyuzhou_result = read_json(xiaoyuzhou_result_path) if xiaoyuzhou_result_path.exists() else {}
        if not _episode_counts_as_broadcast(
            path,
            record=record,
            xiaoyuzhou_result=xiaoyuzhou_result,
            listenhub_episode=listenhub_episode,
        ):
            continue
        metadata = parse_research_metadata(research_markdown) if research_markdown else {
            "title": "",
            "trend_titles": [],
            "product_titles": [],
        }
        trend_titles = metadata["trend_titles"] or [
            line.strip()
            for line in str(record.get("三大趋势", "")).splitlines()
            if line.strip()
        ][:3]
        product_titles = metadata["product_titles"] or [
            line.strip()
            for line in str(record.get("产品洞察", "")).splitlines()
            if line.strip()
        ][:3]
        title = metadata["title"] or str(
            record.get("标题")
            or xiaoyuzhou_result.get("title")
            or record.get("ListenHub标题")
            or listenhub_episode.get("title")
            or path.name
        )
        source_urls = [canonicalize_url(url) for url in extract_urls(research_markdown)]
        trend_sections = extract_trend_sections(research_markdown)
        if trend_sections:
            for section in trend_sections[:3]:
                trend_entries.append(
                    {
                        "episode": path.name,
                        "date": episode_date.isoformat(),
                        "title": section["title"],
                        "body": section["body"],
                        "signature_tokens": story_signature_tokens(section["title"], section["body"]),
                        "primary_topic": primary_topic_token(section["title"]),
                        "anchor_token": item_anchor_token(section["title"], section["body"]),
                    }
                )
        published_shownotes = str(xiaoyuzhou_result.get("episode", {}).get("shownotes") or xiaoyuzhou_result.get("shownotes") or "").strip()
        if published_shownotes:
            trend_entries.append(
                {
                    "episode": path.name,
                    "date": episode_date.isoformat(),
                    "title": title,
                    "body": published_shownotes,
                    "signature_tokens": story_signature_tokens(title, published_shownotes),
                    "primary_topic": primary_topic_token(title),
                    "anchor_token": item_anchor_token(title, published_shownotes),
                }
            )
        listenhub_outline = str(listenhub_episode.get("outline", "")).strip()
        if listenhub_outline:
            for section in extract_trend_sections(listenhub_outline)[:3]:
                trend_entries.append(
                    {
                        "episode": path.name,
                        "date": episode_date.isoformat(),
                        "title": section["title"],
                        "body": section["body"],
                        "signature_tokens": story_signature_tokens(section["title"], section["body"]),
                        "primary_topic": primary_topic_token(section["title"]),
                        "anchor_token": item_anchor_token(section["title"], section["body"]),
                    }
                )
        episodes.append(
            {
                "episode": path.name,
                "date": episode_date.isoformat(),
                "title": title,
                "trend_titles": trend_titles,
                "product_titles": product_titles,
                "anchor_tokens": {
                    item_anchor_token(item_title, "")
                    for item_title in [title, *trend_titles, *product_titles]
                    if item_title
                },
                "signature_tokens": story_signature_tokens(
                    " ".join([title, *trend_titles]),
                    " ".join(product_titles),
                ),
                "source_urls": [url for url in source_urls if url],
            }
        )

    episodes.sort(key=lambda item: (item["date"], item["episode"]), reverse=True)
    episodes = episodes[:max_episodes]
    allowed_episode_ids = {episode["episode"] for episode in episodes}
    trend_entries = [entry for entry in trend_entries if entry["episode"] in allowed_episode_ids]

    title_candidates: list[str] = []
    source_urls: set[str] = set()
    for episode in episodes:
        title_candidates.append(episode["title"])
        title_candidates.extend(episode["trend_titles"])
        title_candidates.extend(episode["product_titles"])
        source_urls.update(episode["source_urls"])

    title_keys = {normalize_title_key(title) for title in title_candidates if normalize_title_key(title)}
    return {
        "episodes": episodes,
        "titles": [title for title in title_candidates if title],
        "title_keys": title_keys,
        "source_urls": source_urls,
        "trend_entries": trend_entries,
    }


def classify_recent_duplicate(item: dict[str, Any], recent_context: dict[str, Any]) -> str | None:
    url = canonicalize_url(item.get("url", ""))
    title = item.get("title", "")
    title_key = normalize_title_key(title)
    body = clean_text(item.get("content", "") or item.get("description", ""))
    primary_topic = primary_topic_token(title)
    anchor_token = item_anchor_token(title, body)
    signature = story_signature_tokens(title, body)
    if url and url in recent_context["source_urls"]:
        return "same_url"
    if title_key and title_key in recent_context["title_keys"]:
        return "same_title"
    for recent_title in recent_context["titles"]:
        if title_similarity(title, recent_title) >= 0.8:
            return "similar_title"
        overlap_count, overlap_coverage = title_overlap_coverage(title, recent_title)
        if overlap_count >= 3 and overlap_coverage >= 0.5:
            return "same_topic"
    for recent_entry in recent_context.get("trend_entries", []):
        if not signature or not recent_entry.get("signature_tokens"):
            continue
        if primary_topic and recent_entry.get("primary_topic") and primary_topic != recent_entry["primary_topic"]:
            if not (anchor_token and recent_entry.get("anchor_token") == anchor_token):
                continue
        overlap = signature & recent_entry["signature_tokens"]
        if len(overlap) >= 3:
            return "same_story_signature"
        # 原先 overlap>=2 + 同 anchor 即判重，导致 "Meta Muse Spark" 被 "Anthropic Mythos"
        # 误伤（共享 AI/model 这类泛 token + 同 anchor 类型 "ai model"）。
        # 收紧为 overlap>=3，让单纯的泛 token 重叠不再触发杀。
        if anchor_token and recent_entry.get("anchor_token") == anchor_token and len(overlap) >= 3:
            return "same_anchor_signature"
        if anchor_token and recent_entry.get("anchor_token") == anchor_token and str(item.get("source_episode", "")).strip():
            return "same_anchor_backlog"
        if primary_topic and recent_entry.get("primary_topic") == primary_topic:
            non_primary_overlap = {token for token in overlap if token != primary_topic}
            # 同样把 same_topic_progression 的 non_primary_overlap 要求从 1 提到 2，
            # 避免仅因为共享一个非 primary 的通用词就把整条新闻误杀
            if len(non_primary_overlap) >= 2 and len(overlap) >= 2:
                return "same_topic_progression"
    for episode in recent_context.get("episodes", []):
        if anchor_token and anchor_token in episode.get("anchor_tokens", set()):
            overlap = signature & episode.get("signature_tokens", set())
            if len(overlap) >= 2:
                return "recent_episode_repeat"
    return None


def filter_recently_covered_search_payload(
    search_payload: dict[str, Any], recent_context: dict[str, Any]
) -> dict[str, Any]:
    filtered = {
        "generated_at": search_payload.get("generated_at", now_local().isoformat()),
        "ai_news": dict(search_payload.get("ai_news", {})),
        "product_hunt": dict(search_payload.get("product_hunt", {})),
    }
    # 透传其余 top-level 键（funnel_stats / b2_revival / b3_backlog / hard_*_filter
    # / _time_cutoff_pool / _history_block_pool / quality_summary 等），
    # 否则下游会丢失观测/复活上下文
    for k, v in search_payload.items():
        if k not in filtered:
            filtered[k] = v
    removed_items: list[dict[str, str]] = []

    for bucket in ("ai_news", "product_hunt"):
        bucket_payload = dict(filtered.get(bucket, {}))
        for result_key in ("tavily_results", "brave_results"):
            results = list(bucket_payload.get(result_key, []))
            kept_results = []
            for item in results:
                duplicate_reason = classify_recent_duplicate(item, recent_context)
                if duplicate_reason:
                    removed_items.append(
                        {
                            "bucket": bucket,
                            "reason": duplicate_reason,
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                        }
                    )
                    continue
                kept_results.append(item)
            bucket_payload[result_key] = kept_results
        filtered[bucket] = bucket_payload

    filtered["history_filter"] = {
        "lookback_days": RECENT_SIMILARITY_LOOKBACK_DAYS,
        "recent_episode_count": len(recent_context["episodes"]),
        "removed_items": removed_items,
    }
    return filtered


# ═══════════════════════════════════════════════════════════════════════════
# Hard Blocks —— 在数据清洗阶段引入绝对的硬性拦截
#   1. 强制时间截断 (Hard Time Cutoff): >36h 或写明 N 天前的新闻一律丢弃
#   2. 强制历史拦截 (Hard History Block): 命中前 N 期脚本中实体的候选直接丢弃
#   3. 缺省降级预案 (Fallback Strategy): 候选不足 3 条时在 LLM prompt 注明
# ═══════════════════════════════════════════════════════════════════════════

# ANSI 颜色代码 —— 让拦截日志在终端高亮
_WARN_COLOR = "\033[1;33m"  # 粗体黄色
_BLOCK_COLOR = "\033[1;31m"  # 粗体红色
_RESET_COLOR = "\033[0m"

# 默认阈值可通过环境变量覆盖
HARD_TIME_CUTOFF_HOURS = float(os.environ.get("SOLO_HARD_TIME_CUTOFF_HOURS", "36"))
HARD_HISTORY_BLOCK_LAST_N = int(os.environ.get("SOLO_HARD_HISTORY_BLOCK_LAST_N", "2"))

# 历史实体拦截的停用词 —— 这些词太常见，不能用它们做拦截 key，
# 否则整个候选池会被清空
_HARD_BLOCK_STOPWORDS: set[str] = {
    # 通用技术词
    "ai", "api", "sdk", "llm", "gpt", "chatgpt", "model", "models",
    "ceo", "cto", "app", "web", "data", "code", "news", "today",
    "update", "release", "launch", "build", "tech", "open", "source",
    # 超高频公司名 —— 这些几乎天天出现，不适合做单 key 拦截
    "openai", "google", "meta", "apple", "microsoft", "amazon",
    "nvidia", "tesla", "ibm", "intel", "samsung",
    # 时间/地理通用词
    "china", "chinese", "china's", "chinese's", "global", "world",
    "today", "week", "month", "year", "daily", "morning",
}
_HARD_BLOCK_STOPWORDS |= {w.capitalize() for w in list(_HARD_BLOCK_STOPWORDS)}

# 中文日期相关正则 —— 用于从标题/正文中探测明确的事件时间
_CHINESE_DATE_RE = re.compile(
    r"(?P<year>\d{4})?\s*年?\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"
)
_CHINESE_RELATIVE_RE = re.compile(
    r"(?P<count>[一二两三四五六七八九十\d]+)\s*(?P<unit>天|日|小时|周|星期|个月|月)前"
)
_CN_DIGIT_MAP = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_chinese_relative(text: str) -> dt.datetime | None:
    """解析「3 天前」「两周前」这类中文相对时间。"""
    match = _CHINESE_RELATIVE_RE.search(text or "")
    if not match:
        return None
    count_raw = match.group("count")
    try:
        count = int(count_raw)
    except ValueError:
        count = _CN_DIGIT_MAP.get(count_raw, 0)
    if count <= 0:
        return None
    unit = match.group("unit")
    unit_map = {
        "天": dt.timedelta(days=count),
        "日": dt.timedelta(days=count),
        "小时": dt.timedelta(hours=count),
        "周": dt.timedelta(weeks=count),
        "星期": dt.timedelta(weeks=count),
        "个月": dt.timedelta(days=30 * count),
        "月": dt.timedelta(days=30 * count),
    }
    delta = unit_map.get(unit)
    if delta is None:
        return None
    return dt.datetime.now(dt.timezone.utc) - delta


def _parse_chinese_absolute(text: str) -> dt.datetime | None:
    """解析「2026年4月1日」「4月3日」这类中文绝对日期，若缺年则假定今年。"""
    match = _CHINESE_DATE_RE.search(text or "")
    if not match:
        return None
    try:
        month = int(match.group("month"))
        day = int(match.group("day"))
    except (TypeError, ValueError):
        return None
    year_str = match.group("year")
    if year_str:
        try:
            year = int(year_str)
        except ValueError:
            return None
    else:
        year = now_local().year
    try:
        return dt.datetime(year, month, day, tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _detect_item_event_time(item: dict[str, Any]) -> tuple[dt.datetime | None, str]:
    """综合判断一条新闻的实际事件时间。

    返回 (datetime, 来源描述)。优先级：
      1. published_date / publishedAt / date / page_age 等结构化字段
      2. 标题 + 摘要 + 正文中写明的日期（中英文）
    """
    # 1) 结构化字段
    for field in ("published_date", "publishedAt", "date", "page_age", "age"):
        raw = item.get(field)
        if not raw:
            continue
        parsed = parse_result_datetime(str(raw))
        if parsed:
            return parsed, f"{field}={raw}"
        # 尝试中文绝对 / 相对
        parsed_cn = _parse_chinese_absolute(str(raw)) or _parse_chinese_relative(str(raw))
        if parsed_cn:
            return parsed_cn, f"{field}={raw}(cn)"

    # 2) 文本内嵌日期探测
    haystack = " ".join(
        str(item.get(k, "") or "")
        for k in ("title", "description", "content", "snippet")
    )
    if haystack:
        cn_abs = _parse_chinese_absolute(haystack)
        if cn_abs:
            return cn_abs, "text_embedded_cn"
        cn_rel = _parse_chinese_relative(haystack)
        if cn_rel:
            return cn_rel, "text_embedded_cn_relative"
        # 英文相对 / ISO 日期
        for pattern in EMBEDDED_DATE_PATTERNS:
            m = pattern.search(haystack)
            if m:
                parsed = parse_result_datetime(m.group(1) if m.groups() else m.group(0))
                if parsed:
                    return parsed, "text_embedded_en"
    return None, ""


def hard_time_cutoff_filter(
    search_payload: dict[str, Any],
    *,
    max_age_hours: float | None = None,
) -> dict[str, Any]:
    """硬性时间截断：丢弃事件时间超过 max_age_hours 的候选新闻。

    无法解析出日期的条目默认保留；但如果是“时间敏感 query + 官方公告/博客页 + 无日期”，则直接丢弃。
    """
    if max_age_hours is None:
        max_age_hours = HARD_TIME_CUTOFF_HOURS
    now_utc = dt.datetime.now(dt.timezone.utc)
    cutoff = now_utc - dt.timedelta(hours=max_age_hours)

    filtered = {
        "generated_at": search_payload.get("generated_at", now_local().isoformat()),
        "ai_news": dict(search_payload.get("ai_news", {})),
        "product_hunt": dict(search_payload.get("product_hunt", {})),
    }
    # 保留其它 bucket（如 history_filter, quality_summary）
    for k, v in search_payload.items():
        if k not in filtered:
            filtered[k] = v

    dropped_items: list[dict[str, Any]] = []
    dropped_full_pool: list[dict[str, Any]] = []  # 保留完整 item，供 B2 复活使用
    undated_count = 0
    kept_count = 0

    for bucket in ("ai_news", "product_hunt"):
        bucket_payload = dict(filtered.get(bucket, {}))
        for result_key in SEARCH_RESULT_KEYS:
            results = list(bucket_payload.get(result_key, []))
            kept: list[dict[str, Any]] = []
            for item in results:
                event_time, source = _detect_item_event_time(item)
                if event_time is None:
                    title_clean = clean_text(str(item.get("title") or ""))
                    content_clean = clean_text(
                        " ".join(
                            str(item.get(field) or "")
                            for field in ("content", "description", "snippet")
                        )
                    )
                    source_query = clean_text(str(item.get("source_query") or ""))
                    if is_undated_time_sensitive_announcement(
                        title_clean.lower(),
                        f"{title_clean} {content_clean}".lower(),
                        str(item.get("url") or ""),
                        source_query,
                        item=item,
                    ):
                        title_snippet = title_clean[:60]
                        print(
                            f"{_WARN_COLOR}[拦截·时间] 丢弃无日期时间敏感公告页: {title_snippet} "
                            f"| query: {source_query[:80]}{_RESET_COLOR}",
                            file=sys.stderr,
                        )
                        dropped_items.append({
                            "bucket": bucket,
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                            "event_time": "",
                            "age_hours": None,
                            "source": "undated_time_sensitive",
                        })
                        dropped_full_pool.append({
                            **item,
                            "_origin_bucket": bucket,
                            "_origin_result_key": result_key,
                            "_drop_reason": "undated_time_sensitive",
                            "_event_time": "",
                            "_age_hours": None,
                        })
                        continue
                    undated_count += 1
                    kept.append(item)
                    continue
                age_hours = (now_utc - event_time).total_seconds() / 3600.0
                if age_hours > max_age_hours:
                    title_snippet = (item.get("title") or "")[:60]
                    event_str = event_time.strftime("%Y-%m-%d")
                    print(
                        f"{_WARN_COLOR}[拦截·时间] 丢弃旧闻: {title_snippet} "
                        f"| 发生时间: {event_str} ({age_hours:.1f}h ago) "
                        f"| 来源: {source}{_RESET_COLOR}",
                        file=sys.stderr,
                    )
                    dropped_items.append({
                        "bucket": bucket,
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "event_time": event_str,
                        "age_hours": round(age_hours, 1),
                        "source": source,
                    })
                    # 完整 item 保留为复活池，附带原 bucket / result_key 元信息
                    dropped_full_pool.append({
                        **item,
                        "_origin_bucket": bucket,
                        "_origin_result_key": result_key,
                        "_drop_reason": "time_cutoff",
                        "_event_time": event_str,
                        "_age_hours": round(age_hours, 1),
                    })
                    continue
                kept.append(item)
                kept_count += 1
            bucket_payload[result_key] = kept
        filtered[bucket] = bucket_payload

    filtered["hard_time_filter"] = {
        "max_age_hours": max_age_hours,
        "dropped_count": len(dropped_items),
        "kept_count": kept_count,
        "undated_count": undated_count,
        "dropped_items": dropped_items,
    }
    # B2/B3: 把完整丢弃池挂在临时键上，供下游复活/backlog 使用
    filtered["_time_cutoff_pool"] = dropped_full_pool
    if dropped_items:
        print(
            f"{_WARN_COLOR}[拦截·时间] 汇总: 丢弃 {len(dropped_items)} 条旧闻，"
            f"保留 {kept_count} 条，{undated_count} 条无日期放行{_RESET_COLOR}",
            file=sys.stderr,
        )
    return filtered


# 实体提取正则
_ENGLISH_PROPER_RE = re.compile(r"\b([A-Z][A-Za-z0-9\-]{3,20})\b")
# 中文"数字+单位"金额/规模，如「300亿」「3800亿美元」「50万」
_CN_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?[万千亿])")
# 中文 3-8 字命名实体候选（用于捕获「通义千问」「文心一言」「飞桨」这类）
_CN_NAMED_ENTITY_RE = re.compile(r"([\u4e00-\u9fa5]{3,8})")
# 高频中文停用词（3+ 字的虚词/常见组合），避免把它们当成拦截 key
_CN_STOPWORDS: set[str] = {
    "今天", "今日", "昨天", "昨日", "明天", "明日", "以下", "以上",
    "播报", "新闻", "动态", "消息", "报道", "全球", "业界", "行业",
    "公司", "企业", "团队", "用户", "功能", "产品", "服务", "平台",
    "研究", "发布", "更新", "推出", "宣布", "完成", "实现", "表示",
    "认为", "指出", "介绍", "展示", "预计", "预期", "达到", "超过",
    "方面", "方式", "方向", "领域", "市场", "价值", "时间", "问题",
    "人工智能", "科技", "技术", "模型", "系统", "应用", "数据",
    "感谢", "收听", "下期", "早上", "早上好",
}


# C3: 实体 TTL 衰减阈值（天）。距今 > TTL 的实体不再参与硬拦截，
# 让 OpenAI/Anthropic/Gemini 这种高频家族词在冷却期之后可以重新进入候选。
HARD_BLOCK_TTL_DAYS = float(os.environ.get("SOLO_HARD_BLOCK_TTL_DAYS", "3"))


def _extract_entities_from_text(text: str) -> set[str]:
    """从一段播报文本里抽取拦截实体集合（不含 TTL 信息，纯字符串集合）。"""
    out: set[str] = set()
    for match in _ENGLISH_PROPER_RE.finditer(text):
        token = match.group(1)
        if token.lower() in _HARD_BLOCK_STOPWORDS:
            continue
        if len(token) < 4:
            continue
        out.add(token.lower())
    for match in _CN_AMOUNT_RE.finditer(text):
        out.add(match.group(1))
    for match in _CN_NAMED_ENTITY_RE.finditer(text):
        token = match.group(1)
        if token in _CN_STOPWORDS:
            continue
        if len(token) <= 2:
            continue
        out.add(token)
    return out


def extract_recent_script_entities(
    *,
    last_n: int | None = None,
    root_dir: Path | None = None,
    ttl_days: float | None = None,
) -> set[str]:
    """读取最近 N 期 podcast_script.txt，提取拦截用的高辨识度实体集合。

    C3 升级：每个实体记录"最近一次出现距今的天数"，仅保留 ≤ ttl_days 的实体。
    这样 Anthropic/OpenAI 这类家族词在 TTL 之后可以重新进入候选池。

    实体来源：
      1. 英文大写专有名词（长度 4-20，排除停用词）
      2. 中文数字+单位（如「300亿」「3800亿」）
      3. 中文 3-8 字命名实体（排除中文停用词）

    返回 lowercase 去重集合（已按 ttl 过滤）。
    """
    if last_n is None:
        last_n = HARD_HISTORY_BLOCK_LAST_N
    if ttl_days is None:
        ttl_days = HARD_BLOCK_TTL_DAYS
    base = Path(root_dir) if root_dir else ROOT_DIR
    today = now_local().date()

    # 按期号倒序取最近 last_n 期
    episode_dirs = sorted(
        [p for p in base.glob("20??????E*") if p.is_dir()],
        key=lambda p: p.name,
        reverse=True,
    )[:last_n]

    entity_age: dict[str, float] = {}  # entity -> 距今最小天数
    for ep_dir in episode_dirs:
        ep_date = episode_date_from_name(ep_dir.name)
        age_days: float
        if ep_date:
            age_days = max((today - ep_date).days, 0)
        else:
            age_days = 0  # 解析不出期号日期时按 today 处理（保守拦截）
        script_file = ep_dir / "podcast_script.txt"
        if not script_file.exists():
            continue
        try:
            text = script_file.read_text(encoding="utf-8")
        except Exception as exc:
            print(f"[拦截·历史] 读取 {script_file} 失败: {exc}", file=sys.stderr)
            continue
        for ent in _extract_entities_from_text(text):
            if ent not in entity_age or age_days < entity_age[ent]:
                entity_age[ent] = age_days

    # 按 TTL 过滤
    fresh = {ent for ent, age in entity_age.items() if age <= ttl_days}
    expired = len(entity_age) - len(fresh)

    if fresh:
        preview = ", ".join(sorted(fresh)[:20])
        print(
            f"{_WARN_COLOR}[拦截·历史] 最近 {len(episode_dirs)} 期共 {len(entity_age)} 个实体，"
            f"TTL≤{ttl_days:.0f}d 保留 {len(fresh)} 个 (过期 {expired} 个)；"
            f"前 20 个: {preview}{_RESET_COLOR}",
            file=sys.stderr,
        )
    else:
        print(
            f"[拦截·历史] 最近 {len(episode_dirs)} 期共 {len(entity_age)} 个实体，"
            f"TTL 过滤后无活跃实体（过期 {expired} 个）",
            file=sys.stderr,
        )
    return fresh


def hard_history_block_filter(
    search_payload: dict[str, Any],
    blocked_entities: set[str],
) -> dict[str, Any]:
    """硬性历史拦截：丢弃标题/摘要命中历史实体的候选新闻。"""
    if not blocked_entities:
        return search_payload

    filtered = {
        "generated_at": search_payload.get("generated_at", now_local().isoformat()),
        "ai_news": dict(search_payload.get("ai_news", {})),
        "product_hunt": dict(search_payload.get("product_hunt", {})),
    }
    for k, v in search_payload.items():
        if k not in filtered:
            filtered[k] = v

    dropped_items: list[dict[str, Any]] = []
    dropped_full_pool: list[dict[str, Any]] = []

    for bucket in ("ai_news", "product_hunt"):
        bucket_payload = dict(filtered.get(bucket, {}))
        for result_key in SEARCH_RESULT_KEYS:
            results = list(bucket_payload.get(result_key, []))
            kept: list[dict[str, Any]] = []
            for item in results:
                haystack_parts = [
                    str(item.get("title", "") or ""),
                    str(item.get("description", "") or ""),
                    str(item.get("content", "") or ""),
                    str(item.get("snippet", "") or ""),
                ]
                haystack = " ".join(haystack_parts)
                haystack_lower = haystack.lower()

                hit_entity: str | None = None
                for ent in blocked_entities:
                    # 英文/数字实体按 lowercase 子串匹配
                    if ent.isascii():
                        if ent in haystack_lower:
                            hit_entity = ent
                            break
                    else:
                        # 中文/混合：直接子串匹配
                        if ent in haystack:
                            hit_entity = ent
                            break

                if hit_entity:
                    title_snippet = (item.get("title") or "")[:60]
                    print(
                        f"{_BLOCK_COLOR}[拦截·历史] 丢弃重复选题: {title_snippet} "
                        f"| 命中实体: {hit_entity}{_RESET_COLOR}",
                        file=sys.stderr,
                    )
                    dropped_items.append({
                        "bucket": bucket,
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "hit_entity": hit_entity,
                    })
                    dropped_full_pool.append({
                        **item,
                        "_origin_bucket": bucket,
                        "_origin_result_key": result_key,
                        "_drop_reason": "history_block",
                        "_hit_entity": hit_entity,
                    })
                    continue
                kept.append(item)
            bucket_payload[result_key] = kept
        filtered[bucket] = bucket_payload

    filtered["hard_history_filter"] = {
        "blocked_entity_count": len(blocked_entities),
        "dropped_count": len(dropped_items),
        "dropped_items": dropped_items,
    }
    filtered["_history_block_pool"] = dropped_full_pool
    if dropped_items:
        print(
            f"{_WARN_COLOR}[拦截·历史] 汇总: 丢弃 {len(dropped_items)} 条命中"
            f"历史实体的候选{_RESET_COLOR}",
            file=sys.stderr,
        )
    return filtered


def count_payload_candidates(search_payload: dict[str, Any]) -> int:
    """统计搜索载荷中所有 bucket 的候选总条数，用于缺省降级判断。"""
    total = 0
    for bucket in ("ai_news", "product_hunt"):
        bucket_payload = search_payload.get(bucket, {})
        if isinstance(bucket_payload, dict):
            total += len(bucket_result_items(bucket_payload))
    return total


# ===========================================================================
# B2 + B3: 首次播报判定 + backlog 复活机制
# ===========================================================================

COVERAGE_INDEX_PATH = ROOT_DIR / ".coverage_index.json"
NEWS_BACKLOG_PATH = ROOT_DIR / ".news_backlog.jsonl"
COVERAGE_INDEX_LOOKBACK_DAYS = 21
BACKLOG_MAX_AGE_DAYS = 14
BACKLOG_MAX_ENTRIES = 300
BACKLOG_REVIVE_MAX_AGE_HOURS = float(os.environ.get("SOLO_BACKLOG_REVIVE_MAX_AGE_HOURS", "168"))  # 7 天
PUBLISHED_EPISODE_STATUSES = {
    "published",
    "published_after_retry",
}


def _norm_url_key(url: str) -> str:
    """归一化 URL，作为 coverage 比较的主键。"""
    canonical = canonicalize_url(url or "")
    return canonical.lower() if canonical else ""


def _norm_title_key(title: str) -> str:
    return normalize_title_key(title or "")


def _episode_counts_as_broadcast(
    episode_dir: Path,
    *,
    record: dict[str, Any] | None = None,
    xiaoyuzhou_result: dict[str, Any] | None = None,
    listenhub_episode: dict[str, Any] | None = None,
) -> bool:
    def _clean_marker(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    if not episode_dir.is_dir():
        return False

    if record is None:
        record_path = episode_dir / "record.json"
        record = read_json(record_path) if record_path.exists() else {}
    if xiaoyuzhou_result is None:
        result_path = episode_dir / "xiaoyuzhou_publish_result.json"
        xiaoyuzhou_result = read_json(result_path) if result_path.exists() else {}
    if listenhub_episode is None:
        listenhub_path = episode_dir / "listenhub_episode_result.json"
        listenhub_episode = read_json(listenhub_path) if listenhub_path.exists() else {}

    status = str(record.get("发布状态") or record.get("status") or "").strip().lower()
    if status in PUBLISHED_EPISODE_STATUSES:
        return True

    if any(
        _clean_marker(record.get(key, ""))
        for key in ("小宇宙链接", "小宇宙发布时间", "xiaoyuzhou_url", "xiaoyuzhou_pub_date")
    ):
        return True

    publish_state = str(record.get("小宇宙提审状态") or "").strip().lower()
    if publish_state in {"submitted_to_xiaoyuzhou", "published"}:
        return True

    episode_payload = xiaoyuzhou_result.get("episode", {}) if isinstance(xiaoyuzhou_result, dict) else {}
    if any(
        _clean_marker(value)
        for value in (
            episode_payload.get("eid"),
            episode_payload.get("id"),
            episode_payload.get("url"),
            xiaoyuzhou_result.get("eid") if isinstance(xiaoyuzhou_result, dict) else "",
            xiaoyuzhou_result.get("url") if isinstance(xiaoyuzhou_result, dict) else "",
        )
    ):
        return True

    if any(
        _clean_marker(value)
        for value in (
            listenhub_episode.get("episodeId") if isinstance(listenhub_episode, dict) else "",
            listenhub_episode.get("episodeUrl") if isinstance(listenhub_episode, dict) else "",
            listenhub_episode.get("publishedAt") if isinstance(listenhub_episode, dict) else "",
        )
    ):
        return True

    return False


def _filter_coverage_index_to_broadcast(
    index: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int]:
    filtered: dict[str, dict[str, Any]] = {}
    verdict_cache: dict[str, bool] = {}
    removed = 0
    for key, record in index.items():
        episode_name = str((record or {}).get("episode", "")).strip()
        if not episode_name:
            filtered[key] = record
            continue
        if episode_name not in verdict_cache:
            verdict_cache[episode_name] = _episode_counts_as_broadcast(ROOT_DIR / episode_name)
        if verdict_cache[episode_name]:
            filtered[key] = record
        else:
            removed += 1
    return filtered, removed


def _build_coverage_index_from_disk(*, lookback_days: int = COVERAGE_INDEX_LOOKBACK_DAYS) -> dict[str, dict[str, Any]]:
    """扫描最近 N 天的 episode 目录，提取所有曾经播报过的 URL/title 作为 coverage 索引。

    返回 {url_key: {episode, date, title}}; 同时覆盖 title_key 作为兜底键。
    """
    today = now_local().date()
    index: dict[str, dict[str, Any]] = {}
    for path in ROOT_DIR.iterdir():
        if not path.is_dir():
            continue
        ep_date = episode_date_from_name(path.name)
        if not ep_date:
            continue
        age = (today - ep_date).days
        if age < 0 or age > lookback_days:
            continue
        record_path = path / "record.json"
        listenhub_episode_path = path / "listenhub_episode_result.json"
        xiaoyuzhou_result_path = path / "xiaoyuzhou_publish_result.json"
        record = read_json(record_path) if record_path.exists() else {}
        listenhub_episode = read_json(listenhub_episode_path) if listenhub_episode_path.exists() else {}
        xiaoyuzhou_result = read_json(xiaoyuzhou_result_path) if xiaoyuzhou_result_path.exists() else {}
        if not _episode_counts_as_broadcast(
            path,
            record=record,
            xiaoyuzhou_result=xiaoyuzhou_result,
            listenhub_episode=listenhub_episode,
        ):
            continue
        # 从 search_results.json 中收集所有候选条目（最准确）
        sr_path = path / "search_results.json"
        if sr_path.exists():
            try:
                sr = read_json(sr_path)
            except Exception:
                sr = {}
            for bucket in ("ai_news", "product_hunt"):
                items = bucket_result_items(sr.get(bucket, {}) or {})
                for item in items:
                    url_key = _norm_url_key(item.get("url", ""))
                    title_key = _norm_title_key(item.get("title", ""))
                    if not url_key and not title_key:
                        continue
                    record = {
                        "episode": path.name,
                        "date": ep_date.isoformat(),
                        "title": item.get("title", ""),
                    }
                    if url_key and url_key not in index:
                        index[url_key] = record
                    if title_key and ("title:" + title_key) not in index:
                        index["title:" + title_key] = record
        # 从 research.md 兜底抓 URL（覆盖未走 search_results 的旧期）
        rm_path = path / "research.md"
        if rm_path.exists():
            try:
                rm_text = rm_path.read_text(encoding="utf-8")
            except Exception:
                rm_text = ""
            for url in extract_urls(rm_text):
                url_key = _norm_url_key(url)
                if url_key and url_key not in index:
                    index[url_key] = {
                        "episode": path.name,
                        "date": ep_date.isoformat(),
                        "title": "",
                    }
    return index


def load_coverage_index(*, force_rebuild: bool = False) -> dict[str, dict[str, Any]]:
    """加载 coverage 索引；缓存到磁盘，超过 6h 自动重建。"""
    if not force_rebuild and COVERAGE_INDEX_PATH.exists():
        try:
            cached = read_json(COVERAGE_INDEX_PATH)
            cached_index = cached.get("index", {})
            filtered_index, removed = _filter_coverage_index_to_broadcast(cached_index)
            if removed:
                cached["index"] = filtered_index
                cached["_built_at"] = now_local().isoformat()
                write_json(COVERAGE_INDEX_PATH, cached)
            built_at = cached.get("_built_at", "")
            if built_at:
                built_dt = parse_result_datetime(built_at)
                if built_dt and (now_local() - built_dt.replace(tzinfo=None)).total_seconds() < 6 * 3600:
                    return filtered_index
        except Exception:
            pass
    index = _build_coverage_index_from_disk()
    try:
        write_json(COVERAGE_INDEX_PATH, {"_built_at": now_local().isoformat(), "index": index})
    except Exception as exc:
        print(f"[B2] 写入 coverage 索引失败: {exc}", file=sys.stderr)
    print(
        f"\033[1;36m[B2] coverage 索引已加载: {len(index)} 个键 "
        f"(最近 {COVERAGE_INDEX_LOOKBACK_DAYS} 天){_RESET_COLOR}",
        file=sys.stderr,
    )
    return index


def is_item_already_broadcast(item: dict[str, Any], coverage_index: dict[str, dict[str, Any]]) -> bool:
    """根据 url / title 在 coverage 索引中查找是否已被播报过。"""
    url_key = _norm_url_key(item.get("url", ""))
    if url_key and url_key in coverage_index:
        return True
    title_key = _norm_title_key(item.get("title", ""))
    if title_key and ("title:" + title_key) in coverage_index:
        return True
    return False


def append_items_to_backlog(items: list[dict[str, Any]], *, kind: str) -> int:
    """把丢弃池中的 item 追加进 backlog JSONL；按 url 去重；超过容量则截断。"""
    if not items:
        return 0
    NEWS_BACKLOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing_keys: set[str] = set()
    existing_lines: list[str] = []
    if NEWS_BACKLOG_PATH.exists():
        try:
            existing_lines = NEWS_BACKLOG_PATH.read_text(encoding="utf-8").splitlines()
        except Exception:
            existing_lines = []
        for line in existing_lines:
            try:
                rec = json.loads(line)
                key = _norm_url_key(rec.get("url", "")) or _norm_title_key(rec.get("title", ""))
                if key:
                    existing_keys.add(key)
            except Exception:
                continue

    added = 0
    new_lines: list[str] = []
    for item in items:
        key = _norm_url_key(item.get("url", "")) or _norm_title_key(item.get("title", ""))
        if not key or key in existing_keys:
            continue
        record = dict(item)
        record["_backlog_kind"] = kind
        record["_backlog_added_at"] = now_local().isoformat()
        new_lines.append(json.dumps(record, ensure_ascii=False))
        existing_keys.add(key)
        added += 1

    if not added:
        return 0

    all_lines = existing_lines + new_lines
    # 容量截断：保留最近 BACKLOG_MAX_ENTRIES 行（尾部最新）
    if len(all_lines) > BACKLOG_MAX_ENTRIES:
        all_lines = all_lines[-BACKLOG_MAX_ENTRIES:]
    NEWS_BACKLOG_PATH.write_text("\n".join(all_lines) + "\n", encoding="utf-8")
    print(
        f"\033[36m[B3] backlog 追加 {added} 条 ({kind})，"
        f"现总量 {len(all_lines)}\033[0m",
        file=sys.stderr,
    )
    return added


def load_backlog_candidates(
    coverage_index: dict[str, dict[str, Any]],
    *,
    max_age_days: int = BACKLOG_MAX_AGE_DAYS,
) -> list[dict[str, Any]]:
    """从 backlog 文件读取尚未播报且未超龄的 item 列表。"""
    if not NEWS_BACKLOG_PATH.exists():
        return []
    try:
        lines = NEWS_BACKLOG_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    cutoff = now_local() - dt.timedelta(days=max_age_days)
    candidates: list[dict[str, Any]] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        added_at_raw = rec.get("_backlog_added_at", "")
        added_dt = parse_result_datetime(added_at_raw) if added_at_raw else None
        if added_dt and added_dt.replace(tzinfo=None) < cutoff.replace(tzinfo=None):
            continue
        if is_item_already_broadcast(rec, coverage_index):
            continue
        candidates.append(rec)
    return candidates


def revive_unbroadcast_from_pools(
    search_payload: dict[str, Any],
    coverage_index: dict[str, dict[str, Any]],
    *,
    max_age_hours: float | None = None,
) -> dict[str, Any]:
    """B2: 把硬时间截断丢弃但从未播报过的条目复活到 bucket['rss_results']。

    复活规则：
      - 来自 _time_cutoff_pool（B2 主战场）
      - URL/title 不在 coverage 索引中
      - 事件时间 ≤ max_age_hours（默认 168h / 7 天，避免把月级别旧闻拉回来）
    """
    if max_age_hours is None:
        max_age_hours = BACKLOG_REVIVE_MAX_AGE_HOURS
    pool = search_payload.get("_time_cutoff_pool", []) or []
    if not pool:
        return search_payload

    revived_by_bucket: dict[str, list[dict[str, Any]]] = {}
    revived_count = 0
    skipped_covered = 0
    skipped_too_old = 0
    for raw in pool:
        age_hours = float(raw.get("_age_hours") or 0)
        if age_hours > max_age_hours:
            skipped_too_old += 1
            continue
        if is_item_already_broadcast(raw, coverage_index):
            skipped_covered += 1
            continue
        bucket = raw.get("_origin_bucket") or "ai_news"
        # 拷贝并清掉内部前缀键
        clean = {k: v for k, v in raw.items() if not k.startswith("_")}
        clean["revived_from"] = "time_cutoff"
        clean["revived_age_hours"] = age_hours
        revived_by_bucket.setdefault(bucket, []).append(clean)
        revived_count += 1

    if revived_count == 0:
        print(
            f"\033[36m[B2] 时间截断池 {len(pool)} 条均未复活 "
            f"(已播报={skipped_covered}, 超龄={skipped_too_old}){_RESET_COLOR}",
            file=sys.stderr,
        )
        return search_payload

    out = dict(search_payload)
    for bucket, items in revived_by_bucket.items():
        bucket_payload = dict(out.get(bucket, {}))
        existing = list(bucket_payload.get("rss_results", []) or [])
        bucket_payload["rss_results"] = existing + items
        out[bucket] = bucket_payload
    out["b2_revival"] = {
        "revived_count": revived_count,
        "skipped_covered": skipped_covered,
        "skipped_too_old": skipped_too_old,
        "max_age_hours": max_age_hours,
    }
    print(
        f"\033[1;32m[B2] 复活未播报旧闻 {revived_count} 条 "
        f"(已播报={skipped_covered}, 超龄={skipped_too_old}){_RESET_COLOR}",
        file=sys.stderr,
    )
    return out


def inject_backlog_candidates(
    search_payload: dict[str, Any],
    coverage_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """B3: 把 backlog 中未播报的条目注入到 backlog_results bucket。"""
    candidates = load_backlog_candidates(coverage_index)
    if not candidates:
        return search_payload

    injected_by_bucket: dict[str, list[dict[str, Any]]] = {}
    for rec in candidates:
        bucket = rec.get("_origin_bucket") or "ai_news"
        clean = {k: v for k, v in rec.items() if not k.startswith("_")}
        clean["revived_from"] = "backlog"
        injected_by_bucket.setdefault(bucket, []).append(clean)

    out = dict(search_payload)
    total = 0
    for bucket, items in injected_by_bucket.items():
        bucket_payload = dict(out.get(bucket, {}))
        existing = list(bucket_payload.get("backlog_results", []) or [])
        bucket_payload["backlog_results"] = existing + items
        out[bucket] = bucket_payload
        total += len(items)
    out["b3_backlog"] = {"injected_count": total}
    print(
        f"\033[1;32m[B3] backlog 注入 {total} 条候选\033[0m",
        file=sys.stderr,
    )
    return out


def persist_drop_pools_to_backlog(search_payload: dict[str, Any]) -> None:
    """把硬过滤的 _time_cutoff_pool / _history_block_pool 写入 backlog JSONL，
    供未来的运行复活使用。"""
    time_pool = search_payload.get("_time_cutoff_pool", []) or []
    hist_pool = search_payload.get("_history_block_pool", []) or []
    if time_pool:
        append_items_to_backlog(time_pool, kind="time_cutoff")
    if hist_pool:
        append_items_to_backlog(hist_pool, kind="history_block")


def update_coverage_index_for_episode(
    episode: str,
    items: list[dict[str, Any]],
) -> None:
    """脚本生成完成后，把当期实际入选的条目写入 coverage 索引（并刷新缓存）。"""
    if not items:
        return
    episode_dir = ROOT_DIR / episode
    if not _episode_counts_as_broadcast(episode_dir):
        print(
            f"\033[36m[B2] 跳过 coverage 索引追加: {episode} 尚未发布\033[0m",
            file=sys.stderr,
        )
        return
    today = now_local().date().isoformat()
    existing = {}
    if COVERAGE_INDEX_PATH.exists():
        try:
            existing = read_json(COVERAGE_INDEX_PATH)
        except Exception:
            existing = {}
    index = existing.get("index", {}) if isinstance(existing, dict) else {}
    added = 0
    for item in items:
        url_key = _norm_url_key(item.get("url", ""))
        title_key = _norm_title_key(item.get("title", ""))
        record = {"episode": episode, "date": today, "title": item.get("title", "")}
        if url_key and url_key not in index:
            index[url_key] = record
            added += 1
        if title_key and ("title:" + title_key) not in index:
            index["title:" + title_key] = record
    try:
        write_json(COVERAGE_INDEX_PATH, {"_built_at": now_local().isoformat(), "index": index})
    except Exception as exc:
        print(f"[B2] 写入 coverage 索引失败: {exc}", file=sys.stderr)
        return
    print(
        f"\033[36m[B2] coverage 索引追加 {added} 条 (期号 {episode})\033[0m",
        file=sys.stderr,
    )


def load_recent_search_backlog(
    *,
    exclude_episode: str = "",
    lookback_days: int = SEARCH_BACKLOG_LOOKBACK_DAYS,
    max_episodes: int = SEARCH_BACKLOG_MAX_EPISODES,
) -> dict[str, list[dict[str, Any]]]:
    today = now_local().date()
    recent_paths: list[tuple[dt.date, Path]] = []
    for path in ROOT_DIR.iterdir():
        if not path.is_dir():
            continue
        if exclude_episode and path.name == exclude_episode:
            continue
        episode_date = episode_date_from_name(path.name)
        if not episode_date:
            continue
        age_days = (today - episode_date).days
        if age_days < 0 or age_days > lookback_days:
            continue
        if not (path / "search_results.json").exists():
            continue
        recent_paths.append((episode_date, path))

    recent_paths.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    backlog = {"ai_news": [], "product_hunt": []}
    for _, path in recent_paths[:max_episodes]:
        try:
            payload = read_json(path / "search_results.json")
        except Exception:
            continue
        for bucket in ("ai_news", "product_hunt"):
            bucket_payload = payload.get(bucket, {})
            for item in bucket_result_items(bucket_payload):
                enriched = dict(item)
                enriched.setdefault("source_episode", path.name)
                enriched.setdefault("source_bucket", bucket)
                backlog[bucket].append(enriched)

    for bucket in backlog:
        backlog[bucket] = dedupe_search_items(backlog[bucket], limit=48)
    return backlog


def supplement_search_payload_with_recent_uncovered_items(
    search_payload: dict[str, Any],
    recent_context: dict[str, Any],
    *,
    exclude_episode: str = "",
) -> dict[str, Any]:
    supplemented = {
        "generated_at": search_payload.get("generated_at", now_local().isoformat()),
        "ai_news": dict(search_payload.get("ai_news", {})),
        "product_hunt": dict(search_payload.get("product_hunt", {})),
    }
    if "history_filter" in search_payload:
        supplemented["history_filter"] = dict(search_payload.get("history_filter", {}))
    # 透传其余 top-level 键（funnel_stats / b2_revival / b3_backlog / hard_*_filter 等）
    for k, v in search_payload.items():
        if k not in supplemented:
            supplemented[k] = v

    backlog = load_recent_search_backlog(exclude_episode=exclude_episode)
    added_items: list[dict[str, str]] = []

    for bucket in ("ai_news", "product_hunt"):
        bucket_payload = dict(supplemented.get(bucket, {}))
        current_items = bucket_result_items(bucket_payload)
        current_count = len(current_items)
        max_to_add = min(
            SEARCH_BACKLOG_MAX_ADDITIONS_PER_BUCKET,
            max(0, SEARCH_BACKLOG_TARGET_PER_BUCKET - current_count),
        )
        backlog_results: list[dict[str, Any]] = []

        if max_to_add > 0:
            for item in backlog.get(bucket, []):
                rejection_reason = search_item_rejection_reason(
                    clean_text(item.get("title", "")),
                    clean_text(item.get("content", "") or item.get("description", "")),
                    str(item.get("url", "")),
                    source_query=clean_text(item.get("source_query", "")),
                    item=item,
                )
                if rejection_reason:
                    continue
                if classify_recent_duplicate(item, recent_context):
                    continue
                if any(items_represent_same_story(item, existing) for existing in current_items + backlog_results):
                    continue
                backlog_results.append(item)
                added_items.append(
                    {
                        "bucket": bucket,
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "source_episode": str(item.get("source_episode", "")),
                    }
                )
                if len(backlog_results) >= max_to_add:
                    break

        bucket_payload["backlog_results"] = backlog_results
        supplemented[bucket] = bucket_payload

    history_filter = dict(supplemented.get("history_filter", {}))
    history_filter["backlog_lookback_days"] = SEARCH_BACKLOG_LOOKBACK_DAYS
    history_filter["backlog_added_items"] = added_items
    supplemented["history_filter"] = history_filter
    return supplemented


def build_recent_coverage_note(recent_context: dict[str, Any]) -> str:
    episodes = recent_context.get("episodes", [])
    if not episodes:
        return "近几天暂无可参考历史选题。"
    lines = ["近几天已经讲过的话题如下，请避免重复同一事件或同一新闻换个媒体标题再讲："]
    for episode in episodes[:RECENT_SIMILARITY_MAX_EPISODES]:
        trend_titles = episode.get("trend_titles") or [episode.get("title", "")]
        lines.append(f"- {episode['date']} {episode['episode']}：{'；'.join(trend_titles[:3])}")
    return "\n".join(lines)


def create_duo_research_markdown(search_payload: dict[str, Any], recent_context: dict[str, Any] | None = None) -> str:
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    qwen_key = os.getenv("QWEN_API_KEY", "").strip()
    if not deepseek_key and not qwen_key:
        raise RuntimeError("双人口播 research 生成需要 DEEPSEEK_API_KEY 或 QWEN_API_KEY，当前均未设置")

    recent_note = build_recent_coverage_note(recent_context or {"episodes": []})

    system_lines = [
        "你是中文 AI 商业媒体主编，负责为双人口播节目生成当日深度研究稿。",
        "请基于 user 消息里的搜索结果 JSON，输出一篇完整 Markdown 日报。",
        "",
        "## 一、选题规则",
        "1. 优先选择今天或昨天发生的事件；超过 48 小时且无新进展的旧闻，不得作为主趋势。",
        "2. 三条趋势必须覆盖不同维度，不能全是模型发布，不能全是融资，至少覆盖以下两类中的两类：",
        "   - 产业基础设施（芯片、算力、数据中心、光通信）",
        "   - 商业与资本信号（财报、融资、裁员、定价策略）",
        "   - 模型与产品动作（发布、定价、开放 API、新能力）",
        "   - AI 对劳动力或行业的真实冲击",
        "3. 不同媒体转述同一条新闻只算一条，合并处理。",
        "4. 近期已播报选题，必须规避：\n" + recent_note,
        "",
        "## 二、输出结构（严格按此顺序）",
        "",
        "### 标题",
        "格式：# YYYY年M月D日：XXXXXXXX",
        "冒号后 8 到 16 个汉字，有冲突感、判断或悬念，像公众号爆款标题。",
        "好标题例子：谷歌赚了 Meta 烧钱，AI 开始分账了 / 万亿融资的背面是万人裁员 / 大模型退潮小参数反杀",
        "禁止标题：AI全产业链重新排位、AI生态演进、AI关键变化、AI研究突破走向产业化。",
        "",
        "### 今日结论",
        "3 条具体结论，每条包含事实依据（公司名/数字/事件）。",
        "禁止空话，禁止「AI热点正在...转向...」这类无信息量的句子。",
        "",
        "### 三大趋势",
        "每条趋势包含三个子节：",
        "",
        "**发生了什么**",
        "- 必须有：具体公司名、产品名、数字、时间、来源中的关键细节。",
        "- 禁止：「一家科技公司宣布」「业内人士表示」等模糊表述。",
        "- 原文关键数据（金额/比例/参数/用户数）必须保留。",
        "",
        "**为什么重要**",
        "- 三条趋势的「为什么重要」内容必须各不相同，严禁套用同一模板句。",
        "- 必须回答：这条新闻在 AI 产业链处于哪个位置？对哪类玩家影响最大？为什么现在这个时间点特别值得关注？",
        "- 包含具体的产业逻辑或竞争背景，不允许只写「竞争正在从模型能力转向」。",
        "",
        "**意味着什么**",
        "- 三条趋势的「意味着什么」内容必须各不相同，严禁套用同一模板句。",
        "- 给出 2 到 3 个不同维度的具体影响，分别针对不同群体（如 NVIDIA、中国 AI 厂商、初创公司、从业者）。",
        "- 包含至少一条带明确方向感的预判，不能只描述现状。",
        "",
        "### Product Hunt 产品洞察",
        "- 至少点评 2 个产品；Product Hunt 内容不足时可用其他 AI 工具补充。",
        "- 每个产品：定位一句话、最值得关注的亮点、潜在局限、对播客受众的启示。",
        "- 有融资额/用户数/增长数据的必须写入。",
        "",
        "### 深度研究",
        "- 800 字以上，禁止空话。",
        "- 至少 3 个分析层次（第一层现象、第二层结构性原因、第三层长期预判）。",
        "- 每层有具体公司名、数字或事件支撑，不允许全段都是抽象判断。",
        "- 最后一段给出明确的、有争议性的大判断，不要用「值得持续关注」收尾。",
        "",
        "### 信息源",
        "格式：- [文章标题](完整URL)",
        "列出所有三条趋势的原始来源，外加 1 到 3 条延伸阅读。",
        "",
        "## 三、全局禁止",
        "- 禁止编造无法从给定材料找到依据的数字或事件。",
        "- 禁止出现「这条变化说明竞争正在从单点模型能力，转向更完整的产品、基础设施和产业配套」及其任何变体。",
        "- 禁止出现「对创业者来说，真正的机会不只是追模型热点，而是找到能承接这些变化的场景和工作流」及其任何变体。",
        "- 禁止三条趋势用完全相同的句式结尾。",
        "- 禁止使用「巨头公司」「一家领先的AI公司」等模糊指代。",
        "- 直接输出 Markdown，不要包裹在代码块或 JSON 里。",
    ]
    system_prompt = "\n".join(system_lines)
    user_content = json.dumps(search_payload, ensure_ascii=False)

    runners: list[tuple[str, object]] = []
    if deepseek_key:
        runners.append((
            "DeepSeek",
            lambda: _openai_compatible_text(
                api_key=deepseek_key,
                base_url="https://api.deepseek.com/v1",
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                system_prompt=system_prompt,
                user_content=user_content,
                label="duo research deepseek",
                max_tokens=4000,
            ),
        ))
    if qwen_key:
        runners.append((
            "Qwen-Plus",
            lambda: _openai_compatible_text(
                api_key=qwen_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model=os.getenv("QWEN_PLUS_MODEL", "qwen-plus"),
                system_prompt=system_prompt,
                user_content=user_content,
                label="duo research qwen-plus",
                max_tokens=4000,
                no_proxy=True,
            ),
        ))
        runners.append((
            "Qwen-Turbo",
            lambda: _openai_compatible_text(
                api_key=qwen_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model=os.getenv("QWEN_MODEL", "qwen-turbo"),
                system_prompt=system_prompt,
                user_content=user_content,
                label="duo research qwen-turbo",
                max_tokens=4000,
                no_proxy=True,
            ),
        ))

    for name, runner in runners:
        try:
            print(f"[duo research] 尝试 {name}...", file=sys.stderr)
            result = runner()
            if result and len(result) > 200:
                print(f"[duo research] {name} 成功，{len(result)} 字符", file=sys.stderr)
                return result
            print(f"[duo research] {name} 返回内容过短，跳过", file=sys.stderr)
        except Exception as exc:
            print(f"[duo research] {name} 失败: {exc}", file=sys.stderr)

    print("[duo research] 所有 LLM 失败，使用 fallback", file=sys.stderr)
    return fallback_research_markdown(search_payload)
def fallback_solo_research_markdown(search_payload: dict[str, Any]) -> str:
    ai_items = select_trend_items(search_payload, limit=5)
    title = f"# {build_fallback_headline(ai_items, [])}"
    story_lines = []
    for index, item in enumerate(ai_items[:4], start=1):
        what = item["content"] or "（待补充）"
        story_lines.append(
            "\n".join(
                [
                    f"### {index:02d}. {item['title']}",
                    "",
                    "这条讲什么：",
                    what,
                    "",
                    "为什么现在讲：",
                    "今天值得优先播报的 AI 动态。",
                    "",
                    "主播怎么接：",
                    "接下一条。",
                ]
            )
        )
    top_items = [item["title"] for item in ai_items[:3]]
    sources = []
    for item in ai_items[:6]:
        if item["url"]:
            sources.append(f"- {item['title']}：<{item['url']}>")
    return "\n\n".join(
        [
            title,
            f"日期：{now_local().strftime('%Y 年 %m 月 %d 日')}",
            "## 开场角度\n\n今天先从几条硬数字说起。",
            "## 今天先抓这几件事\n\n" + "\n".join(f"- {t}" for t in top_items),
            "## 推荐播报顺序\n\n" + "\n\n".join(story_lines),
            "## 收尾提醒\n\n以上是今天的主要故事。",
            "## 主播备注\n\n- 每条第一句必须点出主体和动作。\n- 不要写空泛展望、趋势拔高或市场评价。",
            "## 信息源\n\n" + "\n".join(sources),
        ]
    )


def _solo_research_candidate_cards(search_payload: dict[str, Any]) -> list[dict[str, str]]:
    items = select_trend_items(search_payload, limit=6)
    cards: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        cards.append(
            {
                "id": str(index),
                "title": clean_text(item.get("title", "")),
                "summary": clean_text(item.get("content", ""))[:240],
                "url": item.get("url", ""),
            }
        )
    return cards


def _render_solo_research_markdown(
    payload: dict[str, Any],
    *,
    cards: list[dict[str, str]],
    search_payload: dict[str, Any],
) -> str:
    fallback_title = build_fallback_headline(select_trend_items(search_payload, limit=5), [])
    title = clean_text(str(payload.get("title", "") or fallback_title))
    if not title.startswith("#"):
        title = f"# {title}"

    opening_angle = clean_text(str(payload.get("opening_angle", "") or ""))
    if not opening_angle:
        opening_angle = "今天先从几条硬数字说起。"

    top_items = [
        clean_text(str(value))
        for value in (payload.get("top_items") or [])
        if clean_text(str(value))
    ][:4]
    if not top_items:
        top_items = [
            "只播报今天最硬的主体、动作和数字。",
            "开头一句带出主线，但不要写成长评论。",
        ]

    host_notes = [
        clean_text(str(value))
        for value in (payload.get("host_notes") or [])
        if clean_text(str(value))
    ][:4]
    if not host_notes:
        host_notes = [
            "每条第一句必须点出主体和动作。",
            "不要写空泛展望、趋势拔高或市场评价。",
        ]

    closing_note = clean_text(str(payload.get("closing_note", "") or ""))
    if not closing_note:
        closing_note = "以上是今天的主要故事。"

    raw_stories = payload.get("stories") or []
    story_lines: list[str] = []
    for index, story in enumerate(raw_stories[:5], start=1):
        if not isinstance(story, dict):
            continue
        story_title = clean_text(str(story.get("title", "")))
        what = clean_text(str(story.get("what", "")))
        why_now = clean_text(str(story.get("why_now", "")))
        transition = clean_text(str(story.get("transition", "")))
        if not story_title:
            continue
        story_lines.append(
            "\n".join(
                [
                    f"### {index:02d}. {story_title}",
                    "",
                    "这条讲什么：",
                    what or "（待补充）",
                    "",
                    "为什么现在讲：",
                    why_now or "今天值得优先播报的 AI 动态。",
                    "",
                    "主播怎么接：",
                    transition or "接下一条。",
                ]
            )
        )

    if not story_lines:
        return fallback_solo_research_markdown(search_payload)

    sources = []
    for card in cards[:6]:
        if card.get("url"):
            sources.append(f"- {card['title']}：<{card['url']}>")

    return "\n\n".join(
        [
            title,
            f"日期：{now_local().strftime('%Y 年 %m 月 %d 日')}",
            f"## 开场角度\n\n{opening_angle}",
            "## 今天先抓这几件事\n\n" + "\n".join(f"- {item}" for item in top_items),
            "## 推荐播报顺序\n\n" + "\n\n".join(story_lines),
            f"## 收尾提醒\n\n{closing_note}",
            "## 主播备注\n\n" + "\n".join(f"- {note}" for note in host_notes),
            "## 信息源\n\n" + "\n".join(sources),
        ]
    )


def create_solo_research_markdown(search_payload: dict[str, Any], recent_context: dict[str, Any] | None = None) -> str:
    cards = _solo_research_candidate_cards(search_payload)
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    qwen_key = os.getenv("QWEN_API_KEY", "").strip()
    if not deepseek_key and not qwen_key:
        return fallback_solo_research_markdown(search_payload)
    recent_note = build_recent_coverage_note(recent_context or {"episodes": []})
    system_prompt = textwrap.dedent(
        """
        你是中文 AI 晨间播客的执行主编。请根据候选新闻卡片，输出一个 JSON 对象，用来渲染单人口播 research.md。

        输出 JSON 结构必须是：
        {
          “title”: “YYYY年M月：一句中文爆款标题（有冲突/有态度/有悬念）”,
          “opening_angle”: “一句开场钩子，点出今天的核心矛盾或最惊人的数字”,
          “top_items”: [“第一条一句话摘要”, “第二条一句话摘要”, “第三条一句话摘要”],
          “stories”: [
            {
              “title”: “中文故事标题”,
              “what”: “这条讲什么：主体、动作、关键数字，一到两句话”,
              “why_now”: “为什么现在讲：今天播这条的理由，一句话”,
              “transition”: “主播怎么接：过渡到下一条的一句承接语”
            }
          ],
          “closing_note”: “收尾一句话，串联今天所有故事的主线”,
          “host_notes”: [“备注1”, “备注2”, “备注3”]
        }

        约束：
        1. 全部用简体中文输出，只有公司名、模型名、产品名这类专有名词可以保留英文。
        2. 这是给 3 到 5 分钟单人口播准备的 research，stories 放 3 到 5 条，不要写成长评论稿。
        3. 每条 story 的 what 必须优先写主体、动作、数字、时间；why_now 和 transition 必须短，服务于”怎么播报”。
        4. 不要出现”潜力巨大、市场高度认可、持续增长、有望达到”这类空泛表述。
        5. 如果候选标题是英文，请翻译成自然中文标题；正文可以保留 Meta、OpenAI、HBM 这类专有名词。
        6. 必须避开近几天已经讲过的同一事件，优先选择新事件或新角度。
        """
    ).strip()
    user_content = json.dumps(
        {
            "date": now_local().strftime("%Y-%m-%d"),
            "recent_note": recent_note,
            "candidate_cards": cards,
        },
        ensure_ascii=False,
    )
    runners: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    if deepseek_key:
        runners.append(
            (
                "DeepSeek",
                lambda: _openai_compatible_json(
                    api_key=deepseek_key,
                    base_url="https://api.deepseek.com/v1",
                    model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                    system_prompt=system_prompt,
                    user_content=user_content,
                    label="deepseek solo research",
                ),
            )
        )
    if qwen_key:
        runners.append(
            (
                "Qwen-Turbo",
                lambda: _openai_compatible_json(
                    api_key=qwen_key,
                    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model=os.getenv("QWEN_MODEL", "qwen-turbo"),
                    system_prompt=system_prompt,
                    user_content=user_content,
                    label="qwen solo research",
                    no_proxy=True,
                ),
            )
        )
    for name, runner in runners:
        try:
            payload = runner()
            return _render_solo_research_markdown(payload, cards=cards, search_payload=search_payload)
        except Exception as exc:
            print(f"[research] {name} solo research 失败: {exc}", file=sys.stderr)
    return fallback_solo_research_markdown(search_payload)


def create_research_markdown(search_payload: dict[str, Any], recent_context: dict[str, Any] | None = None) -> str:
    return create_duo_research_markdown(search_payload, recent_context)


def create_podcast_script(research_markdown: str) -> str:
    meta = parse_research_metadata(research_markdown)
    trend_titles = meta["trend_titles"] or ["今天的三条 AI 趋势"]
    hook = select_podcast_hook(f"{meta['title']} {' '.join(trend_titles)}")
    if not os.getenv("OPENAI_API_KEY"):
        return textwrap.dedent(
            f"""
            {hook} 今天我们就把这些碎片拼起来，聊聊 {now_local().strftime('%Y年%-m月%-d日')} 这一轮 AI 变化，背后到底在指向什么。

            对，而且这期我们不打算报流水账。因为真正有意思的，不是又多了一个新模型，也不是又多了一个新产品，而是这些变化正在同时指向同一件事：AI 正在从“一个会聊天的工具”，变成“一个能真正接管工作流的系统”。

            先看第一条。{trend_titles[0]}。这类新闻表面看像一条单独的技术或产品更新，但它真正说明的是，AI 竞争已经不只是模型能力，而是在比谁更能进入真实使用场景，谁更能把结果直接交付给用户。

            没错，过去我们总喜欢问，大模型谁更聪明，谁分数更高。但现在真正的竞争问题变成了，谁更能嵌进工作流，谁更能把一段复杂流程压缩成一个结果。对普通用户来说，参数差异感知越来越弱，能不能直接帮我把事情做完，才是最核心的体验。

            第二条趋势也很关键，{trend_titles[1] if len(trend_titles) > 1 else 'AI 产品层正在迅速卷向结果交付'}。这意味着产业已经不满足于展示能力，而是在抢“谁能先把用户的时间省下来”。越往后，真正值钱的不是功能点，而是结果闭环。

            这里面 Product Hunt 的信号特别值得看。现在热门 AI 产品越来越像一种“代做型工具”。它们不是只提供一个按钮，而是试图接管一段流程。你会发现，用户愿意传播的产品，往往不是最炫技的，而是最接近交付结果的。

            第三条趋势，{trend_titles[2] if len(trend_titles) > 2 else '内容行业也在被迫升级为判断型生产'}。这条对我们做内容尤其重要。因为当信息越来越密集时，简单转述消息已经不构成价值，真正稀缺的是把变化组织成图景，再把图景翻译成结论。

            所以今天这期内容，最后想落到一个更大的判断上。AI 下半场的胜负手，已经不只是模型更强，而是谁能把模型、产品、场景和基础设施串成一个闭环。模型当然是引擎，但真正形成壁垒的，往往是工作流，是使用频率，是用户是否愿意把关键任务交给你。

            说得更直接一点，未来最有机会的 AI 产品，不一定是最会说的，而是最能交付结果的。未来最有价值的 AI 内容，也不一定是最快的，而是最能帮用户形成判断的。

            如果今天这些新闻必须浓缩成一句话，那就是：AI 正在从“能力展示”走向“结果交付”，而这个变化，可能比任何一次模型跑分刷新都更重要。
            """
        ).strip()
    prompt = textwrap.dedent(
        f"""
        你是中文科技播客编导。请把给定研究稿改写成一篇 6 到 9 分钟的双人对话播客稿。
        要求：
        1. 语气自然、判断明确、适合直接提交给 AI 播客生成器。
        2. 不要使用 Markdown 标记。
        3. 保留三大趋势和最后的深度判断。
        4. 开头要有一句钩子，结尾要有一句收束。
        5. 风格参考中文科技播客里的“口水稿”，像两个主播在拆解新闻背后的共同主线。
        6. 不要再用这些开场句或其轻微改写：{", ".join(BANNED_PODCAST_OPENERS)}。
        7. 这次开头请换一种说法，但保持口语化，不要模板味太重。
        """
    ).strip()
    return openai_response(prompt, {"research_markdown": research_markdown})


def select_cover_title(episode_dir: Path, record: dict[str, Any]) -> str:
    title_path = episode_dir / "title.txt"
    candidates = []
    if title_path.exists():
        candidates.append(title_path.read_text(encoding="utf-8").strip())
    candidates.extend(
        [
            str(record.get("ListenHub标题", "")).strip(),
            str(record.get("标题", "")).strip(),
        ]
    )
    for candidate in candidates:
        if candidate:
            return re.sub(r"\s+", " ", candidate).strip()
    return "AI播客"


def infer_cover_theme_clues(title: str, record: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    combined = " ".join(
        [
            title,
            str(record.get("三大趋势", "")),
            " ".join(metadata.get("trend_titles") or []),
            " ".join(metadata.get("product_titles") or []),
        ]
    ).lower()
    clues: list[str] = []
    if any(token in combined for token in ["基础设施", "芯片", "算力", "hbm", "memory", "server", "chip"]):
        clues.append("深水棋盘上互相压迫的算力堡垒、资本筹码、HBM 晶圆与发热的数据中心")
        clues.append("云厂商、供应链、芯片和光缆像冷战阵营一样在暗色空间里对峙")
    if any(token in combined for token in ["智能体", "agent", "workflow", "orchestration", "desktop", "应用"]):
        clues.append("AI Agent 像棋子或机械手一样进入中心战场，挤压办公室、代码仓库和云端入口")
        clues.append("被资本和自动化推向边缘的人类办公席位，与中央巨型智能体形成强烈视觉冲突")
    if any(token in combined for token in ["安全", "治理", "risk", "security", "guardrail", "合规"]):
        clues.append("被红线与边界网格切开的智能体网络、监控节点围住关键数据通路")
    if any(token in combined for token in ["模型", "anthropic", "reasoning", "mythos", "capability"]):
        clues.append("失控扩张的模型军备竞赛，模型徽章像军团一样向算力塔楼推进")
    if not clues:
        clues.append("深水、棋盘、算力堡垒、资本筹码和科技巨头剪影组成一张暗色电影海报")
    return clues[:4]


def extract_cover_key_excerpt(research_markdown: str) -> str:
    preferred_sections = ("## 深度研究", "## 今日结论", "## 三大趋势")
    current_section = ""
    paragraphs: list[str] = []

    for raw_line in research_markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            current_section = line
            continue
        if line.startswith("### ") or line.startswith("- ") or re.match(r"^\d+\.\s", line):
            continue
        if current_section in preferred_sections:
            cleaned = re.sub(r"<[^>]+>", "", line)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if len(cleaned) >= 28:
                paragraphs.append(cleaned)
        if len(paragraphs) >= 2:
            break

    excerpt = " ".join(paragraphs[:2]).strip()
    return excerpt[:420]


def build_cover_background_prompt(research_markdown: str, record: dict[str, Any], episode_dir: Path) -> str:
    metadata = parse_research_metadata(research_markdown)
    title = select_cover_title(episode_dir, record)
    scene_clues = infer_cover_theme_clues(title, record, metadata)
    excerpt = extract_cover_key_excerpt(research_markdown)
    return (
        f"生成一张 1:1 方图播客正式封面背景，主题是“{title}”。"
        "风格基准是 assets/cover_benchmarks/ai_deepwater_chess_benchmark.png：只参考它的电影海报感、深色背景、强光影、蓝红对峙、棋盘/深水/资本博弈隐喻和缩略图冲击力，不要逐像素复制。"
        "画面必须像高端科技杂志封面或电影海报，不像 PPT 页面、信息图、数据看板、流程图或 UI 展板。"
        "优先使用具象隐喻和真实场景，不要只做抽象渐变、漂浮图标、普通海报底纹、复杂卡片布局或蓝色科技方块看板。"
        f"优先考虑这些画面方向：{'；'.join(scene_clues)}。"
        f"今天新闻的核心张力是：{excerpt or 'AI 产业链正在从基础设施、产品入口到交付结果同时被重排。'}"
        "视觉风格要深色背景、强光影、高对比、强中心视觉冲突、前景中景背景清晰，主体比信息密度更重要。"
        "可使用棋盘、深水、科技巨头剪影、AI 芯片、数据中心、资本筹码、算力机柜、云厂商阵营、机械手等隐喻元素，但必须组织成单一完整场景。"
        "下方 28-32% 保持偏暗、干净、有呼吸感，作为后续本地中文主标题叠字安全区；不要让人物、图标、棋子或高亮元素压住这一区域。"
        "不要播客麦克风、收音机、耳机、声波、播放器 UI、播放按钮、播客 App 卡片。"
        "严禁信息图、PPT 页面、数据看板、流程图、蓝色科技展板、多段小字排版、文字重叠、复杂卡片布局、窗口界面、任务面板、表格、箭头说明、标签云。"
        "不要把新闻内容逐条塞进画面；只保留一个主视觉冲突和少量可读的大形状。"
        "不要生成任何文字、数字、logo 或伪文字；建筑、屏幕、芯片、棋子、墙面、标牌、云朵、光效上都不能出现汉字、英文、数字或类似字形。"
        "所有可能带字的位置必须保持空白、纹理、反光、纯图形或无字图标；中文标题会由本地脚本后期叠加。"
    )


COMPANY_NAME_CANDIDATES = [
    "Google DeepMind",
    "OpenAI",
    "Anthropic",
    "Microsoft",
    "Meta",
    "Google",
    "Tesla",
    "NVIDIA",
    "英伟达",
    "Apple",
    "苹果",
    "Amazon",
    "AWS",
    "Oracle",
    "xAI",
    "DeepSeek",
    "阿里",
    "腾讯",
    "百度",
    "字节跳动",
    "华为",
    "小米",
    "BYD",
    "比亚迪",
    "小鹏",
    "大众",
    "普渡机器人",
    "Thrive Capital",
    "麦格理银行",
]


CODEX_COVER_KEYWORD_CANDIDATES = [
    "资本开支",
    "GPU军备",
    "裁员潮",
    "Agent",
    "硬件入口",
    "法务刹车",
    "机器人",
    "人才争夺",
    "具身智能",
    "终端入口",
    "云端入口",
    "监管前移",
    "算力互连",
    "内存短缺",
    "智能体并行",
    "供应链",
    "数据中心",
    "云服务",
    "资本博弈",
    "销售入口",
    "融资通道",
    "成本压力",
]


def _compact_cover_text(value: str, max_chars: int) -> str:
    cleaned = re.sub(r"[《》“”\"'`#*_]+", "", value)
    cleaned = re.sub(r"[，。！？；：、,.!?;:｜|]+", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned).strip("，。！？；：、,.!?;: ")
    return cleaned[:max_chars]


def _pick_companies_for_cover(script_text: str, extra_text: str = "", limit: int = 6) -> list[str]:
    combined = f"{script_text}\n{extra_text}"
    hits: list[tuple[int, int, str]] = []
    for name in COMPANY_NAME_CANDIDATES:
        count = len(re.findall(re.escape(name), combined, flags=re.IGNORECASE))
        if count:
            first = combined.lower().find(name.lower())
            hits.append((-count, first if first >= 0 else 999999, name))
    hits.sort()
    selected: list[str] = []
    seen_lower: set[str] = set()
    for _, _, name in hits:
        lowered = name.lower()
        if lowered in seen_lower:
            continue
        if name == "Google" and any(item.lower() == "google deepmind" for item in selected):
            continue
        selected.append(name)
        seen_lower.add(lowered)
        if len(selected) >= limit:
            break
    return selected


def _pick_keywords_for_cover(script_text: str, title: str, limit: int = 8) -> list[str]:
    combined = f"{title}\n{script_text}"
    scored: list[tuple[int, int, str]] = []
    for keyword in CODEX_COVER_KEYWORD_CANDIDATES:
        count = combined.count(keyword)
        if count:
            scored.append((-count, combined.find(keyword), keyword))
    scored.sort()
    selected = [item[2] for item in scored[:limit]]
    fallback = ["资本开支", "GPU军备", "云端入口", "供应链", "Agent", "监管前移"]
    for keyword in fallback:
        if keyword not in selected:
            selected.append(keyword)
        if len(selected) >= min(limit, 6):
            break
    return selected[:limit]


def _pick_center_question_for_cover(script_text: str, title: str, keywords: list[str]) -> str:
    questions = re.findall(r"[^。！？\n]{2,22}[？?]", script_text)
    for question in questions:
        cleaned = _compact_cover_text(question.replace("?", "？"), 16)
        if 4 <= len(cleaned) <= 16 and "什么是" not in cleaned:
            return cleaned
    if "供应链" in keywords or "算力" in title:
        return "谁在为AI买单？"
    if "监管" in keywords:
        return "谁会被提前刹车？"
    if "入口" in "".join(keywords):
        return "谁能拿下入口？"
    return "谁会被推出棋盘？"


def _pick_main_theme_for_cover(script_text: str, title: str) -> str:
    title_head = re.split(r"[，,：:｜|。！？]", title.strip(), maxsplit=1)[0]
    title_head = _compact_cover_text(title_head, 14)
    if 8 <= len(title_head) <= 14:
        return title_head
    title = _compact_cover_text(title, 14)
    if 8 <= len(title) <= 14:
        return title
    if "算力" in script_text and "供应链" in script_text:
        return "算力烧钱之后"
    if "裁员" in script_text:
        return "巨头开始下狠棋"
    if "入口" in script_text:
        return "入口战开到云上"
    return title or "AI竞争进入深水区"


def build_codex_cover_brief(episode_dir: Path) -> dict[str, Any]:
    script_path = episode_dir / "podcast_script.txt"
    if not script_path.exists():
        raise RuntimeError(f"缺少 podcast_script.txt: {episode_dir}")

    script_text = script_path.read_text(encoding="utf-8").strip()
    title = ""
    title_path = episode_dir / "title.txt"
    if title_path.exists():
        title = title_path.read_text(encoding="utf-8").strip()
    if not title:
        record_path = episode_dir / "record.json"
        if record_path.exists():
            title = str(read_json(record_path).get("标题", "")).strip()

    extra_parts: list[str] = []
    for name in ("search_results.json", "research.json", "title.txt"):
        path = episode_dir / name
        if path.exists():
            extra_parts.append(path.read_text(encoding="utf-8", errors="replace")[:4000])
    extra_text = "\n".join(extra_parts)
    companies = _pick_companies_for_cover(script_text, extra_text, limit=6)
    keywords = _pick_keywords_for_cover(script_text, title, limit=8)
    theme = _pick_main_theme_for_cover(script_text, title)
    center_question = _pick_center_question_for_cover(script_text, title, keywords)

    if not companies:
        companies = ["OpenAI", "Meta", "Google"]

    prompt = textwrap.dedent(
        f"""
        Use case: stylized-concept
        Asset type: square formal podcast cover, 1:1, at least 1024x1024.

        Editorial source:
        Generate the cover directly from the latest podcast_script.txt in {episode_dir.name}. Make a finished image-text integrated cover, not a background image and not a separate typography layer.

        Required readable text in the image:
        Top main title: {theme}
        Center question: {center_question}
        Company labels, max six: {" / ".join(companies[:6])}
        Keyword labels, short and sparse: {" / ".join(keywords[:8])}

        Text quality:
        Chinese text must be clean, large, complete, and readable at thumbnail size. English company names and Agent are allowed. Do not create fake Chinese, random numbers, pseudo text, dense small UI labels, or overlapping type. If text becomes difficult, reduce label count before adding small text.

        Visual direction:
        A cinematic commercial technology magazine cover and satirical editorial poster about today's AI power game. Use blue-red confrontation with left-right or upper-lower camps. Build one clear metaphor: deep-water chessboard, cold-war boardroom, server fortress, capital chips, robotic queues, chip wall, cloud gateway, regulatory gate, giant hand moving a chess piece, or data-center battlefield. The company names and keywords must be embedded as camp labels, chess-piece labels, building labels, or tactical panel labels, without logos.

        Composition:
        One dominant scene, strong foreground-midground-background depth, dramatic rim light, premium sci-fi editorial finish, anxious and clickable. Make the core story instantly readable: AI giant competition is entering a capital, compute, supply-chain, and entrance-control game. Avoid generic abstract tech wallpaper.

        Strict negative constraints:
        No podcast microphone, headphones, sound waves, play button, media-player UI, podcast app card, company logos, infographic, PPT page, data dashboard, flowchart, table, dense UI board, tiny caption overload, generic slogans such as AI未来 or 科技革命.
        """
    ).strip()

    return {
        "theme": theme,
        "center_question": center_question,
        "companies": companies[:6],
        "keywords": keywords[:8],
        "prompt": prompt,
    }


def find_latest_solo_episode_for_cover(date_key: str | None = None) -> Path:
    key = date_key or now_local().strftime("%Y%m%d")
    candidates: list[Path] = []
    episodes_root = ROOT_DIR / "episodes"
    for episode_dir in episodes_root.glob(f"{key}E*"):
        if not episode_dir.is_dir() or not (episode_dir / "podcast_script.txt").exists():
            continue
        flow_path = episode_dir / "flow_type.txt"
        if not flow_path.exists():
            continue
        if flow_path.read_text(encoding="utf-8").strip() == "solo":
            candidates.append(episode_dir)
    if not candidates:
        raise RuntimeError(f"未找到 {key} 的 solo episode podcast_script.txt")
    return sorted(candidates, key=lambda p: (p.name, (p / "podcast_script.txt").stat().st_mtime))[-1]


def resolve_episode_dir_for_cover(episode: str) -> Path:
    if not episode:
        return find_latest_solo_episode_for_cover()
    direct = ROOT_DIR / episode
    nested = ROOT_DIR / "episodes" / episode
    if nested.exists():
        return nested
    return direct


def finalize_codex_cover_image(episode_dir: Path, source_image: Path) -> dict[str, Any]:
    assert_solo_flow_type_file(episode_dir)
    if not source_image.exists():
        raise RuntimeError(f"Codex 封面源图不存在: {source_image}")
    final_cover = episode_dir / "cover.jpeg"
    candidate_cover = episode_dir / CODEX_COVER_CANDIDATE_NAME
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("缺少 Pillow，无法校验和转换 Codex 封面") from exc

    with Image.open(source_image) as image:
        width, height = image.size
        if width != height:
            raise RuntimeError(f"Codex 封面不是 1:1: {width}x{height}")
        if width < 1024:
            raise RuntimeError(f"Codex 封面尺寸不足: {width}x{height}")
        rgb = image.convert("RGB")
        rgb.save(final_cover, format="JPEG", quality=94, subsampling=0)
        rgb.save(candidate_cover, format="PNG")

    if final_cover.stat().st_size < 50 * 1024:
        raise RuntimeError(f"cover.jpeg 文件过小: {final_cover.stat().st_size} bytes")

    cover_meta = {
        "status": "success",
        "provider": CODEX_COVER_PROVIDER,
        "cover_path": str(final_cover),
        "generated_at": now_local().isoformat(timespec="seconds"),
    }
    write_json(episode_dir / "cover_generation.json", cover_meta)

    record_path = episode_dir / "record.json"
    record = read_json(record_path) if record_path.exists() else {}
    old_status = record.get("发布状态", "")
    record["封面路径"] = str(final_cover)
    if old_status == "cover_retry_pending":
        record["发布状态"] = "draft_assets_ready"
    write_json(record_path, record)
    return cover_meta


def prepare_codex_cover_for_episode(episode_dir: Path) -> dict[str, Any]:
    assert_solo_flow_type_file(episode_dir)
    brief = build_codex_cover_brief(episode_dir)
    write_text(episode_dir / "cover_codex_prompt.txt", brief["prompt"])
    pending = {
        "status": "codex_image_required",
        "provider": CODEX_COVER_PROVIDER,
        "episode_dir": str(episode_dir),
        "prompt_path": str(episode_dir / "cover_codex_prompt.txt"),
        "brief": {key: brief[key] for key in ("theme", "center_question", "companies", "keywords")},
    }
    write_json(episode_dir / "cover_generation.json", pending)
    return pending


def detect_problematic_text_in_cover(image_path: Path) -> dict[str, Any]:
    if shutil.which("tesseract") is None:
        return {
            "engine": "unavailable",
            "contains_text": False,
            "recognized_strings": [],
            "weak_text_hits": [],
            "raw_output": "",
            "warning": "tesseract not installed; skipped OCR validation",
        }
    proc = subprocess.run(
        ["tesseract", str(image_path), "stdout", "-l", "eng", "--psm", "6"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "未知错误"
        raise RuntimeError(f"封面文本检测失败: {detail}")

    raw_lines = [re.sub(r"\s+", " ", line).strip() for line in proc.stdout.splitlines()]
    recognized_strings: list[str] = []
    weak_text_hits: list[str] = []
    for line in raw_lines:
        if not line:
            continue
        alpha_words = re.findall(r"[A-Za-z]{3,}", line)
        medium_words = [word for word in alpha_words if len(word) >= 5]
        long_words = [word for word in alpha_words if len(word) >= 8]
        digit_groups = re.findall(r"\d{2,}", line)
        compact_alpha = "".join(alpha_words)
        alnum_chars = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", line)
        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", line))
        if (
            has_cjk
            or bool(long_words)
            or len(medium_words) >= 2
            or bool(digit_groups)
            or (len(compact_alpha) >= 18 and len(alpha_words) >= 4)
        ):
            recognized_strings.append(line)
        elif len(alnum_chars) >= 10 and len(alpha_words) >= 3:
            weak_text_hits.append(line)

    return {
        "engine": "tesseract-eng",
        "contains_text": bool(recognized_strings) or len(weak_text_hits) >= 4,
        "recognized_strings": recognized_strings[:8],
        "weak_text_hits": weak_text_hits[:8],
        "raw_output": "\n".join(raw_lines[:20]).strip(),
    }


def save_rejected_cover_candidate(
    output_path: Path,
    *,
    provider: str,
    attempt: int,
    validation: dict[str, Any],
) -> Path | None:
    if not output_path.exists():
        return None

    rejected_path = output_path.with_name(
        f"{output_path.stem}_rejected_{provider}_attempt{attempt}{output_path.suffix}"
    )
    meta_path = rejected_path.with_suffix(rejected_path.suffix + ".json")
    try:
        shutil.copy2(output_path, rejected_path)
        write_json(
            meta_path,
            {
                "status": "rejected",
                "provider": provider,
                "attempt": attempt,
                "saved_at": now_local().isoformat(timespec="seconds"),
                "source_image": str(output_path),
                "rejected_image": str(rejected_path),
                "validation": validation,
            },
        )
    except Exception as exc:
        print(f"[cover] 保存拒绝封面样本失败: {exc}", file=sys.stderr)
        return None
    return rejected_path


def generate_cover_with_gemini(prompt: str, output_path: Path) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 GEMINI_API_KEY。")
    if not GEMINI_VENV_PYTHON.exists():
        raise RuntimeError("Gemini 虚拟环境不存在，无法调用新版 google.genai SDK。")
    helper_script = ROOT_DIR / "gemini_genai_cover.py"
    retry_prompt_suffix = textwrap.dedent(
        """
        上一次结果出现了疑似文字或乱码。
        这一次必须完全移除所有文字元素：
        - 不要中文
        - 不要英文
        - 不要数字
        - 不要字母
        - 不要任何标题、标签、按钮文案、logo、符号串或伪文字
        - 不要信息图、PPT 页面、数据看板、流程图、蓝色科技展板、多段小字排版或复杂卡片布局
        - 所有原本可能放字的区域都改成电影海报式光影、深色留白或纯场景元素
        """
    ).strip()
    last_error: RuntimeError | None = None

    for attempt in range(1, MAX_COVER_GENERATION_ATTEMPTS + 1):
        attempt_prompt = prompt if attempt == 1 else f"{prompt}\n\n{retry_prompt_suffix}"
        output_path.unlink(missing_ok=True)
        proc = subprocess.run(
            [str(GEMINI_VENV_PYTHON), str(helper_script), str(output_path), attempt_prompt],
            env={**os.environ, "GEMINI_API_KEY": api_key},
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            last_error = RuntimeError(f"Gemini 图片生成失败: {proc.stderr or proc.stdout}")
            continue

        try:
            meta = json.loads(proc.stdout.strip())
        except json.JSONDecodeError as exc:
            last_error = RuntimeError(f"Gemini 返回不是合法 JSON: {proc.stdout}")
            continue

        if not output_path.exists():
            last_error = RuntimeError(f"Gemini 没有生成图片文件: {meta}")
            continue

        validation = detect_problematic_text_in_cover(output_path)
        meta["attempt"] = attempt
        meta["validation"] = validation
        if validation.get("contains_text"):
            recognized = ", ".join(validation.get("recognized_strings", [])[:4]) or "detected"
            rejected_path = save_rejected_cover_candidate(
                output_path,
                provider="gemini",
                attempt=attempt,
                validation=validation,
            )
            output_path.unlink(missing_ok=True)
            detail = f"；已保存复盘样本: {rejected_path.name}" if rejected_path else ""
            last_error = RuntimeError(f"封面包含疑似文字或乱码，已拒绝本次结果: {recognized}{detail}")
            continue
        return meta

    raise last_error or RuntimeError("Gemini 封面生成失败")


def generate_cover_with_gemini_web(prompt: str, output_path: Path) -> dict[str, Any]:
    helper_script = ROOT_DIR / "gemini_web_cover.py"
    retry_prompt_suffix = textwrap.dedent(
        """
        上一次结果出现了疑似文字或乱码。
        这一次必须完全移除所有文字元素：
        - 不要中文
        - 不要英文
        - 不要数字
        - 不要字母
        - 不要任何标题、标签、按钮文案、logo、符号串或伪文字
        - 不要信息图、PPT 页面、数据看板、流程图、蓝色科技展板、多段小字排版或复杂卡片布局
        - 所有原本可能放字的区域都改成电影海报式光影、深色留白或纯场景元素
        """
    ).strip()
    last_error: RuntimeError | None = None

    for attempt in range(1, MAX_COVER_GENERATION_ATTEMPTS + 1):
        attempt_prompt = prompt if attempt == 1 else f"{prompt}\n\n{retry_prompt_suffix}"
        output_path.unlink(missing_ok=True)
        proc = subprocess.run(
            [sys.executable, str(helper_script), str(output_path), attempt_prompt],
            env={**os.environ, "GEMINI_FORCE_FRESH": "1"},
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "未知错误"
            last_error = RuntimeError(f"Gemini 网页端封面生成失败: {detail}")
            continue
        if not output_path.exists():
            last_error = RuntimeError("Gemini 网页端未生成图片文件")
            continue

        validation = detect_problematic_text_in_cover(output_path)
        meta = {
            "provider": "gemini-web",
            "attempt": attempt,
            "validation": validation,
        }
        if validation.get("contains_text"):
            recognized = ", ".join(validation.get("recognized_strings", [])[:4]) or "detected"
            rejected_path = save_rejected_cover_candidate(
                output_path,
                provider="gemini_web",
                attempt=attempt,
                validation=validation,
            )
            output_path.unlink(missing_ok=True)
            detail = f"；已保存复盘样本: {rejected_path.name}" if rejected_path else ""
            last_error = RuntimeError(f"封面包含疑似文字或乱码，已拒绝本次结果: {recognized}{detail}")
            continue
        return meta

    raise last_error or RuntimeError("Gemini 网页端封面生成失败")


def generate_cover_with_qwen(prompt: str, output_path: Path) -> dict[str, Any]:
    """通过阿里云 DashScope Wanx 图片生成 API 生成封面背景（最末级兜底方案）。

    与 Gemini / MiniMax 一致：带反文字守卫，命中疑似文字则换 prompt 重刷，
    最多 MAX_QWEN_COVER_ATTEMPTS 次。
    """
    import urllib.request as _req
    import urllib.error as _err
    import time as _time

    api_key = os.getenv("QWEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 QWEN_API_KEY，无法使用 Wanx 图片生成")

    retry_prompt_suffix = textwrap.dedent(
        """
        上一次结果出现了疑似文字或乱码。
        这一次必须完全移除所有文字元素：
        - 不要中文
        - 不要英文
        - 不要数字
        - 不要字母
        - 不要任何标题、标签、按钮文案、logo、符号串或伪文字
        - 不要信息图、PPT 页面、数据看板、流程图、蓝色科技展板、多段小字排版或复杂卡片布局
        - 所有原本可能放字的区域都改成电影海报式光影、深色留白或纯场景元素
        """
    ).strip()

    submit_url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
    opener = _req.build_opener(_req.ProxyHandler({}))
    last_error: RuntimeError | None = None

    for attempt in range(1, MAX_QWEN_COVER_ATTEMPTS + 1):
        attempt_prompt = prompt if attempt == 1 else f"{prompt}\n\n{retry_prompt_suffix}"
        output_path.unlink(missing_ok=True)

        # 提交异步任务
        payload = {
            "model": "wanx-v1",
            "input": {"prompt": attempt_prompt},
            "parameters": {"size": "1024*1024", "n": 1},
        }
        data = json.dumps(payload, ensure_ascii=False).encode()
        req = _req.Request(submit_url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-DashScope-Async": "enable",
        })
        try:
            resp = opener.open(req, timeout=30)
            body = json.loads(resp.read())
        except _err.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            last_error = RuntimeError(f"Wanx 任务提交失败: HTTP {exc.code} {detail}")
            continue
        except Exception as exc:
            last_error = RuntimeError(f"Wanx 任务提交失败: {exc}")
            continue

        task_id = body.get("output", {}).get("task_id")
        if not task_id:
            last_error = RuntimeError(f"Wanx 未返回 task_id: {body}")
            continue

        # 轮询等待完成（最多 120 秒）
        poll_url = f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
        image_url = ""
        task_error: RuntimeError | None = None
        for _ in range(24):
            _time.sleep(5)
            poll_req = _req.Request(poll_url, headers={"Authorization": f"Bearer {api_key}"})
            resp = opener.open(poll_req, timeout=30)
            status_body = json.loads(resp.read())
            task_status = status_body.get("output", {}).get("task_status", "")
            if task_status == "SUCCEEDED":
                results = status_body["output"].get("results", [])
                if not results or not results[0].get("url"):
                    task_error = RuntimeError("Wanx 成功但无图片 URL")
                    break
                image_url = results[0]["url"]
                break
            elif task_status == "FAILED":
                task_error = RuntimeError(f"Wanx 图片生成失败: {status_body}")
                break
        else:
            task_error = RuntimeError(f"Wanx 图片生成超时 (task_id={task_id})")

        if task_error is not None:
            last_error = task_error
            continue

        # 下载图片
        try:
            img_req = _req.Request(image_url)
            img_resp = opener.open(img_req, timeout=60)
            output_path.write_bytes(img_resp.read())
        except Exception as exc:
            last_error = RuntimeError(f"Wanx 图片下载失败: {exc}")
            continue

        validation = detect_problematic_text_in_cover(output_path)
        if validation.get("contains_text"):
            recognized = ", ".join(validation.get("recognized_strings", [])[:4]) or "detected"
            rejected_path = save_rejected_cover_candidate(
                output_path,
                provider="qwen_wanx",
                attempt=attempt,
                validation=validation,
            )
            output_path.unlink(missing_ok=True)
            detail = f"；已保存复盘样本: {rejected_path.name}" if rejected_path else ""
            last_error = RuntimeError(f"封面包含疑似文字或乱码，已拒绝本次结果: {recognized}{detail}")
            continue

        return {
            "provider": "qwen-wanx",
            "attempt": attempt,
            "task_id": task_id,
            "image_url": image_url,
            "validation": validation,
        }

    raise last_error or RuntimeError("Wanx 封面生成失败")


def generate_cover_with_minimax(prompt: str, output_path: Path) -> dict[str, Any]:
    import urllib.request as _req
    import urllib.error as _err

    api_key = os.getenv("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 MINIMAX_API_KEY，无法使用 MiniMax 图片生成")

    retry_prompt_suffix = textwrap.dedent(
        """
        上一次结果出现了疑似文字或乱码。
        这一次必须完全移除所有文字元素：
        - 不要中文
        - 不要英文
        - 不要数字
        - 不要字母
        - 不要任何标题、标签、按钮文案、logo、符号串或伪文字
        - 不要信息图、PPT 页面、数据看板、流程图、蓝色科技展板、多段小字排版或复杂卡片布局
        - 所有原本可能放字的区域都改成电影海报式光影、深色留白或纯场景元素
        """
    ).strip()
    last_error: RuntimeError | None = None

    for attempt in range(1, MAX_MINIMAX_COVER_ATTEMPTS + 1):
        attempt_prompt = prompt if attempt == 1 else f"{prompt}\n\n{retry_prompt_suffix}"
        output_path.unlink(missing_ok=True)
        payload = {
            "model": MINIMAX_IMAGE_MODEL,
            "prompt": attempt_prompt,
            "aspect_ratio": "1:1",
            "response_format": "base64",
            "n": 1,
            "prompt_optimizer": False,
            "aigc_watermark": False,
        }
        req = _req.Request(
            "https://api.minimaxi.com/v1/image_generation",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with _req.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except _err.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            last_error = RuntimeError(f"MiniMax 图片生成失败: HTTP {exc.code} {detail}")
            continue
        except Exception as exc:
            last_error = RuntimeError(f"MiniMax 图片生成失败: {exc}")
            continue

        base_resp = body.get("base_resp", {})
        if base_resp.get("status_code", 0) != 0:
            last_error = RuntimeError(f"MiniMax 图片生成失败: {base_resp.get('status_msg', body)}")
            continue

        image_urls = body.get("data", {}).get("image_urls") or []
        data = body.get("data", {}) or {}
        base64_candidates: list[str] = []
        for key in ("image_base64", "images_base64", "image_base64_list", "base64", "images"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                base64_candidates.append(value.strip())
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        base64_candidates.append(item.strip())
                    elif isinstance(item, dict):
                        for nested_key in ("image_base64", "base64", "data"):
                            nested_value = item.get(nested_key)
                            if isinstance(nested_value, str) and nested_value.strip():
                                base64_candidates.append(nested_value.strip())

        image_url = str(image_urls[0]).strip() if image_urls else ""
        if base64_candidates:
            raw_base64 = base64_candidates[0]
            if "," in raw_base64 and raw_base64.startswith("data:"):
                raw_base64 = raw_base64.split(",", 1)[1]
            try:
                output_path.write_bytes(base64.b64decode(raw_base64))
            except Exception as exc:
                last_error = RuntimeError(f"MiniMax base64 图片解码失败: {exc}")
                continue
        elif image_url:
            try:
                with _req.urlopen(_req.Request(image_url), timeout=120) as img_resp:
                    output_path.write_bytes(img_resp.read())
            except Exception as exc:
                last_error = RuntimeError(f"MiniMax 图片下载失败: {exc}")
                continue
        else:
            last_error = RuntimeError(f"MiniMax 未返回可用图片数据: {body}")
            continue

        validation = detect_problematic_text_in_cover(output_path)
        if validation.get("contains_text"):
            recognized = ", ".join(validation.get("recognized_strings", [])[:4]) or "detected"
            rejected_path = save_rejected_cover_candidate(
                output_path,
                provider="minimax",
                attempt=attempt,
                validation=validation,
            )
            output_path.unlink(missing_ok=True)
            detail = f"；已保存复盘样本: {rejected_path.name}" if rejected_path else ""
            last_error = RuntimeError(f"封面包含疑似文字或乱码，已拒绝本次结果: {recognized}{detail}")
            continue

        return {
            "provider": "minimax",
            "model": MINIMAX_IMAGE_MODEL,
            "attempt": attempt,
            "image_url": image_url,
            "validation": validation,
        }

    raise last_error or RuntimeError("MiniMax 封面生成失败")


def compose_cover_with_local_title(background_path: Path, output_path: Path, title: str) -> None:
    helper_script = ROOT_DIR / "compose_cover_title.py"
    if not helper_script.exists():
        raise RuntimeError("本地封面叠字脚本缺失")
    if not GEMINI_VENV_PYTHON.exists():
        raise RuntimeError("Gemini 虚拟环境不存在，无法调用本地封面叠字脚本。")

    proc = subprocess.run(
        [str(GEMINI_VENV_PYTHON), str(helper_script), str(background_path), str(output_path), title],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "未知错误"
        raise RuntimeError(f"本地封面叠字失败: {detail}")
    if not output_path.exists():
        raise RuntimeError("本地封面叠字未生成输出文件")


def regenerate_cover_for_episode(
    episode_dir: Path,
    provider: str = "gemini",
    codex_source_image: Path | None = None,
) -> dict[str, Any]:
    if provider in ("codex", CODEX_COVER_PROVIDER):
        if codex_source_image:
            return finalize_codex_cover_image(episode_dir, codex_source_image)
        return prepare_codex_cover_for_episode(episode_dir)

    record = read_json(episode_dir / "record.json")
    research_markdown = (episode_dir / "research.md").read_text(encoding="utf-8")
    overlay_title = select_cover_title(episode_dir, record)
    prompt = build_cover_background_prompt(research_markdown, record, episode_dir)
    original_cover = episode_dir / "cover.jpeg"
    if original_cover.exists():
        backup = episode_dir / "cover_listenhub_original.jpeg"
        if not backup.exists():
            shutil.copy2(original_cover, backup)
    background_output = episode_dir / "cover_background.png"
    final_cover = episode_dir / "cover.jpeg"
    try:
        # Gemini API → MiniMax → Qwen-Wanx 文生图降级链
        try:
            meta = generate_cover_with_gemini(prompt, background_output)
            meta["method"] = "genai_api"
        except Exception as api_exc:
            print(f"[cover] gemini-api 失败: {api_exc}", file=sys.stderr)
            try:
                meta = generate_cover_with_minimax(prompt, background_output)
                meta["method"] = "minimax_image_fallback"
            except Exception as minimax_exc:
                print(f"[cover] minimax 失败: {minimax_exc}", file=sys.stderr)
                meta = generate_cover_with_qwen(prompt, background_output)
                meta["method"] = "qwen_wanx_fallback"
        compose_cover_with_local_title(background_output, final_cover, overlay_title)
    except Exception as exc:
        failure_meta = {
            "status": "failed",
            "error": str(exc),
            "prompt": prompt,
            "overlay_title": overlay_title,
            "provider_requested": provider,
            "cover_path": str(original_cover) if original_cover.exists() else "",
        }
        write_json(episode_dir / "cover_generation.json", failure_meta)
        return failure_meta
    meta["status"] = "success"
    meta["prompt"] = prompt
    meta["render_mode"] = "background_plus_local_title"
    meta["overlay_title"] = overlay_title
    meta["provider_requested"] = provider
    meta["background_path"] = str(background_output)
    meta["cover_path"] = str(final_cover)
    write_json(episode_dir / "cover_generation.json", meta)
    record["封面路径"] = str(final_cover)
    write_json(episode_dir / "record.json", record)
    return meta


def _filter_conflicting_trends(
    trend_titles: list[str],
    recent_context: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """P2: 去除与近期历史重叠的趋势，避免 prompt 同时下达"别讲"和"必须讲"。"""
    if not recent_context:
        return trend_titles, []
    recent_note = build_recent_coverage_note(recent_context)
    recent_lower = recent_note.lower()
    # 通用词不参与判断
    skip_words = {"发生了什么", "为什么重要", "意味着什么", "超级周期", "告急", "正在", "已经", "AI", "人工智能"}
    clean: list[str] = []
    avoided: list[str] = []
    for t in trend_titles:
        words = [w for w in re.split(r"[：:——\-\s／/，。]", t)
                 if len(w) >= 4 and w not in skip_words]
        if any(w.lower() in recent_lower for w in words):
            avoided.append(t)
        else:
            clean.append(t)
    return clean, avoided


def _compress_research_extractive(research_markdown: str) -> str:
    """LLM 不可用时的回退：只提取今日结论 + 每条趋势的发生了什么。"""
    lines = research_markdown.splitlines()
    sections: list[str] = []
    current: list[str] = []
    capture = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## 今日结论") or stripped.startswith("### 趋势"):
            if current:
                sections.append("\n".join(current))
            current = [stripped]
            capture = True
        elif capture and stripped.startswith("**为什么重要"):
            sections.append("\n".join(current))
            current = []
            capture = False
        elif capture and stripped.startswith("## ") and "今日结论" not in stripped and "趋势" not in stripped:
            sections.append("\n".join(current))
            current = []
            capture = False
        elif capture:
            current.append(stripped)
    if current:
        sections.append("\n".join(current))
    return "\n\n".join(s for s in sections if s.strip())[:6000]


def load_today_solo_basis(episode: str) -> str:
    """读取当天 E1 单口的终版口播稿，作为双人对话设计稿的事实底稿。

    E1 稿件经过 codex PASS + Claude 复核双审，事实和边界声明是当天最可靠的版本；
    双人链路自己的研究稿没有这道校验，历史上出现过把"报道称"加码成既成事实的问题。
    """
    date_key = episode[:8]
    solo_dir = ROOT_DIR / "episodes" / f"{date_key}E1"
    script_path = solo_dir / "podcast_script.txt"
    if script_path.exists():
        text = script_path.read_text(encoding="utf-8").strip()
        if len(text) > 500:
            return text
    research_path = solo_dir / "research.md"
    if research_path.exists():
        text = research_path.read_text(encoding="utf-8").strip()
        if len(text) > 500:
            return text
    return ""


SOLO_BASIS_BRIEF_RULES = """
【事实底稿（最高优先级）】
下面是今天已通过双重审稿的单口节目终稿。它的事实和措辞强度是本次设计稿的上限：
- 凡是设计稿涉及底稿覆盖过的新闻，事实和措辞强度一律以底稿为准；底稿与研究稿冲突时以底稿为准。
- 底稿对某条新闻做了边界声明（如"报道未经官方证实""不是正式招股书""不等于已大规模替代"），
  设计稿必须保留同等保留措辞，禁止把"据报道/预计/正在准备"加码成"已确认/已提交/已完成"。
- 把关键边界声明安排进 B 的追问或 A 的提醒台词里，让对话自带核查感。
单口终稿底稿：
"""


DUAL_DIFFERENTIATION_RULES = """
【与 E1 单口的差异化（重要）】
E1 是新闻播报体，E2 是双人对谈体。两档话题可以重叠，但绝不能是同一内容换个形式再讲一遍。
- 错开优先：三条主线尽量挑 E1 没展开、或只一笔带过的角度、子话题、当事人视角。
- 错不开就讲深度：热点就这些、话题不可避免和 E1 撞时，靠对谈把同一件事做深——
  · 用第一人称体验吐槽（如"我充了钱做了个 app，结果模型说下架就下架了"）；
  · 打生活化比方，把抽象的产业/技术问题翻译成人话；
  · 引入多方甚至相反的观点（如有人把模型迭代说得很玄，也有从业者认为这只是产业发展里的自然规律、不必神化），让 B 站到不同立场上追问；
  · 做延伸推演（"这事再往下走会怎样""对开发者和普通用户分别意味着什么"）。
- 判断标准：E2 听完要有 E1 没给到的信息增量或情绪增量，不能只是把 E1 的结论复述一遍。
"""


def build_dialog_brief(
    research_markdown: str,
    trend_titles: list[str],
    recent_context: dict[str, Any] | None = None,
    solo_basis: str = "",
) -> str:
    """P3+P4: 用 LLM 把研究稿压缩为对话设计稿，供 ListenHub 直接演绎。

    参照 20260524E_double_llm_basics 的 query 风格：
    有开头钉子台词、每条主线给具体事实+可以这样说+B的追问方向。
    solo_basis 是当天 E1 的双审终稿，提供时作为事实与措辞强度的上限。
    """
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    qwen_key = os.getenv("QWEN_API_KEY", "").strip()
    if not deepseek_key and not qwen_key:
        return _compress_research_extractive(research_markdown)

    clean_trends, avoided_trends = _filter_conflicting_trends(trend_titles, recent_context)
    avoided_note = ""
    if avoided_trends:
        avoided_note = (
            "\n\n以下趋势在近期已播报过，不要放进设计稿，请从研究稿里另选角度：\n"
            + "\n".join(f"- {t}" for t in avoided_trends)
        )

    system_prompt = (
        "你是专业播客策划人，负责把 AI 日报研究稿改写成【对话设计稿】，供 ListenHub AI 生成双人新闻播客。\n"
        "对话设计稿不是完整稿件，是框架：告诉 ListenHub 每个主线说什么、可以怎么说、B 该从哪里追问。\n"
        "输出纯文本，不使用 markdown 加粗或列表符号。"
    )
    user_content = f"""请把下面的研究稿改写成对话设计稿。

输出结构（依次）：

今日标题
一行，格式"YYYY年M月：XXXX"，冒号后8到14字，有冲突感或悬念。

核心判断
一句话主线，带判断不带描述，不超过40字。

开头钉子
4到6轮实际对话台词，直接可用，不要用"今天我们来聊"这种平铺开头。
风格参考：
A：我发现一个怪事。
B：什么怪事？
A：（具体的反直觉事实）

第一条主线（标题）
必须覆盖的事实：3条，每条含公司名或具体数字
可以这样说：2到3句口语句，单句不超过20字，供 ListenHub 直接复用
B的追问方向：2条追问，角度必须比A多一层

第二条主线（标题）
（同上结构）

第三条主线（标题）
（同上结构）

结尾约束
最后60秒必须双人互相收束。任何单个 speaker 不超过80字。
禁止哲学独白式收尾。结尾落到一个具体的、有判断力的问题。

总字数控制在 1800 到 2500 字。禁止包含深度研究、Product Hunt、信息源列表。{avoided_note}
{(DUAL_DIFFERENTIATION_RULES + SOLO_BASIS_BRIEF_RULES + solo_basis[:6000]) if solo_basis else ""}
研究稿：
{research_markdown[:8000]}"""

    candidates: list[tuple[str, str, str, str]] = []
    if deepseek_key:
        candidates.append((
            deepseek_key,
            "https://api.deepseek.com/v1",
            os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            "dialog_brief deepseek",
        ))
    if qwen_key:
        candidates.append((
            qwen_key,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            os.getenv("QWEN_PLUS_MODEL", "qwen-plus"),
            "dialog_brief qwen-plus",
        ))
        candidates.append((
            qwen_key,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            os.getenv("QWEN_MODEL", "qwen-turbo"),
            "dialog_brief qwen-turbo",
        ))

    errors: list[str] = []
    for api_key, base_url, model, label in candidates:
        try:
            result = _openai_compatible_text(
                api_key=api_key,
                base_url=base_url,
                model=model,
                system_prompt=system_prompt,
                user_content=user_content,
                label=label,
                max_tokens=3000,
            )
            if result and len(result) > 500:
                return result
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            continue

    print(f"[build_dialog_brief] LLM 全链失败，回退提取式压缩: {errors}", file=sys.stderr)
    return _compress_research_extractive(research_markdown)


def build_listenhub_query(
    search_payload: dict[str, Any],
    research_markdown: str,
    recent_context: dict[str, Any] | None = None,
    time_target: str = "8 到 12 分钟",
    solo_basis: str = "",
) -> str:
    metadata = parse_research_metadata(research_markdown)
    trend_titles = metadata["trend_titles"] or ["今天的 AI 三大趋势"]
    recent_note = build_recent_coverage_note(recent_context or {"episodes": []})
    title = metadata["title"]

    dialog_brief = build_dialog_brief(research_markdown, trend_titles, recent_context, solo_basis=solo_basis)
    fact_discipline_note = ""
    if solo_basis:
        fact_discipline_note = (
            "\n【事实纪律（最高优先级）】\n"
            "对话中的事实与措辞强度不得超过设计稿：设计稿写\"据报道/预计/正在准备\"，"
            "对话不得说成\"已确认/已提交/已完成\"。设计稿里的边界提醒必须在对话中保留。\n"
        )

    prompt = f"""请生成一期中文双人 AI 播客，风格目标：两个真实的人在聊天，不是在念稿。听众应该感觉是偶然听到两个懂 AI 的朋友在随口聊这周的事。

【最高优先级：口语化要求】
每句话不超过 20 个汉字。长句拆成短句，用句号断开，不要用"而且、因此、从而、不仅如此"把多个意思串成一句。

禁止这些书面感句式（出现即违规）：
- "从某种角度来看，这说明..."
- "这其实是一个非常值得关注的..."
- "如果把这些新闻放在一起看..."
- "这意味着在未来的发展中..."
- "总体而言，..."  / "换句话说，..."
- "这一现象背后的深层逻辑是..."
- "不难发现，...正在经历..."
- "说得太对了" / "完全正确" / "非常好的问题"

提倡这些口语句式：
- "你看，这件事有意思的地方就在这里。"
- "等等，这个我没太懂，你是说...？"
- "对，就是这个感觉。说白了就是..."
- "但问题是——这钱从哪来？"
- "我一开始也觉得是这样，但后来想了一下..."
- "这个比喻挺好的，就像你去超市..."
- "所以本质上是...对吗？"
- "这里有个地方我不确定，..."

对话节奏示范（正确）：
A：Levie那条推文，你看到了吗？
B：看了。他用了个生命科学公司的例子。
A：对。他的意思是——AI没有消灭对工程师的需求，反而放大了。
B：放大？怎么放大的？
A：就是，以前一个团队做10个功能，现在同样的人能做50个。活儿多了，不是人少了。
B：哦，所以需求是跟着扩的，不是缩的。但这个逻辑有个前提——得有那么多需求才行。
A：这就是关键。Levie认为需求是无限的，企业之前是被成本卡住了。
B：明白了。就像以前请不起厨师，现在有了料理机，你会做更多菜，不是少做菜。

对话节奏示范（错误，禁止）：
A：首先，让我们来看第一个趋势。根据Box CEO Aaron Levie的深度分析，AI正在从根本上改变软件工程师的工作模式。
B：是的，这个观点非常深刻。这说明了AI在当今时代的重要意义，以及它如何推动整个行业的结构性变革。

【结尾硬约束（最高优先级，与口语化并列）】
最后 60 秒必须双人互相收束。任何单个 speaker 不超过 80 字。
禁止最后一大段哲学独白。
结尾必须落到一个具体的、有判断力的问题，不能是"让我们拭目以待"这类套话。

风格要求：
1. 开头先抛一个钉子，不要用"最近 AI 新闻又开始密集"这类固定句式，也不要和前几期明显重复。
2. 全文是双人对话，不要主持词标签，不要 markdown，不要列表。
3. 主持人A负责主线、结论和节奏；主持人B负责追问、拆解、类比、轻吐槽，把复杂内容翻译成人话。
4. 每轮尽量 2 到 4 句。每 60 秒必须出现一次追问、打断、补充、反问、类比或吐槽。
5. B 不能只复述 A 的话，要加一个新角度或提一个 A 没提到的疑问。
6. 第一次出现英文缩写、专业术语时，必须立刻用中文解释（不超过一句话）。
7. 结论先行，例子跟后，背景最后。不要先铺垫五句再说结论。
8. 可以加入情绪词：有意思的是、这就有点微妙了、听起来很玄但其实很简单。不要加 TTS 停顿标签。
9. 必须在对话里点明具体公司名和产品名，严禁使用"一家领先的AI公司"、"巨头公司"等模糊指代。
10. 时长目标 {time_target}。
11. 标题格式 "YYYY年M月：XXXXXXXX"，冒号后 8 到 14 个汉字，有冲突感、态度或悬念。
12. 好标题正例：大模型退潮，小参数模型凭什么反杀？/ Meta 砸千亿搞 AI，但 Llama 越来越不开源了 / 万亿融资的背面是万人裁员。
13. 绝对禁止这些空泛标题：AI全产业链重新排位、AI生态持续演进、AI节点升温、AI关键变化、AI基础设施继续上强度。

{fact_discipline_note}
历史避重参考：
{recent_note}

今日对话设计稿：
{dialog_brief}"""
    return prompt.strip()
def summarize_sources(search_payload: dict[str, Any]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for item in search_payload.get("sources", []):
        title = item.get("title") or item.get("url") or "Untitled"
        url = item.get("url") or ""
        if url:
            sources.append({"title": title, "url": url})
    return sources[:12]


def merge_search_payloads(tavily_data: dict[str, Any], brave_data: dict[str, Any]) -> dict[str, Any]:
    brave_results = brave_data.get("web", {}).get("results", [])
    filtered_out = (tavily_data.get("filtered_out", []) or []) + (brave_data.get("filtered_out", []) or [])
    result = {
        "generated_at": now_local().isoformat(),
        "tavily_answer": tavily_data.get("tavily_answer") or tavily_data.get("answer", ""),
        "tavily_results": tavily_data.get("tavily_results") or tavily_data.get("results", []),
        "brave_results": brave_results,
        "queries": tavily_data.get("queries", []),
        "dynamic_queries": tavily_data.get("dynamic_queries", []),
        "brave_queries": brave_data.get("queries", []),
        "brave_dynamic_queries": brave_data.get("dynamic_queries", []),
    }
    if filtered_out:
        result["filtered_out"] = filtered_out[:20]
        result["filtered_counts"] = summarize_filtered_reasons(filtered_out)
    return result


def _follow_builder_normalize_url(url: Any) -> str:
    return str(url or "").strip().strip("<>").rstrip(").,]>")


def _follow_builder_transcript_excerpt(text: Any, limit: int = FOLLOW_BUILDER_TRANSCRIPT_CHAR_LIMIT) -> str:
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    return collapsed[:limit]


def _follow_builder_generated_at(payload: Mapping[str, Any] | None) -> dt.datetime | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("feed_generated_at", "generatedAt", "generated_at"):
        raw = str(payload.get(key) or "").strip()
        if not raw:
            continue
        parsed = parse_result_datetime(raw)
        if parsed:
            return parsed
    return None


def _persist_follow_builder_feed_bundle(feed_bundle: dict[str, Any]) -> None:
    ensure_dir(FOLLOW_BUILDER_DIR)
    ensure_dir(FOLLOW_BUILDER_ARTIFACTS_DIR)
    generated_at = str(feed_bundle.get("generatedAt") or now_local().isoformat())
    x_items = list(feed_bundle.get("x", []) or [])
    blog_items = list(feed_bundle.get("blogs", []) or [])
    podcast_items = list(feed_bundle.get("podcasts", []) or [])

    write_json(
        FOLLOW_BUILDER_FEED_X_PATH,
        {
            "generatedAt": generated_at,
            "x": x_items,
            "stats": {
                "xBuilders": len(x_items),
                "totalTweets": sum(len(builder.get("tweets", []) or []) for builder in x_items),
            },
        },
    )
    write_json(
        FOLLOW_BUILDER_FEED_BLOGS_PATH,
        {
            "generatedAt": generated_at,
            "blogs": blog_items,
            "stats": {"blogPosts": len(blog_items)},
        },
    )
    write_json(
        FOLLOW_BUILDER_FEED_PODCASTS_PATH,
        {
            "generatedAt": generated_at,
            "podcasts": podcast_items,
            "stats": {"podcastEpisodes": len(podcast_items)},
        },
    )
    write_json(FOLLOW_BUILDER_FEED_BUNDLE_PATH, feed_bundle)


def refresh_follow_builder_feed_bundle() -> dict[str, Any] | None:
    if not FOLLOW_BUILDER_PREPARE_DIGEST_SCRIPT.exists():
        return None
    try:
        proc = subprocess.run(
            ["node", FOLLOW_BUILDER_PREPARE_DIGEST_SCRIPT.name],
            cwd=str(FOLLOW_BUILDER_PREPARE_DIGEST_SCRIPT.parent),
            capture_output=True,
            text=True,
            timeout=FOLLOW_BUILDER_REFRESH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        print(f"[follow-builder] 刷新最新 feed 失败: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()[:300]
        print(f"[follow-builder] prepare-digest.js 执行失败: {stderr}", file=sys.stderr)
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"[follow-builder] prepare-digest.js 输出不是合法 JSON: {exc}", file=sys.stderr)
        return None
    if payload.get("status") == "error":
        print(f"[follow-builder] prepare-digest.js 返回错误: {payload.get('message', '')}", file=sys.stderr)
        return None
    if not any(payload.get(key) for key in ("x", "blogs", "podcasts")):
        print("[follow-builder] 最新 feed 为空，保留现有本地数据", file=sys.stderr)
        return None
    try:
        _persist_follow_builder_feed_bundle(payload)
    except Exception as exc:
        print(f"[follow-builder] 写入最新 feed 失败: {exc}", file=sys.stderr)
        return None
    print(
        "[follow-builder] 已刷新最新 feed: "
        f"x={len(payload.get('x', []) or [])}, "
        f"blogs={len(payload.get('blogs', []) or [])}, "
        f"podcasts={len(payload.get('podcasts', []) or [])}",
        file=sys.stderr,
    )
    return payload


def _latest_follow_builder_feed_timestamp() -> dt.datetime | None:
    latest: dt.datetime | None = None
    for path in (FOLLOW_BUILDER_FEED_X_PATH, FOLLOW_BUILDER_FEED_BLOGS_PATH, FOLLOW_BUILDER_FEED_PODCASTS_PATH):
        if not path.exists():
            continue
        try:
            payload = read_json(path)
        except Exception:
            payload = {}
        candidate = _follow_builder_generated_at(payload)
        if not candidate:
            candidate = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def _load_follow_builder_digest_items() -> list[dict[str, Any]]:
    if not FOLLOW_BUILDER_DIGEST_ARTIFACT_PATH.exists():
        return []
    try:
        digest_payload = read_json(FOLLOW_BUILDER_DIGEST_ARTIFACT_PATH)
    except Exception as exc:
        print(f"[follow-builder] 读取 latest-digest.json 失败: {exc}", file=sys.stderr)
        return []
    raw_items = digest_payload.get("items", [])
    if not isinstance(raw_items, list) or not raw_items:
        return []

    digest_ts = _follow_builder_generated_at(digest_payload)
    if not digest_ts:
        digest_ts = dt.datetime.fromtimestamp(FOLLOW_BUILDER_DIGEST_ARTIFACT_PATH.stat().st_mtime, tz=dt.timezone.utc)
    latest_feed_ts = _latest_follow_builder_feed_timestamp()
    if latest_feed_ts and digest_ts + dt.timedelta(minutes=1) < latest_feed_ts:
        print("[follow-builder] digest 产物早于最新 feed，回退到 feed 文件", file=sys.stderr)
        return []

    items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        sanitized = sanitize_search_item(
            {
                **item,
                "_follow_builder": True,
                "provider": str(item.get("provider") or "follow_builder_digest"),
                "source_query": str(item.get("source_query") or "follow_builder_digest"),
            }
        )
        if sanitized.get("title") and sanitized.get("url"):
            items.append(sanitized)
    if items:
        print(f"[follow-builder] 优先使用 digest 产物 {len(items)} 条", file=sys.stderr)
    return items


def _load_follow_builder_items_from_feeds() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    if FOLLOW_BUILDER_FEED_BLOGS_PATH.exists():
        try:
            blogs_data = read_json(FOLLOW_BUILDER_FEED_BLOGS_PATH)
            for blog in blogs_data.get("blogs", blogs_data.get("articles", [])):
                title = str(blog.get("title", "")).strip()
                url = _follow_builder_normalize_url(blog.get("url", ""))
                content = str(blog.get("description") or blog.get("content") or "")[:2000]
                if not title or not url:
                    continue
                items.append(
                    sanitize_search_item(
                        {
                            "title": title,
                            "url": url,
                            "content": content,
                            "published_date": str(blog.get("publishedAt") or ""),
                            "score": 0.55,
                            "source_query": "follow_builder_blog",
                            "_follow_builder": True,
                            "provider": "follow_builder_feed",
                            "feed_name": str(blog.get("name") or ""),
                        }
                    )
                )
        except Exception as exc:
            print(f"[follow-builder] 读取 feed-blogs.json 失败: {exc}", file=sys.stderr)

    if FOLLOW_BUILDER_FEED_X_PATH.exists():
        try:
            x_data = read_json(FOLLOW_BUILDER_FEED_X_PATH)
            for builder in x_data.get("x", []):
                handle = str(builder.get("handle", "")).strip()
                name = str(builder.get("name", "")).strip()
                bio = str(builder.get("bio", "")).strip()
                title_prefix = name or (f"@{handle}" if handle else "X")
                if handle and name:
                    title_prefix = f"{name} (@{handle})"
                for tweet in builder.get("tweets", []):
                    text = str(tweet.get("text", "")).strip()
                    tweet_url = _follow_builder_normalize_url(tweet.get("url", ""))
                    likes = int(tweet.get("likes", 0) or 0)
                    created = str(tweet.get("createdAt", "") or "")
                    if not text or not tweet_url:
                        continue
                    if likes < 50 and count_ai_signals(f"{text}\n{bio}".lower()) < 2:
                        continue
                    content = text
                    if bio:
                        content = f"{text}\n\nBuilder bio: {bio}"
                    items.append(
                        sanitize_search_item(
                            {
                                "title": f"{title_prefix}: {text[:100]}",
                                "url": tweet_url,
                                "content": content,
                                "published_date": created,
                                "score": min(0.3 + likes / 1000, 0.8),
                                "source_query": "follow_builder_x",
                                "_follow_builder": True,
                                "provider": "follow_builder_feed",
                                "feed_name": name or handle,
                            }
                        )
                    )
        except Exception as exc:
            print(f"[follow-builder] 读取 feed-x.json 失败: {exc}", file=sys.stderr)

    if FOLLOW_BUILDER_FEED_PODCASTS_PATH.exists():
        try:
            podcasts_data = read_json(FOLLOW_BUILDER_FEED_PODCASTS_PATH)
            for podcast in podcasts_data.get("podcasts", []):
                show_name = str(podcast.get("name", "")).strip()
                episode_title = str(podcast.get("title", "")).strip()
                url = _follow_builder_normalize_url(podcast.get("url", ""))
                if not episode_title or not url:
                    continue
                title = f"{show_name}: {episode_title}" if show_name and show_name not in episode_title else episode_title
                content = _follow_builder_transcript_excerpt(
                    podcast.get("description") or podcast.get("transcript") or episode_title
                )
                items.append(
                    sanitize_search_item(
                        {
                            "title": title,
                            "url": url,
                            "content": content,
                            "published_date": str(podcast.get("publishedAt") or ""),
                            "score": 0.62,
                            "source_query": "follow_builder_podcast",
                            "_follow_builder": True,
                            "provider": "follow_builder_feed",
                            "feed_name": show_name,
                        }
                    )
                )
        except Exception as exc:
            print(f"[follow-builder] 读取 feed-podcasts.json 失败: {exc}", file=sys.stderr)

    return items


def load_follow_builder_items(*, refresh: bool = True) -> list[dict[str, Any]]:
    """加载 follow-builder 补充条目。

    优先顺序：
    1. 运行 prepare-digest.js 刷新本地 feed 快照
    2. 如果 latest-digest.json 与当前 feed 同步，则优先使用 digest 产物
    3. 否则回退到最新的 feed-x / feed-blogs / feed-podcasts
    """
    if not FOLLOW_BUILDER_DIR.exists() and not FOLLOW_BUILDER_PREPARE_DIGEST_SCRIPT.exists():
        return []
    if refresh:
        refresh_follow_builder_feed_bundle()
    digest_items = _load_follow_builder_digest_items()
    if digest_items:
        return digest_items
    items = _load_follow_builder_items_from_feeds()
    print(f"[follow-builder] 从最新 feed 加载了 {len(items)} 条补充条目", file=sys.stderr)
    return items


def inject_follow_builder_into_payload(search_payload: dict[str, Any]) -> dict[str, Any]:
    """将 follow-builder 数据注入 search_payload 的 ai_news.tavily_results 中。"""
    fb_items = load_follow_builder_items()
    if not fb_items:
        return search_payload
    ai_news = search_payload.get("ai_news", {})
    if not ai_news:
        return search_payload
    existing_urls = {
        canonicalize_url(item.get("url", "") or "") or str(item.get("url", "") or "").strip()
        for item in (ai_news.get("tavily_results", []) + ai_news.get("brave_results", []))
    }
    new_items = [
        item
        for item in fb_items
        if (canonicalize_url(item.get("url", "") or "") or str(item.get("url", "") or "").strip()) not in existing_urls
    ]
    ai_news["tavily_results"] = ai_news.get("tavily_results", []) + new_items
    search_payload["ai_news"] = ai_news
    print(f"[follow-builder] 注入了 {len(new_items)} 条新条目到 search_payload", file=sys.stderr)
    return search_payload


def parse_research_metadata(markdown: str) -> dict[str, Any]:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    title = lines[0].lstrip("# ").strip() if lines else "AI 日报"
    def is_valid_heading(line: str) -> bool:
        cleaned = line.lstrip("# ").strip()
        lowered = cleaned.lower()
        if len(cleaned) > 80:
            return False
        if any(
            marker in lowered
            for marker in (
                "information you can trust",
                "cookies",
                "privacy",
                "follow us",
                "main menu",
                "go to homepage",
                "skip to main content",
            )
        ):
            return False
        return True

    trend_titles = [
        line.lstrip("# ").strip()
        for line in lines
        if line.startswith("### 趋势") and is_valid_heading(line)
    ]
    product_titles = [
        line.lstrip("# ").strip()
        for line in lines
        if line.startswith("### ") and "趋势" not in line and is_valid_heading(line)
    ][:6]
    return {
        "title": title,
        "trend_titles": trend_titles[:3],
        "product_titles": product_titles[:3],
    }


def notify(title: str, body: str) -> None:
    from mobile_notify import notify as unified_notify

    unified_notify(title, body)


def submit_to_listenhub(
    query_text: str,
    *,
    language: str,
    mode: str,
    speaker_ids: list[str],
    sources: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": query_text,
        "speakers": [{"speakerId": speaker_id} for speaker_id in speaker_ids],
        "language": language,
        "mode": mode,
    }
    if sources:
        payload["sources"] = sources
    return listenhub_post("/podcast/episodes", payload)


def poll_listenhub_episode(episode_id: str, *, timeout_seconds: int = 420) -> dict[str, Any]:
    start = time.time()
    time.sleep(60)
    while time.time() - start < timeout_seconds:
        payload = listenhub_get(f"/podcast/episodes/{episode_id}")
        data = payload.get("data", {})
        status = data.get("processStatus")
        if status == "success":
            return data
        if status == "failed":
            raise RuntimeError(f"ListenHub 生成失败: {json.dumps(payload, ensure_ascii=False)}")
        time.sleep(10)
    raise TimeoutError(f"ListenHub 生成超时: {episode_id}")


def download_listenhub_assets(episode_dir: Path, episode_data: dict[str, Any]) -> dict[str, Any]:
    audio_url = episode_data.get("audioUrl", "")
    cover_url = episode_data.get("cover", "")
    if audio_url:
        tmp_audio = episode_dir / ".listenhub_audio"
        urllib.request.urlretrieve(audio_url, tmp_audio)
        title = clean_text(episode_data.get("title", "ListenHub播客")).replace("/", " ")
        final_audio = episode_dir / f"{title}.mp3"
        shutil.move(tmp_audio, final_audio)
        episode_data["local_audio_path"] = str(final_audio)
    if cover_url:
        tmp_cover = episode_dir / ".listenhub_cover"
        urllib.request.urlretrieve(cover_url, tmp_cover)
        final_cover = episode_dir / "cover.jpeg"
        subprocess.run(["sips", "-s", "format", "jpeg", str(tmp_cover), "--out", str(final_cover)], check=False)
        tmp_cover.unlink(missing_ok=True)
        if final_cover.exists():
            episode_data["local_cover_path"] = str(final_cover)
    return episode_data


def synthesize_local_tts(script_text: str, output_mp3: Path) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        text_path = Path(temp_dir) / "script.txt"
        aiff_path = Path(temp_dir) / "speech.aiff"
        text_path.write_text(script_text, encoding="utf-8")
        subprocess.run(["say", "-v", "Tingting", "-f", str(text_path), "-o", str(aiff_path)], check=True)
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg。")
        subprocess.run([ffmpeg, "-y", "-i", str(aiff_path), "-codec:a", "libmp3lame", "-q:a", "2", str(output_mp3)], check=True)


def collect_search_data(*, force_refresh: bool = False) -> dict[str, Any]:
    if not force_refresh:
        cached_payload = load_daily_search_cache()
        if cached_payload:
            # 即便命中缓存，也必须在返回前执行硬性时间截断，
            # 防止缓存里夹带 >36h 的旧闻进入 research.md。
            return hard_time_cutoff_filter(cached_payload)

    tavily_key = os.getenv("TAVILY_API_KEY")
    tavily_key_paid = (os.getenv("TAVILY_API_KEY_PAID") or "").strip() or None
    brave_key = os.getenv("BRAVE_SEARCH_API_KEY") or os.getenv("BRAVE_API_KEY")
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    huggingface_token = os.getenv("HUGGINGFACE_TOKEN", "").strip()
    apify_token = os.getenv("APIFY_TOKEN", "").strip()
    apify_actor_id = os.getenv("APIFY_X_ACTOR_ID", "apidojo/tweet-scraper").strip()
    if not tavily_key:
        raise RuntimeError("缺少 TAVILY_API_KEY。")
    warnings: list[str] = []

    base_ai_queries = materialize_queries(AI_NEWS_TAVILY_QUERY_TEMPLATES)
    base_product_queries = materialize_queries(PRODUCT_TAVILY_QUERY_TEMPLATES)
    tavily_ai_base = fetch_tavily_batch(base_ai_queries, tavily_key, fallback_key=tavily_key_paid)
    tavily_products_base = fetch_tavily_batch(
        base_product_queries, tavily_key, fallback_key=tavily_key_paid
    )

    if brave_key:
        brave_ai = fetch_brave_batch(materialize_queries(AI_NEWS_BRAVE_QUERY_TEMPLATES), brave_key)
        brave_products = fetch_brave_batch(materialize_queries(PRODUCT_BRAVE_QUERY_TEMPLATES), brave_key)
        warnings.extend(brave_ai.get("errors", []))
        warnings.extend(brave_products.get("errors", []))
    else:
        brave_ai = {"web": {"results": []}, "queries": []}
        brave_products = {"web": {"results": []}, "queries": []}
        warnings.append("缺少 BRAVE_SEARCH_API_KEY，已跳过 Brave 搜索。")

    tavily_ai = tavily_ai_base
    tavily_products = tavily_products_base
    base_ai_payload = merge_search_payloads(tavily_ai, brave_ai)
    base_product_payload = merge_search_payloads(tavily_products, brave_products)

    ai_backfill_plan = select_ai_news_backfill_queries(base_ai_payload)
    if brave_key and ai_backfill_plan["brave_queries"]:
        brave_ai_backfill = fetch_brave_batch(
            ai_backfill_plan["brave_queries"],
            brave_key,
            count_per_query=5,
            merged_limit=10,
        )
        warnings.extend(brave_ai_backfill.get("errors", []))
        brave_ai = merge_brave_provider_payloads(brave_ai, brave_ai_backfill, merged_limit=18)
        brave_ai["dynamic_queries"] = ai_backfill_plan["brave_queries"]
    brave_ai["backfill_categories"] = ai_backfill_plan["gap_categories"]
    brave_ai["quality_counts"] = ai_backfill_plan["counts"]

    product_intermediate_result = {
        "ai_news": merge_search_payloads(tavily_ai, brave_ai),
        "product_hunt": base_product_payload,
    }
    product_supplement_plan = select_product_community_supplement_queries(product_intermediate_result)
    external_product_payload = {"web": {"results": []}, "queries": []}
    used_provider_categories: list[str] = []

    for provider_category in product_supplement_plan.get("provider_categories", []):
        provider_payload: dict[str, Any] | None = None
        if provider_category == "github_newstars_api":
            provider_payload = fetch_github_recent_star_repos(api_token=github_token, merged_limit=10)
        elif provider_category == "hf_trending_api":
            provider_payload = fetch_huggingface_trending_items(api_token=huggingface_token, merged_limit=10)
        elif provider_category == "x_watch_apify":
            if apify_token:
                provider_payload = fetch_apify_x_watch_batch(
                    api_token=apify_token,
                    actor_id=apify_actor_id,
                    merged_limit=10,
                )
            else:
                warnings.append("缺少 APIFY_TOKEN，已跳过 Apify X 观察补源。")
        if not provider_payload:
            continue
        warnings.extend(provider_payload.get("errors", []))
        if provider_payload.get("web", {}).get("results"):
            external_product_payload = merge_brave_provider_payloads(
                external_product_payload,
                provider_payload,
                merged_limit=18,
            )
            used_provider_categories.append(provider_category)

    if brave_key and product_supplement_plan["brave_queries"]:
        brave_product_supplement = fetch_brave_batch(
            product_supplement_plan["brave_queries"],
            brave_key,
            count_per_query=5,
            merged_limit=10,
        )
        warnings.extend(brave_product_supplement.get("errors", []))
        brave_products = merge_brave_provider_payloads(brave_products, brave_product_supplement, merged_limit=18)
        brave_products["dynamic_queries"] = product_supplement_plan["brave_queries"]
    if external_product_payload.get("web", {}).get("results"):
        brave_products = merge_brave_provider_payloads(brave_products, external_product_payload, merged_limit=18)
        brave_products["provider_queries"] = external_product_payload.get("queries", [])
    brave_products["provider_categories"] = used_provider_categories
    brave_products["backfill_categories"] = product_supplement_plan["gap_categories"]

    # A1+A2: RSS 直订阅 — 在写入 result 前抓取并按 bucket 分发到 rss_results
    rss_by_bucket = fetch_rss_batch()

    # A3: Hacker News（免费 Firebase API）— 作为一个聚合源并入 ai_news bucket，
    # 走与 RSS 完全相同的 sanitize + 拒绝规则 + rss_results 链路。
    try:
        hn_batch = fetch_hackernews_batch()
    except Exception as exc:
        hn_batch = {"items": [], "errors": [f"hackernews: {exc}"]}
    hn_items = hn_batch.get("items", [])
    if hn_items or hn_batch.get("errors"):
        ai_bucket = rss_by_bucket.setdefault(
            "ai_news",
            {"web": {"results": []}, "queries": [], "errors": [], "rss_meta": {"feeds": []}},
        )
        ai_bucket["web"]["results"].extend(hn_items)
        ai_bucket.setdefault("queries", []).append("hackernews:top+new")
        ai_bucket.setdefault("rss_meta", {"feeds": []}).setdefault("feeds", []).append(
            {"name": "Hacker News", "count": len(hn_items)}
        )
        if hn_batch.get("errors"):
            ai_bucket["errors"] = (ai_bucket.get("errors") or []) + hn_batch["errors"][:4]

    result = {
        "generated_at": now_local().isoformat(),
        "ai_news": merge_search_payloads(tavily_ai, brave_ai),
        "product_hunt": merge_search_payloads(tavily_products, brave_products),
    }
    # 把 RSS 结果挂到对应 bucket 的 rss_results 字段（SEARCH_RESULT_KEYS 已包含此键）
    for bucket_name, rss_payload in rss_by_bucket.items():
        if bucket_name not in result:
            continue
        rss_items = list(rss_payload.get("web", {}).get("results", []) or [])
        # RSS 条目走轻量 sanitize（已在 fetch 阶段标准化），并复用搜索条目的拒绝规则
        accepted: list[dict[str, Any]] = []
        rejected = 0
        for item in rss_items:
            sanitized = sanitize_search_item(item)
            reason = search_item_rejection_reason(
                sanitized.get("title", ""),
                sanitized.get("content", "") or sanitized.get("description", ""),
                sanitized.get("url", ""),
                source_query=sanitized.get("source_query", ""),
                item=sanitized,
            )
            if reason:
                rejected += 1
                continue
            accepted.append(sanitized)
        result[bucket_name]["rss_results"] = accepted
        result[bucket_name]["rss_meta"] = {
            **(rss_payload.get("rss_meta") or {}),
            "accepted": len(accepted),
            "rejected": rejected,
        }
        # 把 RSS 抓取错误并入 warnings，否则整源失败时在 search_results.json 里完全不可见，
        # 排查时只能看到"RSS 神秘归零"。
        for rss_err in rss_payload.get("errors", []) or []:
            warning = f"rss:{rss_err}"
            if warning not in warnings:
                warnings.append(warning)

    result["ai_news"]["search_strategy"] = compact_dict(
        {
            "mode": "primary_then_backfill",
            "gap_categories": ", ".join(brave_ai.get("backfill_categories", [])),
            "backfill_queries": brave_ai.get("dynamic_queries", []),
            "quality_counts": brave_ai.get("quality_counts", {}),
        }
    )
    product_quality_counts = bucket_quality_counts(result["product_hunt"], limit=24)
    result["product_hunt"]["search_strategy"] = compact_dict(
        {
            "mode": (
                "precision_then_supplement"
                if brave_products.get("dynamic_queries") or brave_products.get("provider_queries")
                else "precision_only"
            ),
            "gap_categories": ", ".join(brave_products.get("backfill_categories", [])),
            "supplement_queries": brave_products.get("dynamic_queries", []),
            "provider_categories": brave_products.get("provider_categories", []),
            "provider_queries": brave_products.get("provider_queries", []),
            "quality_counts": product_quality_counts,
        }
    )
    result["quality_summary"] = summarize_search_quality(result)
    if warnings:
        result["warnings"] = warnings[:10]
    # 硬性时间截断：在写入每日缓存之前立即执行，
    # 确保任何 published_date 超过 HARD_TIME_CUTOFF_HOURS 的条目都被当场丢弃，
    # 绝对不允许进入后续的 research.md 生成环节。
    result = hard_time_cutoff_filter(result)
    return save_daily_search_cache(result)


def build_episode(args: argparse.Namespace) -> int:
    episode = args.episode or next_episode_id()
    episode_dir = ROOT_DIR / "episodes" / episode
    ensure_dir(episode_dir)

    if args.search_file:
        search_payload = read_json(Path(args.search_file))
        # 外部 search_file 同样必须过一遍硬性时间截断
        search_payload = hard_time_cutoff_filter(search_payload)
    else:
        search_payload = collect_search_data()

    # 注入 follow-builder 补充数据源
    search_payload = inject_follow_builder_into_payload(search_payload)

    funnel_stats: dict[str, int] = {}
    funnel_stats["raw_after_collect"] = count_payload_candidates(search_payload)

    coverage_index = load_coverage_index()
    funnel_stats["after_hard_time"] = count_payload_candidates(search_payload)

    search_payload = revive_unbroadcast_from_pools(search_payload, coverage_index)
    funnel_stats["after_b2_revive"] = count_payload_candidates(search_payload)

    # 硬性历史拦截：读取最近 N 期脚本的实体，拦截重复选题
    blocked_entities = extract_recent_script_entities()
    search_payload = hard_history_block_filter(search_payload, blocked_entities)
    funnel_stats["after_hard_history"] = count_payload_candidates(search_payload)

    search_payload = inject_backlog_candidates(search_payload, coverage_index)
    funnel_stats["after_b3_backlog"] = count_payload_candidates(search_payload)

    persist_drop_pools_to_backlog(search_payload)

    recent_context = load_recent_coverage_context(exclude_episode=episode)
    search_payload = filter_recently_covered_search_payload(search_payload, recent_context)
    funnel_stats["after_legacy_dedup"] = count_payload_candidates(search_payload)
    search_payload = supplement_search_payload_with_recent_uncovered_items(
        search_payload,
        recent_context,
        exclude_episode=episode,
    )
    funnel_stats["final"] = count_payload_candidates(search_payload)
    search_payload["funnel_stats"] = funnel_stats
    search_payload["quality_summary"] = summarize_search_quality(search_payload)
    print(
        "\033[1;36m[漏斗] "
        + " → ".join(f"{k}={v}" for k, v in funnel_stats.items())
        + "\033[0m",
        file=sys.stderr,
    )
    for _internal_key in ("_time_cutoff_pool", "_history_block_pool"):
        search_payload.pop(_internal_key, None)
    write_json(episode_dir / "search_results.json", search_payload)

    research_md = create_duo_research_markdown(search_payload, recent_context)
    script_text = create_podcast_script(research_md)
    metadata = parse_research_metadata(research_md)
    research_json = {
        "meta": {
            "date": now_local().strftime("%Y-%m-%d"),
            "title": metadata["title"],
        },
        "trend_titles": metadata["trend_titles"],
        "products": [{"name": name} for name in metadata["product_titles"]],
        "sources": summarize_sources(search_payload["ai_news"]) + summarize_sources(search_payload["product_hunt"]),
    }

    write_text(episode_dir / "research.md", research_md)
    write_text(episode_dir / "podcast_script.txt", script_text)
    write_json(episode_dir / "research.json", research_json)

    try:
        broadcast_items = bucket_result_items(search_payload.get("ai_news", {}) or {}) + \
                          bucket_result_items(search_payload.get("product_hunt", {}) or {})
        update_coverage_index_for_episode(episode, broadcast_items)
    except Exception as exc:
        print(f"[B2] 更新 coverage 索引失败: {exc}", file=sys.stderr)

    audio_path = ""
    cover_path = ""
    listenhub_episode_url = ""
    listenhub_title = ""
    selected_voice_id = ""

    if args.audio_mode == "listenhub":
        solo_basis = load_today_solo_basis(episode)
        if solo_basis:
            print(f"[dual] 已加载当天 E1 终稿作为事实底稿（{len(solo_basis)} 字）", file=sys.stderr)
        else:
            print("[dual] 警告：当天 E1 终稿缺失，对话设计稿回退自有研究链，无双审事实校验", file=sys.stderr)
        query_text = build_listenhub_query(search_payload, research_md, recent_context, solo_basis=solo_basis)
        write_text(episode_dir / "listenhub_query.txt", query_text)
        listenhub_episode_id = ""
        if not args.force_listenhub_create:
            listenhub_episode_id = existing_listenhub_episode_id(episode_dir)

        if listenhub_episode_id:
            existing_result = read_optional_json(episode_dir / "listenhub_create_result.json")
            if not existing_result:
                write_json(
                    episode_dir / "listenhub_create_result.json",
                    {"code": 0, "message": "reused existing episode", "data": {"episodeId": listenhub_episode_id}},
                )
        else:
            result = submit_to_listenhub(
                query_text,
                language=args.listenhub_language,
                mode=args.listenhub_mode,
                speaker_ids=[args.listenhub_speaker_1, args.listenhub_speaker_2],
                sources=[{"type": "url", "content": item["url"]} for item in research_json["sources"] if item.get("url")][:8],
            )
            write_json(episode_dir / "listenhub_create_result.json", result)
            listenhub_episode_id = result.get("data", {}).get("episodeId", "")
            if not listenhub_episode_id:
                raise RuntimeError(f"ListenHub 创建失败: {json.dumps(result, ensure_ascii=False)}")

        listenhub_episode_url = f"https://listenhub.ai/zh/app/episode/{listenhub_episode_id}"
        write_pending_download(
            {
                "episodeId": listenhub_episode_id,
                "episodeDir": str(episode_dir),
                "episodeUrl": listenhub_episode_url,
                "createdAt": now_local().isoformat(),
            }
        )
        if args.download_after_create:
            episode_data = poll_listenhub_episode(
                listenhub_episode_id, timeout_seconds=args.listenhub_timeout
            )
            episode_data = download_listenhub_assets(episode_dir, episode_data)
            write_json(episode_dir / "listenhub_episode_result.json", episode_data)
            audio_path = episode_data.get("local_audio_path", "")
            cover_path = episode_data.get("local_cover_path", "")
            listenhub_title = episode_data.get("title", "")
            clear_pending_download()
    elif args.audio_mode == "minimax":
        audio_path = str(episode_dir / "podcast.mp3")
        from minimax_tts import minimax_tts
        selected_voice_id = minimax_tts(script_text, Path(audio_path))
    else:
        audio_path = str(episode_dir / "podcast.mp3")
        selected_voice_id = ""
        synthesize_local_tts(script_text, Path(audio_path))

    row = {
        "日期": now_local().strftime("%Y-%m-%d"),
        "期数": episode,
        "标题": metadata["title"],
        "三大趋势": "\n".join(metadata["trend_titles"]),
        "产品洞察": "\n".join(metadata["product_titles"]),
        "研究文档路径": str(episode_dir / "research.md"),
        "口播稿路径": str(episode_dir / "podcast_script.txt"),
        "音频路径": audio_path,
        "TTS音色": selected_voice_id,
        "封面路径": cover_path,
        "ListenHub作品链接": listenhub_episode_url,
        "ListenHub标题": listenhub_title,
        "flow_type": "dual_host",
    }
    append_record_csv(row)
    write_json(episode_dir / "record.json", row)
    if args.cover_provider in ("gemini", "gemini-web"):
        regenerate_cover_for_episode(episode_dir, provider=args.cover_provider)
    try:
        sys.path.insert(0, str(ROOT_DIR))
        from db.schema import upsert_episode
        import json as _json
        upsert_episode({
            "id": episode,
            "date": row["日期"],
            "flow_type": "dual_host",
            "title": row["标题"],
            "trends": _json.dumps(metadata["trend_titles"], ensure_ascii=False),
            "products": _json.dumps(metadata["product_titles"], ensure_ascii=False),
            "script_path": row["口播稿路径"],
            "audio_path": row["音频路径"],
            "cover_path": row["封面路径"],
            "listenhub_url": row["ListenHub作品链接"],
            "listenhub_title": row["ListenHub标题"],
            "status": "audio_ready" if row["音频路径"] else "draft",
        })
    except Exception:
        pass
    notify("AI 播客生成完成", metadata["title"])
    notify("AI 播客待确认", f"{episode} 已生成脚本、音频和封面，确认内容后再发布。\n【下一步】确认后回复 下载单集 {episode}")
    # 手机端审稿：把对话设计稿（实际对话由 ListenHub 按它演绎）全文推到飞书
    try:
        from mobile_notify import notify_feishu_long_text
        query_path = episode_dir / "listenhub_query.txt"
        if query_path.exists():
            query_text_full = query_path.read_text(encoding="utf-8")
            marker = "今日对话设计稿："
            brief = query_text_full.split(marker, 1)[1].strip() if marker in query_text_full else ""
            if brief:
                notify_feishu_long_text(
                    f"AI观测站 · {episode} 双人对话设计稿（ListenHub 按此演绎）",
                    brief,
                )
    except Exception as exc:
        print(f"[dual] 对话设计稿推送失败: {exc}", file=sys.stderr)
    print(episode_dir)
    return 0


def collect_mode(args: argparse.Namespace) -> int:
    payload = collect_search_data(force_refresh=args.force_refresh)
    write_json(Path(args.output), payload)
    print(args.output)
    return 0


def local_tts_mode(args: argparse.Namespace) -> int:
    episode_dir = ROOT_DIR / args.episode
    script_text = (episode_dir / "podcast_script.txt").read_text(encoding="utf-8")
    output_mp3 = episode_dir / "podcast.mp3"
    synthesize_local_tts(script_text, output_mp3)
    print(output_mp3)
    return 0


def regenerate_cover_mode(args: argparse.Namespace) -> int:
    episode_dir = resolve_episode_dir_for_cover(args.episode)
    codex_source = Path(args.codex_source_image) if args.codex_source_image else None
    meta = regenerate_cover_for_episode(
        episode_dir,
        provider=args.provider,
        codex_source_image=codex_source,
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI daily news podcast pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect-search", help="Collect search results from Tavily and Brave")
    collect_parser.add_argument("--output", required=True)
    collect_parser.add_argument("--force-refresh", action="store_true")
    collect_parser.set_defaults(func=collect_mode)

    build_parser = subparsers.add_parser("build-episode", help="Generate research and create podcast")
    build_parser.add_argument("--episode", default="")
    build_parser.add_argument("--search-file", default="")
    build_parser.add_argument("--audio-mode", choices=["listenhub", "local-tts", "minimax"], default="listenhub")
    build_parser.add_argument("--listenhub-language", default="zh")
    build_parser.add_argument("--listenhub-mode", default="deep")
    build_parser.add_argument("--listenhub-speaker-1", default=os.getenv("LISTENHUB_SPEAKER_1", "chat-girl-105-cn"))
    build_parser.add_argument("--listenhub-speaker-2", default=os.getenv("LISTENHUB_SPEAKER_2", "CN-Man-Beijing-V2"))
    build_parser.add_argument("--download-after-create", action="store_true")
    build_parser.add_argument("--listenhub-timeout", type=int, default=420)
    build_parser.add_argument("--cover-provider", choices=["none", "gemini", "gemini-web", "codex"], default="gemini-web")
    build_parser.add_argument("--force-listenhub-create", action="store_true")
    build_parser.set_defaults(func=build_episode)

    tts_parser = subparsers.add_parser("local-tts", help="Generate local TTS from an existing script")
    tts_parser.add_argument("--episode", required=True)
    tts_parser.set_defaults(func=local_tts_mode)

    cover_parser = subparsers.add_parser("regenerate-cover", help="Regenerate cover for an existing episode")
    cover_parser.add_argument("--episode", default="")
    cover_parser.add_argument("--provider", choices=["gemini", "gemini-web", "codex"], default="gemini")
    cover_parser.add_argument("--codex-source-image", default="")
    cover_parser.set_defaults(func=regenerate_cover_mode)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
