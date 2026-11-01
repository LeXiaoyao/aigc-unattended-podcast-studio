"""Script planning, drafting, polishing, and quality control."""
from __future__ import annotations

from .shared import *
from .solo_llm import *
from .solo_news import *


def _gemini_polish_script(
    script_text: str,
    cards_text: str,
    *,
    story_count: int,
    outline_text: str = "",
) -> str:
    """Gemini 二次润色：只顺中文表达，不允许新增事实或主体。"""
    if not os.getenv("GEMINI_API_KEY", "").strip():
        return script_text
    story_floor = _solo_story_floor(story_count)
    target_min, target_max = _solo_script_length_bounds(story_count)
    system_prompt = (
        "你是一名中文科技播客的总编校对，只负责对现有口播稿做删改和顺稿。"
        "你不能新增任何卡片里没有的新闻、组织、产品、项目、数字、日期或判断；"
        "如果原稿里有不确定、机翻、套话、主体漂移或逻辑断裂，直接删掉或改短，不要补写新事实。"
    )
    user_content = (
        (f"本期提纲：\n{outline_text}\n\n" if outline_text else "")
        +
        f"候选新闻卡片：\n{cards_text}\n\n"
        f"待润色口播稿：\n{script_text}\n\n"
        "润色要求：\n"
        f"1. 保持新闻顺序基本不变，必须保留全部 {story_count} 条合格新闻，不要删掉任何一条只是因为它和别的新闻属于同一主体、同一主题或同一赛道\n"
        f"2. 全文目标篇幅约 {target_min} 到 {target_max} 字；如果原稿已经接近这个区间，不要为了“精炼”无故大幅压缩，除非存在明显硬伤，正文不要缩短超过 20%\n"
        "3. 只改中文表达，让句子更自然、像晨间播报\n"
        "4. 把生硬英文营销句式改成自然中文，但保留 Gemma 4、Claude、Codex、Avocado 这类产品或模型名\n"
        "5. 如果发现原稿里出现卡片中不存在的主体或细节，直接删除，不要替换成新的主体\n"
        "6. 如果开头说了几个主线或关键词，必须和正文对应；不对应就改开头，不要硬凑条数\n"
        "7. 删除这类句子：『虽然具体细节不多』『有观点认为』『如果团队没有开始构建』『Artificial Intelligence Automation Agency』\n"
        "8. 结尾只能保留固定片尾，不要再新增第二套感谢词或祝福句\n"
        "9. 删除任何没有明确主体、只是泛泛总结趋势的正文段落\n"
        "10. 删除或改写所有空泛判断和未来展望，尤其避免『潜力巨大』『市场认可』『有望达到』『持续增长』这类句式\n"
        "11. 如果某段只有一句有效事实，就保留这一句，不要为了顺滑追加背景、评价或展望\n"
        "12. 英文人名第一次出现时不要裸报姓名，必须补上清晰身份，例如『Vercel CEO Guillermo Rauch』『Box CEO Aaron Levie』『Every 联合创始人 Dan Shipper』；如果素材里没有明确职务，也至少补上最保守的身份或组织关系，不要只念名字\n"
        f"13. {_solo_quality_guardrails()}\n"
        "14. 只输出润色后的最终口播稿，不要解释"
    )
    # 二次润色走统一的 _llm_script 降级链（DeepSeek → Qwen-Plus → Gemini → Qwen-Turbo）
    # task 名保留 gemini_second_polish 以维持日志连续性
    try:
        polished = _normalize_script_text(
            _llm_script(system_prompt, user_content, temperature=0.2, task="gemini_second_polish")
        )
        if polished and len(polished) > len(script_text) * 0.6:
            _daily_log_stage(
                "script_polish",
                result="ok",
                before_len=len(script_text),
                after_len=len(polished),
                story_count=story_count,
            )
            return polished
        print(f"[llm] 二次润色输出过短（{len(polished)} < {int(len(script_text) * 0.6)}），回退原稿", file=sys.stderr)
    except Exception as exc:
        print(f"[llm] 二次润色全链失败: {exc}", file=sys.stderr)
        _daily_log_stage(
            "script_polish",
            result="fail",
            error=str(exc),
            before_len=len(script_text),
        )
    return script_text


