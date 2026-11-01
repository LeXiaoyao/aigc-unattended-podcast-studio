"""Title generation helpers for the solo pipeline."""
from __future__ import annotations

from .shared import *
from .shared import _load_env
from .solo_llm import *
from .solo_news import *


def _build_solo_title_fallback(search_payload: dict[str, object]) -> str:
    """基于关键词匹配生成带冲突感的fallback标题。"""
    items = _select_solo_news_items(search_payload, limit=6)
    lowered = " ".join(item["title"].lower() for item in items)
    period_label = _period_label()

    # 按主题匹配，每个主题配一个有冲突感的标题模板
    conflict_templates: list[tuple[list[str], str]] = [
        (["openai", "anthropic", "claude"], "巨头互撕，谁在裸泳？"),
        (["agent", "agentic", "agent framework"], "智能体混战，谁能活到最后？"),
        (["chip", "cpu", "gpu", "hbm", "nvidia"], "芯片暗战，巨头押注谁？"),
        (["open source", "llama", "gemma", "qwen"], "开源反杀，闭源慌了"),
        (["copilot", "cursor", "codex", "code"], "AI写代码，程序员慌不慌？"),
        (["funding", "valuation", "ipo", "billion"], "资本疯抢AI，泡沫还是风口？"),
        (["apple", "google", "meta", "microsoft"], "科技巨头全押AI，谁在虚张声势？"),
        (["data center", "infrastructure", "compute"], "算力军备赛，谁先烧不起？"),
    ]
    for keywords, template in conflict_templates:
        if any(kw in lowered for kw in keywords):
            return f"{period_label}：{template}"

    # 兜底：从新闻中取最具体的主体
    top_entity = ""
    for item in items[:3]:
        entity = item.get("entity") or item.get("topic") or ""
        if entity and len(entity) <= 10:
            top_entity = entity
            break
    if top_entity:
        return f"{period_label}：{top_entity}搅局，谁在接招？"
    return f"{period_label}：AI暗流涌动，大变局将至"


def _title_quality_ok(title: str) -> bool:
    text = (title or "").strip()
    if len(text) < 8:
        return False
    if _english_heavy(text):
        return False
    if text.count("：") != 1:
        return False
    if text != _normalize_publish_safe_title(text):
        return False
    if re.search(r"：\s*(AI|人工智能)\s*：", text, re.I):
        return False
    # 拒绝平淡空泛的标题
    bland_keywords = (
        "关键变化", "全产业链重新排位", "生态持续演进", "持续演进",
        "全面升级", "新篇章", "多线并行", "加速落地",
    )
    if any(bad in text for bad in bland_keywords):
        return False
    # 拒绝以"升温"结尾的标题——太平淡
    after_colon = text.split("：", 1)[-1].strip()
    if after_colon.endswith("升温"):
        return False
    return True


def _normalize_title_text(title_text: str) -> str:
    title = (title_text or "").replace(TEXT_MARKER_START, "").replace(TEXT_MARKER_END, "").strip()
    title = title.splitlines()[0].strip() if title else ""
    title = re.sub(r"^标题[:：]\s*", "", title)
    title = re.sub(r"\s+", " ", title)
    if "：" not in title:
        title = f"{_period_label()}：{title}"
    title = re.sub(r"^\d{4}年\d{1,2}月[:：]\s*", f"{_period_label()}：", title)
    return _normalize_publish_safe_title(title.strip("。 "))


def _load_recent_titles(limit: int = 10) -> list[str]:
    """加载最近 N 期标题用于查重。"""
    titles = []
    episodes = sorted(ROOT_DIR.glob("20??????E*/title.txt"), reverse=True)
    for p in episodes[:limit]:
        try:
            t = p.read_text(encoding="utf-8").strip()
            if t:
                titles.append(t)
        except Exception:
            pass
    return titles