def _verify_script_quality(script_text: str) -> dict:
    """LLM-as-a-Judge 质检：只审不改，返回 pass/fail 和具体问题列表。"""
    hard_issues = _deterministic_script_issues(script_text)
    if hard_issues:
        return {
            "pass": False,
            "reason": "命中硬规则：" + "；".join(str(issue.get("suggestion", "")).strip() for issue in hard_issues if issue.get("suggestion")),
            "issues": hard_issues,
        }
    segments = _extract_script_quality_segments(script_text)
    review_text = segments["body"] or script_text
    if not review_text.strip():
        return {"pass": True, "reason": "", "issues": []}

    system_prompt = (
        '你是一位经验丰富的科技媒体总编辑，正在审核一篇 AI 晨间口播稿。\n'
        '注意：固定模板的开头和结尾已经从待审文本中剥离，它们属于节目包装，不在本次质检范围内；'
        '不要因为没有开场白或结束语而判定失败。\n\n'
        '请逐条检查以下问题，并评估严重程度（严重/轻微/无）：\n'
        '1. 车轱辘话/水词：如「这引起了业界的广泛关注」「值得我们持续关注」「敬请期待」「具体细节不多」「有观点认为」等没有信息增量的句子。\n'
        '2. 废话段落：整段话没有具体事实、数据或主体，只是泛泛而谈趋势或展望。\n'
        '3. 机翻痕迹：不自然的中文表达、英文语序直译、主谓宾错位。\n'
        '4. 逻辑断裂：前后文衔接不通顺，话题跳跃没有过渡，或者开头提到的内容在正文中没有对应。\n'
        '5. 重复啰嗦：同一个意思用不同措辞反复出现。\n'
        '6. 原始 URL / 平台短链入稿：出现 http、t.co、x.com、github.com 这类链接或短链。\n'
        '7. 重复日期表述：开场同时出现“今天/今日/于今日/时间为 + 当天日期”等重复说法。\n'
        '8. 双结束语：正文里提前出现感谢收听、订阅引导或多套片尾。\n\n'
        '判断标准：\n'
        '- 如果存在"严重"问题（整段废话、大段机翻、核心逻辑断裂、URL 入稿、双结束语），判定为不通过\n'
        '- 如果只有"轻微"问题（个别水词、轻微啰嗦、段间过渡稍弱），判定为通过，但在reason中指出可改进之处\n'
        '- 口播稿允许适度的口语化过渡和简短的趋势概括，这不算水词\n\n'
        '你只负责审核，不要修改稿件。严格以 JSON 格式返回：\n'
        '{"pass": true/false, "reason": "简述具体问题", '
        '"issues": [{"text": "原文中有问题的句子", "type": "water/waste/translation/logic/repeat/url/date/closing", "severity": "severe/minor", "suggestion": "修改建议"}]}'
    )
    user_content = f"固定模板开头和结尾已豁免，以下仅为需要质检的正文部分：\n\n{review_text}"

    try:
        result = _llm_json(
            system_prompt,
            user_content,
            order_env="SOLO_JUDGE_MODEL_ORDER",
            default_order="deepseek,qwen,gemini",
            label="稿件质检",
        )
        passed = result.get("pass", False)
        if isinstance(passed, str):
            passed = passed.lower() in ("true", "1", "yes")
        reason = str(result.get("reason", "")).strip()
        issues = result.get("issues", [])
        if not isinstance(issues, list):
            issues = []
        return {"pass": bool(passed), "reason": reason, "issues": issues}
    except Exception as exc:
        print(f"[judge] 质检调用失败，默认放行: {exc}", file=sys.stderr)
        return {"pass": True, "reason": "", "issues": []}