def _create_title_with_gemini_api(search_payload: dict[str, object], script_text: str) -> str:
    recent_titles = _load_recent_titles(10)
    avoid_clause = ""
    if recent_titles:
        avoid_clause = "\n\n以下是近期已用过的标题，绝对不能重复或高度相似：\n" + "\n".join(f"- {t}" for t in recent_titles)
    system_prompt = (
        "你是一位中文科技自媒体爆款标题编辑，请为 AI 单人口播生成一行标题。"
        "标题格式必须是 YYYY年M月：XXXXXXXX。"
        "冒号后控制在 8 到 14 个汉字（CJK字符算2、英文字母算1，总可见长度不超过36）。"
        "标题风格要求：\n"
        "1. 必须有观点、有态度、有冲突感，像公众号爆款标题，能让人一眼想点进来\n"
        "2. 善用对比、反转、设问句式，如『XXX的黄昏』『凭什么反杀』『别卷了，AI已经自己XX』\n"
        "3. 必须从当天新闻中提炼出一个核心矛盾或转折点，不是简单罗列事件\n"
        "4. 绝对禁止空泛套话：『产业加速落地』『生态持续演进』『全面升级』『新篇章』都是差标题\n"
        "5. 可以保留 Claude、OpenAI、Meta 这类短英文名\n"
        "只输出最终标题一行，不要解释。"
        + avoid_clause
    )
    user_prompt = (
        "标题风格示例：\n"
        "差标题反例（空泛无观点，绝对不要写成这样）：\n"
        f"- {_period_label()}：AI全产业链重新排位\n"
        f"- {_period_label()}：芯片升温\n"
        f"- {_period_label()}：Sycamore融资，Gemma4发布\n"
        f"- {_period_label()}：AI生态持续演进，多线并行推进\n"
        "好标题正例（有冲突、有态度、让人想点进来）：\n"
        "- 2026年4月：古典程序员的黄昏，Cursor到底砸了谁的饭碗？\n"
        "- 2026年3月：大模型退潮，小参数模型凭什么反杀？\n"
        "- 2026年4月：别卷提示词了，AI已经开始自己写Prompt\n"
        "- 2026年4月：OpenAI公开撕Anthropic，万亿估值背后的焦虑\n"
        "- 2026年3月：Meta砸千亿搞AI，但Llama越来越不开源了\n"
        "- 2026年4月：Sam Altman说AI要当研究实习生，但他可能完全失败\n\n"
        f"今日候选新闻：\n{_build_solo_news_brief(search_payload)}\n\n"
        f"今日脚本：\n{script_text}\n\n"
        "请从今日新闻中找到最具冲突感和话题性的切入点，写出一个让人忍不住想点进来听的标题。只输出最终标题。"
    )
    if os.getenv("GEMINI_API_KEY", "").strip():
        return _normalize_title_text(_gemini_text(system_prompt, user_prompt))
    return _normalize_title_text(_llm_script(system_prompt, user_prompt))


def _generate_solo_title(search_payload: dict[str, object], script_text: str, episode_dir: Path) -> str:
    # 给 Gemini 最多 2 次机会生成有冲突感的标题
    for attempt in range(2):
        try:
            title = _create_title_with_gemini_api(search_payload, script_text)
            if _title_quality_ok(title):
                return title
            print(f"[solo] Gemini 标题质量不达标 (attempt {attempt+1}): {title}", file=sys.stderr)
        except Exception as exc:
            print(f"[solo] Gemini API 标题生成失败 (attempt {attempt+1}): {exc}", file=sys.stderr)
    return _build_solo_title_fallback(search_payload)


def _resolve_final_script_path(episode_dir: Path, final_script_path: Path | None = None) -> Path:
    if final_script_path and final_script_path.exists():
        return final_script_path

    record_path = episode_dir / "record.json"
    if record_path.exists():
        try:
            record_script = read_json(record_path).get("口播稿路径")
            if record_script:
                candidate = Path(str(record_script)).expanduser()
                if candidate.exists():
                    return candidate
        except Exception:
            pass

    codex_script = episode_dir / "podcast_script_codex.txt"
    if codex_script.exists():
        return codex_script

    candidates = list(episode_dir.glob("podcast_script_*.txt"))
    main_script = episode_dir / "podcast_script.txt"
    if main_script.exists():
        candidates.append(main_script)
    if not candidates:
        raise RuntimeError(f"找不到最终口播稿: {episode_dir}")
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def _script_excerpt_for_title(script_text: str, limit: int = 3600) -> str:
    text = re.sub(r"\s+", "\n", (script_text or "").strip())
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n...\n{tail}"


def _cover_context_for_title(episode_dir: Path) -> str:
    meta_path = episode_dir / "cover_generation.json"
    if not meta_path.exists():
        return ""
    try:
        meta = read_json(meta_path)
    except Exception:
        return ""
    parts: list[str] = []
    for key in ("overlay_title", "prompt", "cover_prompt", "theme", "keywords"):
        value = meta.get(key)
        if value:
            parts.append(f"{key}: {value}")
    return "\n".join(parts)[:1600]


def _clean_codex_regen_title(raw_title: str) -> str:
    """清理 LLM 输出的原始标题，只做最小化处理，不截断正文内容。
    不调用 _normalize_publish_safe_title——那个函数会把"，AI..."这类内容当英文尾巴删掉。
    """
    title = (raw_title or "").replace(TEXT_MARKER_START, "").replace(TEXT_MARKER_END, "").strip()
    if not title:
        return ""
    # 取第一行（防止 LLM 多输出解释文字）
    title = title.splitlines()[0].strip()
    # 去掉"标题：""最终标题："等前缀
    title = re.sub(r"^[-\d.、\s]*(最终标题|标题|推荐)[:：]\s*", "", title)
    # 去掉书名号
    title = re.sub(r"^《(.+)》$", r"\1", title)
    # 去掉日期前缀
    title = re.sub(r"^\d{4}年\d{1,2}月(?:\d{1,2}日)?[:：]\s*", "", title)
    # 去掉所有空白（包括全角空格）
    title = re.sub(r"[\s　]+", "", title)
    # 去掉首尾标点
    title = title.strip("。！？!?，,、；;：:")
    return title


def _codex_regen_title_quality_ok(title: str, *, old_title: str = "", research_title: str = "") -> bool:
    text = (title or "").strip()
    if not text or text == old_title.strip() or text == research_title.strip():
        return False
    if any(bad in text for bad in ("今日AI新闻", "AI日报", "科技早报", "今日新闻", "产业观察")):
        return False
    if re.match(r"^\d{4}年\d{1,2}月", text):
        return False
    if _english_heavy(text):
        return False
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    if cjk_count < 10 or cjk_count > 34:
        return False
    return True


def _title_quality_fail_reason(
    title: str, *, old_title: str = "", research_title: str = ""
) -> str:
    """返回标题质检失败的具体原因，供错误提示使用。"""
    text = (title or "").strip()
    if not text:
        return "标题为空"
    if text == old_title.strip():
        return f"与当前标题完全相同（{old_title!r}）"
    if research_title and text == research_title.strip():
        return f"与研究标题完全相同（{research_title!r}）"
    bad_words = ("今日AI新闻", "AI日报", "科技早报", "今日新闻", "产业观察", "AI生态", "全产业链重新排位")
    matched = [w for w in bad_words if w in text]
    if matched:
        return f"包含泛标题词：{'、'.join(matched)}"
    if re.match(r"^\d{4}年\d{1,2}月", text):
        return "以日期开头（禁止以'2026年X月'等格式起头）"
    if _english_heavy(text):
        return "英文比例过高"
    if not _title_has_cjk(text):
        return "不含汉字"
    vl = _title_visible_length(text)
    if vl < 20:
        return f"标题太短（visible_len={vl}，要求≥20，约10个汉字）"
    if vl > 58:
        return f"标题太长（visible_len={vl}，要求≤58）"
    return "未知原因"