def _fix_script_by_issues(script_text: str, issues: list[dict]) -> str:
    """根据质检指出的具体问题，用独立的 LLM 调用定点修复稿件。"""
    normalized_full = _normalize_script_text(script_text)
    if normalized_full != script_text and not _deterministic_script_issues(normalized_full):
        print("[judge] 本地硬规则修复完成")
        return normalized_full

    segments = _extract_script_quality_segments(script_text)
    body = segments["body"] or script_text
    if not issues:
        return script_text

    issue_instructions = "\n".join(
        f"- 问题句: 「{iss.get('text', '')}」→ 建议: {iss.get('suggestion', '删除或改写')}"
        for iss in issues
        if iss.get("text")
    )
    if not issue_instructions.strip():
        return script_text

    system_prompt = (
        "你是一位中文科技播客的文字编辑。请根据总编给出的修改意见，对口播稿做定点修复。\n"
        "规则：\n"
        "- 只改总编指出的问题句，其余内容原封不动\n"
        "- 不能新增卡片里没有的新闻、主体、数字\n"
        "- 如果建议删除某句，直接删掉，不要用别的话替换\n"
        "- 每条新闻必须独立成段，段间用空行分隔\n"
        "- 只输出修复后的正文，不要补写开头或结尾"
    )
    user_content = (
        f"总编修改意见：\n{issue_instructions}\n\n"
        f"待修复正文：\n{body}"
    )
    try:
        fixed_body = _llm_script(system_prompt, user_content, temperature=0.3, task="script_fix")
        fixed_body = _normalize_script_text(fixed_body)
        # 从 fixed_body 中提取纯正文部分
        fixed_segments = _extract_script_quality_segments(fixed_body)
        fixed_body_only = fixed_segments["body"] or fixed_body
        if fixed_body_only and len(fixed_body_only) > len(body) * 0.5:
            result = _rebuild_script_from_quality_segments(
                segments["intro"],
                fixed_body_only,
                segments["outro"],
            )
            print(f"[judge] 定点修复完成: {len(body)}字 → {len(fixed_body_only)}字")
            return result
        print("[judge] 修复后正文过短，保留原稿", file=sys.stderr)
    except Exception as exc:
        print(f"[judge] 定点修复失败，保留原稿: {exc}", file=sys.stderr)
    return script_text


def _write_solo_script_files(episode_dir: Path, script_text: str) -> None:
    main_path = episode_dir / "podcast_script.txt"
    write_text(main_path, script_text)

    timestamp = now_local().strftime("%Y%m%d-%H%M%S")
    snapshot_path = episode_dir / f"podcast_script_{timestamp}.txt"
    if not snapshot_path.exists():
        write_text(snapshot_path, script_text)


def _simplify_version_numbers(text: str) -> str:
    """简化口播稿中冗长的版本号，如 1.0.0b260402 → 1.0 beta。

    规则：
    - 带预发布标签的版本号（alpha/beta/rc/dev/a/b + 数字）→ 只保留主次版本 + 中文标签
    - 纯数字三段及以上（如 2.3.1.4）→ 只保留前两段
    - 已经是简短格式（如 4.0、v2）的不动
    """
    def _replace(m: re.Match) -> str:
        full = m.group(0)
        prefix = m.group(1) or ""  # 可能的 v 前缀
        major = m.group(2)
        minor = m.group(3) or ""

        # 检测预发布标签
        pre_match = re.search(r"[.\-]?(alpha|beta|rc|dev|a|b)[\d.]*", full[len(prefix) + len(major) + len(minor):], re.I)
        if pre_match:
            tag = pre_match.group(1).lower()
            label_map = {"a": "alpha", "b": "beta", "rc": "RC", "alpha": "alpha", "beta": "beta", "dev": "dev"}
            label = label_map.get(tag, tag)
            ver = f"{prefix}{major}"
            if minor:
                ver += f".{minor}"
            return f"{ver} {label}版"

        # 三段及以上纯数字版本号 → 只保留前两段
        remaining = full[len(prefix) + len(major):]
        dot_segments = re.findall(r"\.\d+", remaining)
        if len(dot_segments) >= 2:
            ver = f"{prefix}{major}"
            if minor:
                ver += f".{minor}"
            return ver

        return full

    # 匹配版本号模式：可选v前缀 + 主版本号 + 可选次版本号 + 后续部分
    text = re.sub(
        r"(v?)(\d+)(?:\.(\d+))(?:\.\d+[-.\w]*|\-(?:alpha|beta|rc|dev|a|b)[-.\d\w]*)",
        _replace,
        text,
        flags=re.I,
    )
    # 去除上下文已有标签导致的冗余，如 "beta版本1.0 beta版" → "beta版1.0"
    text = re.sub(r"(alpha|beta|rc|dev)版本(\S+)\s+\1版", r"\1版\2", text, flags=re.I)
    return text


def _normalize_script_text(script_text: str) -> str:
    text = (script_text or "").strip()
    text = text.replace(TEXT_MARKER_START, "").replace(TEXT_MARKER_END, "").strip()
    text = text.replace("感谢收收听", "感谢收听")
    text = _simplify_version_numbers(text)
    for pattern in SOLO_INLINE_URL_PATTERNS:
        text = pattern.sub("", text)
    text = re.sub(r"(?im)^\s*链接[:：].*$", "", text)
    paragraphs = _split_script_paragraphs(text)
    normalized_paragraphs: list[str] = []
    for paragraph in paragraphs:
        cleaned = paragraph.strip()
        cleaned = re.sub(r"以上就是今天的播报内容[，。,].*$", "", cleaned)
        cleaned = re.sub(r"以上是今天的AI产业全链路观察.*$", "", cleaned)
        cleaned = re.sub(r"以上就是今天的AI产业观察.*$", "", cleaned)
        cleaned = re.sub(r"如果觉得有用，欢迎订阅AI观测站.*$", "", cleaned)
        cleaned = re.sub(r"感谢收+听(?:AI观测站)?[，,。 ]*我们明天(?:同一时间)?再见[。！？!]*$", "", cleaned)
        cleaned = re.sub(r"我们明天(?:同一时间)?再见[。！？!]*$", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，、,:：;；")
        if not cleaned:
            continue
        if _is_fixed_closing_paragraph(cleaned):
            continue
        normalized_paragraphs.append(cleaned)
    body = _join_script_paragraphs(normalized_paragraphs).strip()
    if not body:
        return SOLO_CLOSING_LINE
    return body.rstrip("。 \n") + "\n\n" + SOLO_CLOSING_LINE


def _deterministic_script_issues(script_text: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    text = script_text or ""
    paragraphs = _split_script_paragraphs(text)
    compact_text = re.sub(r"\s+", "", text)

    if _contains_inline_url(text):
        match = next((pattern.search(text) for pattern in SOLO_INLINE_URL_PATTERNS if pattern.search(text)), None)
        issues.append(
            {
                "text": match.group(0) if match else "原始链接",
                "type": "url",
                "severity": "severe",
                "suggestion": "删除所有原始 URL、平台短链和链接口令，不要把链接念进口播稿。",
            }
        )

    closing_indexes = [idx for idx, paragraph in enumerate(paragraphs) if _is_fixed_closing_paragraph(paragraph)]
    if len(closing_indexes) > 1 or (closing_indexes and closing_indexes[-1] != len(paragraphs) - 1):
        closing_text = paragraphs[closing_indexes[0]] if closing_indexes else SOLO_CLOSING_LINE
        issues.append(
            {
                "text": closing_text,
                "type": "closing",
                "severity": "severe",
                "suggestion": "片尾只能保留一条固定结束语，并且必须位于全文最后一段。",
            }
        )

    today = _date_str_cn()
    if paragraphs:
        opening = re.sub(r"\s+", "", paragraphs[0])
        repeated_date_markers = sum(opening.count(token) for token in ("今天", "今日", "于今日", "时间为"))
        if opening.count(today) > 1 or (today in opening and repeated_date_markers >= 2):
            issues.append(
                {
                    "text": paragraphs[0],
                    "type": "date",
                    "severity": "severe",
                    "suggestion": "开场日期只报一次，去掉“今天/今日/于今日/时间为”等重复日期表述。",
                }
            )

    if compact_text.count(today) > 1:
        issues.append(
            {
                "text": today,
                "type": "date",
                "severity": "minor",
                "suggestion": "当天日期在整篇稿子里出现过多，除开场外不要重复播报同一天日期。",
            }
        )

    deduped: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for issue in issues:
        key = (issue.get("type", ""), issue.get("suggestion", ""))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(issue)
    return deduped


def _outline_source_items(items: list[dict[str, str | float | bool]]) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        payload.append(
            {
                "id": str(index),
                "title": _structured_title(item),
                "entity": _clean_optional_text(item.get("entity"), limit=36),
                "action": _clean_optional_text(item.get("action"), limit=20),
                "fact": _structured_fact_line(item),
                "topic": _clean_optional_text(item.get("topic"), limit=20),
                "bucket": _clean_optional_text(item.get("bucket"), limit=16),
            }
        )
    return payload


def _fallback_solo_outline(
    items: list[dict[str, str | float | bool]],
    *,
    angle_note: str = "",
) -> dict[str, object]:
    source_items = _outline_source_items(items)
    opening_keywords = [
        value
        for value in (
            [item["entity"] or item["topic"] for item in source_items[:3]]
            + ([angle_note] if angle_note else [])
        )
        if value
    ][:3]
    opening_focus = "、".join(dict.fromkeys(opening_keywords)) or "开源模型升级、AI Agent 基础设施和创投市场"
    return {
        "opening_focus": opening_focus,
        "opening_keywords": opening_keywords,
        "story_order": [item["id"] for item in source_items],
        "story_connections": [],
    }


def _render_outline_for_prompt(outline: dict[str, object]) -> str:
    keywords = outline.get("opening_keywords", [])
    if not isinstance(keywords, list):
        keywords = []
    story_order = outline.get("story_order", [])
    if not isinstance(story_order, list):
        story_order = []
    lines = [
        f"开头主线：{_clean_optional_text(outline.get('opening_focus'), limit=80)}",
        "开头关键词：" + "、".join(str(value).strip() for value in keywords if str(value).strip()),
        "故事顺序：" + " -> ".join(str(value).strip() for value in story_order if str(value).strip()),
    ]
    connections = outline.get("story_connections", [])
    if isinstance(connections, list) and connections:
        lines.append("关联提示：" + "；".join(_clean_optional_text(value, limit=40) for value in connections if value))
    return "\n".join(line for line in lines if line and not line.endswith("："))


def _plan_solo_script_outline(
    items: list[dict[str, str | float | bool]],
    *,
    angle_note: str = "",
) -> dict[str, object]:
    fallback = _fallback_solo_outline(items, angle_note=angle_note)
    source_items = _outline_source_items(items)
    system_prompt = (
        "你是 AI 科技早报的主编。"
        "请根据给定新闻卡片先做播报提纲，只输出 JSON。"
        "输入卡片里可能包含非 AI 领域的重大国际或商业新闻，这些属于当天的宏观背景。"
        "如果当天 AI 核心新闻偏平，可以把这些宏观背景新闻放进主线，或单独规划成重要板块，但不要强行把它们改写成 AI 新闻。"
        "JSON 格式必须是："
        "{\"opening_focus\":\"一句中文\",\"opening_keywords\":[\"最多3个词\"],\"story_order\":[\"1\",\"2\"],\"story_connections\":[\"可选，说明哪些卡片属于同一主线\"]}。"
        "要求：开头不要硬报数字，必须让开头主线和后面的正文一一对应；如果几条新闻属于同一主线，可以在 connections 里点出来。"
        "开头主线必须由给定卡片中的具体主体和动作抽象出来，不能写空泛的产业升级、前景看好、潜力巨大这类套话。"
    )
    user_content = json.dumps(
        {
            "date": _date_str_cn(),
            "angle_note": angle_note,
            "items": source_items,
            "macro_context_note": "卡片中可能含有非 AI 的重大国际/商业新闻，可作为当天宏观背景板块使用。",
            "quality_guardrails": _solo_quality_guardrails(),
        },
        ensure_ascii=False,
    )
    try:
        outline = _llm_json(
            system_prompt,
            user_content,
            order_env="SOLO_OUTLINE_MODEL_ORDER",
            default_order="deepseek,qwen,gemini",
            label="口播提纲规划",
        )
        story_order = outline.get("story_order", [])
        if not isinstance(story_order, list) or not story_order:
            return fallback
        return {
            "opening_focus": _clean_optional_text(outline.get("opening_focus"), limit=80) or fallback["opening_focus"],
            "opening_keywords": outline.get("opening_keywords", fallback["opening_keywords"]),
            "story_order": [str(value).strip() for value in story_order if str(value).strip()],
            "story_connections": outline.get("story_connections", []),
        }
    except Exception as exc:
        print(f"[solo] 提纲规划失败，回退默认提纲: {exc}", file=sys.stderr)
        return fallback


def _order_items_by_outline(
    items: list[dict[str, str | float | bool]],
    outline: dict[str, object],
) -> list[dict[str, str | float | bool]]:
    source_items = _outline_source_items(items)
    items_by_id = {item["id"]: candidate for item, candidate in zip(source_items, items)}
    ordered: list[dict[str, str | float | bool]] = []
    for value in outline.get("story_order", []):
        key = str(value).strip()
        candidate = items_by_id.get(key)
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    for candidate in items:
        if candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _draft_solo_script_from_outline(
    items: list[dict[str, str | float | bool]],
    outline: dict[str, object],
    *,
    story_count: int,
    angle_note: str = "",
) -> str:
    ordered_items = _order_items_by_outline(items, outline)
    cards_text = _build_solo_news_cards_from_items(ordered_items)
    target_min, target_max = _solo_script_length_bounds(story_count)
    story_floor = _solo_story_floor(story_count)
    paragraph_floor = 120 if story_count <= 12 else 90 if story_count <= 20 else 70
    paragraph_ceiling = 500 if story_count <= 8 else 320 if story_count <= 16 else 220
    system_prompt = (
        "你是一档中文 AI 晨间播客的主播。"
        "现在你已经拿到了主编给出的提纲，请严格按提纲写出正式口播稿。"
        "候选卡片里可能混有非 AI 领域的重大国际或商业新闻，它们是当天的宏观背景。"
        "如果当天 AI 核心新闻偏平，可以把这些宏观背景新闻当作重要板块来播，但必须明确说明它们是市场环境或国际背景，不要硬说成 AI 新闻本身。"
        "只用新闻卡片中的事实写作，可以做轻量归纳，但不能编造卡片里没有的新主体、新金额、新版本号。"
    )
    user_content = (
        f"今天日期：{_date_str_cn()}\n"
        f"主编提纲：\n{_render_outline_for_prompt(outline)}\n\n"
        f"内容主线补充：{angle_note or '聚焦当天最值得播报的 AI 产业动态'}\n\n"
        f"候选新闻卡片：\n{cards_text}\n\n"
        "写作要求：\n"
        f"- 总字数控制在 {target_min} 到 {target_max} 字\n"
        f"- 正文必须覆盖全部 {story_count} 条合格新闻，顺序必须与提纲一致，不要因为同主体、同赛道或同主题就省略其中任何一条\n"
        "- 开头用『早上好，今天是……以下是今日播报』这类自然起手，然后概括几个主线，最后说『好，我们一条一条来看。』\n"
        "- 输入卡片里可能包含非 AI 领域的重大国际或商业新闻；如果当天 AI 核心新闻平淡，可以把它们作为宏观背景板块播报，并明确交代这是当天的市场背景或国际背景\n"
        "- 【结构硬性要求】每条新闻必须独立成段，段与段之间用空行分隔；严禁把多条新闻挤在同一段落里\n"
        f"- 每条新闻段落建议写 {paragraph_floor}-{paragraph_ceiling} 字，至少包含：谁做了什么（第一句）、关键数字或细节（第二句）、简短一句为什么值得关注（第三句）；如果当天合格新闻很多，优先保证全部覆盖，可以压缩到一到三句，但不能漏掉任何达标新闻\n"
        "- 每条第一句必须说清楚谁做了什么；如果是融资，要尽量点名公司名；如果是框架或模型，要点出具体产品名\n"
        "- 如果主体是英文人名，第一次出现时不能只写名字，必须补上他的身份、头衔或组织关系，例如『Vercel CEO Guillermo Rauch』『Box CEO Aaron Levie』『Every 联合创始人 Dan Shipper』；若卡片里没有精确头衔，也要用最保守的身份说明他是谁\n"
        "- 保留 Gemma 4、Codex、Claude、HBM4 这类英文模型或产品名，但把英文营销句式改成自然中文\n"
        "- 每一段正文都必须对应至少一张具体卡片，并明确点出该卡片的主体；不允许写没有明确主体的趋势总结型段落\n"
        "- 如果合并两张卡片，只能合并同一条主线，而且合并后的段落必须把两个具体主体都点出来\n"
        "- 没有具体动作、数字、主体的卡片不要硬写，不要出现『虽然具体细节不多』这类句子\n"
        "- 【严禁模板句式】绝对不能写『主体是X，动作是X，核心事实是X』这种结构化填空句式。卡片里的标签（主体/动作/核心事实/补充细节）只是给你参考的结构化数据，写稿时必须用自然口语重新组织。句式要多样化，比如：'OpenAI 悄悄开始内测一款网络安全产品'、'斯坦福发了份重磅报告，数据很有意思'、'值得注意的是，Chamath 放了个狠话'。每条新闻的起手句不能雷同\n"
        "- 不要写『潜力巨大』『市场高度认可』『有望达到』『持续增长』这类空泛评价，也不要补写未来展望\n"
        "- 如果某条新闻只有一句硬事实，就只写一句，不要额外补背景废话或评论性过渡\n"
        "- 如果当天值得讲的新闻足够多，可以把篇幅自然拉长；但全文不要超过约 20 分钟口播对应的长度\n"
        f"- {_solo_quality_guardrails()}\n"
        "- 只输出最终口播稿，不要解释"
    )
    return _normalize_script_text(_llm_script(system_prompt, user_content, temperature=0.6))


def _script_quality_ok(script_text: str, *, story_count: int = SOLO_MIN_STORY_COUNT) -> bool:
    text = script_text or ""
    lowered = text.lower()
    if _deterministic_script_issues(text):
        return False
    min_target, max_target = _solo_script_length_bounds(story_count)
    min_length = max(600 if story_count <= 5 else 900, min_target - (260 if story_count <= 5 else 180))
    max_length = max(1800, max_target + 280)
    if len(text) < min_length:
        return False
    if len(text) > max_length:
        return False
    # 正文段落数不能太少：至少要有 min(3, story_count) 个独立段落
    body_paragraph_count = _count_script_news_paragraphs(text)
    min_paragraphs = min(3, story_count)
    if body_paragraph_count < min_paragraphs:
        return False
    if "趋势1" in text or "趋势2" in text or "趋势3" in text:
        return False
    if any(phrase in text for phrase in ["最值得看的方向", "这条变化说明", "对创业者来说", "如果把今天这些新闻放在一起看"]):
        return False
    if any(
        bad in lowered
        for bad in (
            "artificial intelligence automation agency",
            "daily ai agent news",
            "虽然具体细节不多",
            "如果团队没有开始构建",
        )
    ):
        return False
    # 检测连续 5 个以上英文单词（排除短专有名词如 "Daily AI Agent News"）
    eng_matches = re.findall(r"[A-Za-z]{4,}(?:\s+[A-Za-z0-9’&().,\-]{2,}){4,}", text)
    if eng_matches:
        return False
    return True


def _judge_and_refine(script: str, max_retries: int = 2) -> str:
    """审改分离的质检循环：质检只审不改，修复由独立调用完成。"""
    current = script
    attempts_log: list[dict[str, object]] = []
    for attempt in range(1, max_retries + 1):
        verdict = _verify_script_quality(current)
        attempts_log.append({
            "attempt": attempt,
            "pass": bool(verdict.get("pass")),
            "reason": str(verdict.get("reason", ""))[:200],
        })
        if verdict["pass"]:
            print(f"[judge] 质检通过（第{attempt}次检查）")
            _daily_log_stage(
                "quality_check",
                result="pass",
                passed_attempt=attempt,
                attempts=attempts_log,
                script_len=len(current),
            )
            return current
        issues = verdict.get("issues", [])
        severe_issues = [i for i in issues if i.get("severity") == "severe"]
        print(
            f"\033[33m[judge] 质检不通过（第{attempt}/{max_retries}次）: "
            f"{len(severe_issues)} 个严重问题, {len(issues) - len(severe_issues)} 个轻微问题\033[0m",
            file=sys.stderr,
        )
        if not severe_issues:
            # 没有严重问题，质检模型误判或标准偏差，直接放行
            print("[judge] 无严重问题，视为通过", file=sys.stderr)
            _daily_log_stage(
                "quality_check",
                result="pass_no_severe",
                passed_attempt=attempt,
                attempts=attempts_log,
                script_len=len(current),
            )
            return current
        # 有严重问题 → 独立修复调用
        fixed = _fix_script_by_issues(current, severe_issues)
        if fixed != current:
            current = fixed
        else:
            print("[judge] 修复未产生变更，终止循环", file=sys.stderr)
            break

    _daily_log_stage(
        "quality_check",
        result="fail",
        attempts=attempts_log,
        script_len=len(current),
    )
    return current


def create_solo_script(
    research_markdown: str,
    search_payload: dict[str, object] | None = None,
    *,
    episode_dir: Path | None = None,
    scarce: bool = False,
) -> str:
    """生成单人女主播新闻播报稿，采用结构化提取 -> 提纲规划 -> 初稿 -> Gemini 定稿 -> LLM 质检。

    Parameters
    ----------
    scarce: bool
        当硬过滤后合格候选不足 3 条时置为 True，
        会在提纲与初稿的 prompt 中注入"今日新闻较为平淡"的降级指令，
        避免 LLM 为了凑数反向把旧闻/凑数新闻拉进稿件。
    """
    effective_payload = search_payload or {"ai_news": {}, "product_hunt": {}}
    story_count = _solo_story_target(effective_payload)
    items = _select_solo_news_items(effective_payload, limit=story_count)
    if not items:
        raise RuntimeError("可用于生成口播稿的新闻卡片为空")
    story_count = len(items)
    cards_text = _build_solo_news_cards_from_items(items)
    angle_note = _build_solo_angle_note(research_markdown)
    scarcity_note = ""
    if scarce:
        scarcity_note = (
            f"【重要·平淡日提示】今日值得关注的行业大事件较少，"
            f"合格候选仅 {story_count} 条。请严格只播报下列 {story_count} 条新闻，"
            f"禁止自行扩充其他话题，并在开场白中自然地说明"
            f"「今天的 AI 行业相对平淡，我们只聊几条重点」。"
        )
        if angle_note:
            angle_note = scarcity_note + "\n\n" + angle_note
        else:
            angle_note = scarcity_note
        print(
            f"\033[1;33m[缺省降级] 已向 outline/draft prompt 注入平淡日提示\033[0m",
            file=sys.stderr,
        )
    outline = _plan_solo_script_outline(items, angle_note=angle_note)
    outline_text = _render_outline_for_prompt(outline)

    last_error = "unknown"
    best_candidate = ""
    for attempt in range(1, 3):
        try:
            script = _draft_solo_script_from_outline(items, outline, story_count=story_count, angle_note=angle_note)
            if script and len(script) > len(best_candidate):
                best_candidate = script
            if _script_quality_ok(script, story_count=story_count):
                polished = _gemini_polish_script(
                    script,
                    cards_text,
                    story_count=story_count,
                    outline_text=outline_text,
                )
                if polished and len(polished) > len(best_candidate):
                    best_candidate = polished
                if _script_quality_ok(polished, story_count=story_count):
                    return _judge_and_refine(polished)
                return _judge_and_refine(script)
            last_error = f"第{attempt}次定稿质量不足"
            print(f"[solo] {last_error}，重试", file=sys.stderr)
        except Exception as exc:
            last_error = str(exc)
            print(f"[solo] 多阶段文案第{attempt}次失败: {exc}", file=sys.stderr)

    if best_candidate:
        print(f"[solo] 质量闸门未通过，回退为最佳候选稿：{last_error}", file=sys.stderr)
        polished = _gemini_polish_script(
            best_candidate,
            cards_text,
            story_count=story_count,
            outline_text=outline_text,
        )
        return _judge_and_refine(polished or best_candidate)
    raise RuntimeError(f"LLM 口播文案生成失败：{last_error}")


__all__ = [name for name in globals() if not name.startswith("__")]