def _create_codex_regen_title(script_text: str, cover_context: str) -> tuple[str, bool]:
    _load_env()
    system_prompt = (
        "你是小宇宙中文科技播客的单集标题编辑。"
        "请只基于最终口播稿主线生成标题，封面信息只能辅助理解。"
        "先在心里生成3到5个候选并自评，最终只输出一个标题。"
        "标题要求：中文，14到24个汉字左右，不像新闻通稿，有信息密度和一点冲突感，"
        "不要标题党，不要使用『今日AI新闻』『AI日报』『科技早报』等泛标题，"
        "不要加日期前缀，不要解释。"
    )
    user_prompt = (
        f"最终口播稿节选：\n{_script_excerpt_for_title(script_text)}\n\n"
        f"封面/主题信息：\n{cover_context or '无'}\n\n"
        "请输出最适合发布到小宇宙的最终标题一行。"
    )
    try:
        return _clean_codex_regen_title(
            _llm_script(system_prompt, user_prompt, temperature=0.35, task="codex_title_regen")
        ), False
    except Exception as exc:
        raise RuntimeError(f"标题生成 LLM 调用失败，请手动输入标题。原因：{exc}") from exc


def _backup_old_title(title_path: Path) -> Path | None:
    if not title_path.exists():
        return None
    backup = title_path.with_name("title_before_codex_regen.txt")
    if backup.exists():
        stamp = now_local().strftime("%Y%m%d_%H%M%S")
        backup = title_path.with_name(f"title_before_codex_regen_{stamp}.txt")
    shutil.copy2(title_path, backup)
    return backup


def _append_title_regen_log(episode_dir: Path, payload: dict[str, object]) -> None:
    log_path = episode_dir / "pipeline_log.log"
    log_line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n[title_regenerated_by_codex] {log_line}\n")


def generate_episode_title_after_cover(
    episode_dir: Path,
    final_script_path: Path | None = None,
) -> dict[str, object]:
    script_path = _resolve_final_script_path(episode_dir, final_script_path)
    script_text = script_path.read_text(encoding="utf-8").strip()
    if not script_text:
        raise RuntimeError(f"最终口播稿为空: {script_path}")

    title_path = episode_dir / "title.txt"
    old_title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
    record_path = episode_dir / "record.json"
    record = read_json(record_path) if record_path.exists() else {}
    research_title = ""
    research_path = episode_dir / "research.md"
    if research_path.exists():
        first_line = research_path.read_text(encoding="utf-8").splitlines()[0].strip()
        research_title = re.sub(r"^#\s*", "", first_line).strip()

    cover_context = _cover_context_for_title(episode_dir)
    new_title, fallback = _create_codex_regen_title(script_text, cover_context)
    if new_title == old_title and record.get("title_regenerated_by_codex"):
        payload = {
            "title_regenerated_by_codex": True,
            "old_title": str(record.get("old_title") or old_title),
            "new_title": new_title,
            "title_source_script_path": str(script_path),
            "title_generated_at": str(record.get("title_generated_at") or now_local().isoformat(timespec="seconds")),
            "title_generation_fallback": bool(fallback or record.get("title_generation_fallback")),
            "title_regen_skipped": True,
            "backup_path": "",
        }
        _append_title_regen_log(episode_dir, payload)
        return payload
    if not _codex_regen_title_quality_ok(new_title, old_title=old_title, research_title=research_title):
        reason = _title_quality_fail_reason(new_title, old_title=old_title, research_title=research_title)
        raise RuntimeError(
            f"LLM 生成的标题未通过质检，请手动修改标题。\n"
            f"原因：{reason}\n"
            f"LLM 输出：{new_title!r}"
        )

    backup_path = _backup_old_title(title_path)
    write_text(title_path, new_title + "\n")

    generated_at = now_local().isoformat(timespec="seconds")
    record.update(
        {
            "标题": new_title,
            "title_regenerated_by_codex": True,
            "old_title": old_title,
            "new_title": new_title,
            "title_source_script_path": str(script_path),
            "title_generated_at": generated_at,
            "title_generation_fallback": bool(fallback),
        }
    )
    write_json(record_path, record)

    payload = {
        "title_regenerated_by_codex": True,
        "old_title": old_title,
        "new_title": new_title,
        "title_source_script_path": str(script_path),
        "title_generated_at": generated_at,
        "title_generation_fallback": bool(fallback),
        "backup_path": str(backup_path) if backup_path else "",
    }
    _append_title_regen_log(episode_dir, payload)
    return payload


__all__ = [name for name in globals() if not name.startswith("__")]
