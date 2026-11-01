"""Shownotes, chunking, and timeline helpers."""
from __future__ import annotations

from .shared import *
from .solo_llm import *
from .solo_news import *


def _split_spoken_sentences(text: str) -> list[str]:
    parts = re.findall(r"[^。！？!?]+[。！？!?]?", text or "")
    return [part.strip() for part in parts if part.strip()]


def _is_shownote_transition_sentence(sentence: str) -> bool:
    compact = re.sub(r"\s+", "", sentence or "").strip("。！？!?")
    if not compact:
        return True
    return any(re.fullmatch(pattern, compact) for pattern in SOLO_SHOWNOTE_TRANSITION_PATTERNS)


def _strip_shownote_sentence_prefix(sentence: str) -> str:
    cleaned = re.sub(r"^\s*(?:第?[一二三四五六七八九十\d]+[、，,:：]\s*)", "", sentence or "").strip()
    while True:
        original = cleaned
        for prefix in SOLO_SHOWNOTE_DISCOURSE_PREFIXES:
            cleaned = re.sub(rf"^\s*{re.escape(prefix)}[，,:：]\s*", "", cleaned).strip()
        if cleaned == original:
            return cleaned


def _trim_shownote_bullet(text: str, limit: int = 60) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    compact = re.sub(r"\s*([，。！？；：])\s*", r"\1", compact)
    compact = re.sub(r"(?:\.{3,}|…+)$", "", compact).strip(" ，、,:：;；-")
    compact = re.sub(r"(?:，?(?:这|那|它)?(?:也|就)?(?:说明|意味着|预示着|标志着|反映出|体现了|显示出|表明|代表着).*)$", "", compact)
    compact = compact.strip(" ，、,:：;；-")
    if not compact:
        return ""
    if any(compact.startswith(prefix) for prefix in SOLO_SHOWNOTE_COMMENTARY_PREFIXES):
        return ""
    if len(compact) > limit:
        clauses = [part.strip() for part in re.split(r"[，；]", compact.rstrip("。！？!?")) if part.strip()]
        best = ""
        for clause in clauses:
            candidate = clause if not best else f"{best}，{clause}"
            if len(candidate) <= limit:
                best = candidate
            else:
                break
        compact = best.strip(" ，、,:：;；-")
    if not compact:
        return ""
    if re.search(r"(?:因为|由于|为了|以便|并且|以及|包括|导致|用于|专注于|聚焦|围绕|关于|相关|另一则消息)$", compact):
        return ""
    if not re.search(r"[。！？!?]$", compact):
        compact += "。"
    return compact


def _extract_shownote_bullet_from_paragraph(paragraph: str, limit: int = 60) -> str:
    sentences = _split_spoken_sentences(paragraph) or [paragraph.strip()]
    for sentence in sentences:
        if _is_shownote_transition_sentence(sentence):
            continue
        candidate = _strip_shownote_sentence_prefix(sentence)
        if not candidate:
            continue
        if any(candidate.startswith(prefix) for prefix in SOLO_SHOWNOTE_COMMENTARY_PREFIXES):
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", candidate):
            continue
        if not re.search(r"[\u4e00-\u9fff]", candidate) and _english_heavy(candidate):
            continue
        summary = _trim_shownote_bullet(candidate, limit=limit)
        if summary:
            return summary
    return ""


def _shownote_dedupe_key(text: str) -> str:
    return normalize_title_key(re.sub(r"^\d+\.\s*", "", text or "").rstrip("。！？!? "))


def _shownote_tokens(text: str) -> set[str]:
    compact = str(text or "").lower()
    cjk = re.findall(r"[一-鿿]", compact)
    tokens = {a + b for a, b in zip(cjk, cjk[1:])}
    tokens.update(re.findall(r"[a-z][a-z0-9]{2,}|\d+(?:\.\d+)?", compact))
    return tokens


def _shownote_similarity(text_a: str, text_b: str) -> float:
    """要点之间的 Jaccard 相似度，用于语义去重（精确 key 抓不住换说法的重复）。"""
    set_a, set_b = _shownote_tokens(text_a), _shownote_tokens(text_b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _shownote_containment(needle: str, haystack: str) -> float:
    """needle 的 token 被 haystack 覆盖的比例，用于把要点匹配回脚本段落。"""
    needle_tokens = _shownote_tokens(needle)
    if not needle_tokens:
        return 0.0
    return len(needle_tokens & _shownote_tokens(haystack)) / len(needle_tokens)


def _match_bullet_labels_to_segments(
    segments: list[dict[str, str]],
    bullet_labels: list[str] | None,
) -> dict[int, str] | None:
    """把 shownote 要点按顺序匹配到 content 段落。

    返回 {segment 下标: 要点文本}；匹配不上的 content 段（判断段/边界段/总结段）
    不出现在结果里，调用方应跳过它们，不打时间点。
    要点和脚本新闻顺序一致，所以用带 1 条前瞻的贪心顺序匹配。
    匹配率过低（要点几乎对不上脚本）时返回 None，让调用方回退旧行为。
    """
    if not bullet_labels:
        return None
    mapping: dict[int, str] = {}
    bullet_idx = 0
    for seg_idx, segment in enumerate(segments):
        if segment.get("kind") != "content" or bullet_idx >= len(bullet_labels):
            continue
        if _shownote_containment(bullet_labels[bullet_idx], segment["text"]) >= 0.32:
            mapping[seg_idx] = bullet_labels[bullet_idx]
            bullet_idx += 1
            continue
        next_idx = bullet_idx + 1
        if next_idx < len(bullet_labels) and _shownote_containment(
            bullet_labels[next_idx], segment["text"]
        ) >= 0.32:
            mapping[seg_idx] = bullet_labels[next_idx]
            bullet_idx = next_idx + 1
    if len(mapping) < min(3, len(bullet_labels)):
        return None
    return mapping


def _search_fallback_bullet_ok(text: str) -> bool:
    bullet = str(text or "").strip()
    if not bullet:
        return False
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", bullet))
    if cjk_chars < 4 and _english_heavy(bullet):
        return False
    if re.search(r"(?:[A-Za-z]{4,}\s+){2,}[A-Za-z]{3,}", bullet) and cjk_chars < 8:
        return False
    return True


def _append_unique_shownote_bullet(
    bullets: list[str],
    seen_keys: set[str],
    text: str,
    *,
    limit: int = 90,
) -> None:
    bullet = _trim_shownote_bullet(text, limit=limit)
    if not bullet:
        return
    key = _shownote_dedupe_key(bullet)
    if key and key in seen_keys:
        return
    if any(_shownote_similarity(bullet, existing) >= 0.45 for existing in bullets):
        return
    if key:
        seen_keys.add(key)
    bullets.append(bullet)


def _probe_audio_duration_seconds(audio_path: Path) -> int | None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            try:
                return max(1, round(float(proc.stdout.strip())))
            except ValueError:
                pass

    afinfo = shutil.which("afinfo")
    if afinfo:
        proc = subprocess.run(
            [afinfo, str(audio_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        match = re.search(r"estimated duration:\s*([\d.]+)\s*sec", proc.stdout)
        if match:
            return max(1, round(float(match.group(1))))
    return None


def _format_audio_summary(audio_path: Path) -> str:
    size_mb = audio_path.stat().st_size / (1024 * 1024)
    duration_seconds = _probe_audio_duration_seconds(audio_path)
    if duration_seconds:
        return f"{duration_seconds}秒 / {size_mb:.1f}MB"
    return f"{size_mb:.1f}MB"


def _build_solo_shownote_bullets(search_payload: dict[str, object], script_text: str = "") -> list[str]:
    """优先用 LLM 提取要点；失败时退化为脚本/搜索摘要。要点数量与脚本实际新闻条数对齐。"""
    bullets: list[str] = []
    seen_keys: set[str] = set()

    # 目标条数 = 脚本实际新闻段落数（上限 SOLO_SHOWNOTE_BULLET_LIMIT），
    # 但保底至少 3 条，避免脚本段落过少时 shownotes 只有 1-2 条。
    script_news_count = _count_script_news_paragraphs(script_text)
    if script_news_count > 0:
        target_count = min(max(script_news_count, 3), SOLO_SHOWNOTE_BULLET_LIMIT)
    else:
        target_count = SOLO_SHOWNOTE_BULLET_LIMIT

    if script_text:
        try:
            result = _llm_research(
                "你是播客编辑。从播客脚本中提取尽可能完整的新闻要点，生成播客shownotes。",
                f"播客脚本：\n{script_text}\n\n"
                "要求：\n"
                "- 每条要点2-3句话，40-90字，包含具体事实（公司名、数字、产品名）\n"
                "- 全部使用简体中文，英文专有名词保留简称\n"
                "- 去掉所有过渡语和主观评论\n"
                "- 按脚本里实际新闻条数输出，一条新闻对应一条要点，**不要重复**；"
                "判断段、边界提醒段、开场结尾和总结段不要单独成条\n"
                "- 每条要点一一对应脚本中的一条新闻，顺序与脚本一致\n"
                "- 格式：每条前面加序号和右括号，如 1) 2) 3)，每条之间必须空一行\n"
                "- 不要输出“本期要点”“接下来”“最后一条”等提示语\n"
                "- 只输出要点，不要标题和其他内容\n\n"
                "参考格式：\n"
                "1) Anthropic泄露下一代旗舰模型Claude Mythos，内部文件显示该模型在软件编码、学术推理测试中得分大幅提升。\n\n"
                "2) 日本软银集团宣布获得400亿美元桥梁贷款，用于投资ChatGPT制造商OpenAI。",
            )

            bullets = []
            normalized = result.strip()
            normalized = re.sub(r"\r\n?", "\n", normalized)
            normalized = re.sub(r"^\s*本期要点[:：]?\s*", "", normalized, flags=re.M)
            parts = re.split(r"(?:^|\n)\s*(?=\d+[.)、]\s*)", normalized)
            for part in parts:
                line = re.sub(r"^\s*\d+[.)、]\s*", "", part.strip())
                line = re.sub(r"\n{2,}", "\n", line)
                line = re.sub(r"\n", "", line).strip()
                if line and len(line) >= 15:
                    _append_unique_shownote_bullet(bullets, seen_keys, line, limit=90)
            if len(bullets) < 3:
                for block in re.split(r"\n{2,}", normalized):
                    line = re.sub(r"^\s*\d+[.)、]\s*", "", block.strip())
                    line = re.sub(r"\n", "", line).strip()
                    if line and len(line) >= 15:
                        _append_unique_shownote_bullet(bullets, seen_keys, line, limit=90)
            # LLM 自己数的新闻条数比 _count_script_news_paragraphs 的启发式可靠
            # （判断段/边界段经常被启发式误算成新闻），所以 LLM 成功就直接采信，
            # 绝不再用脚本段落补位——补位正是历史上要点重复的来源。
            if len(bullets) >= 3:
                print(f"[shownotes] LLM 生成成功，提取 {len(bullets)} 条 (启发式估计 {target_count})")
                return bullets[:SOLO_SHOWNOTE_BULLET_LIMIT]
        except Exception as exc:
            print(f"[shownotes] LLM 生成失败: {exc}", file=sys.stderr)

    if script_text and len(bullets) < target_count:
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", script_text) if p.strip()]
        for para in paragraphs:
            if _is_fixed_opening_paragraph(para) or _is_fixed_closing_paragraph(para):
                continue
            if any(marker in para for marker in ("嗨，早上好", "早上好，今天是", "以上就是今天", "欢迎订阅", "评论区", "今天的播报到这里结束")):
                continue
            # 跳过 codex 风格的判断段/边界段/承接段，避免把"昨天我们讲过""这个数字要小心"误当新闻要点
            if _is_commentary_or_transition_paragraph(para):
                continue
            cleaned = para
            cleaned = re.sub(r"^先看第[一二三四五六七八九十\d]+条[。:]?", "", cleaned)
            cleaned = re.sub(r"^接下来是第[一二三四五六七八九十\d]+条[。:]?", "", cleaned)
            cleaned = re.sub(r"^第[一二三四五六七八九十\d]+条是关于([^。]+)的[。:]?", r"\1方面，", cleaned)
            cleaned = re.sub(r"^第[一二三四五六七八九十\d]+条是关于([^。]+)[。:]?", r"\1。", cleaned)
            # codex 的"第X条+动词承接"过渡句也要剥掉，如"第二条看电力。"
            cleaned = re.sub(
                r"^第[一二三四五六七八九十\d]+条(?:看|转到|讲|聊聊|聊|关注|说|来到|换到|进入)[^。：:]*[。:：]?",
                "",
                cleaned,
            )
            cleaned = re.sub(r"^此外[，,]?", "", cleaned)
            cleaned = re.sub(r"^最后一条是([^。]+)的新动态[。:]?", r"\1。", cleaned)
            cleaned = re.sub(r"^最后一条是([^。]+)[。:]?", r"\1。", cleaned)
            # "先看最会拉动预期的 OpenAI。"这类整句过渡先整体剥掉，避免和下一句拼成残句
            cleaned = re.sub(r"^(?:先看|再看|接着看|最后看|然后看)[^。！？]{0,20}[。！？]", "", cleaned).strip()
            sentences = [s.strip() for s in re.split(r"(?<=[。！？])", cleaned) if s.strip()]
            if not sentences:
                continue
            # codex 风格的新闻段几乎都以信源/事实动词开头；判断段、边界段、开场总结段
            # 首句是修辞（"边界要说清楚""这个数字很大"），靠这一条把它们挡在要点之外
            if not re.search(
                r"(报道|研究|公告|数据|宣布|发布|披露|预计|裁决|协议|合作|融资|收购|上市|营收|净营收|收入|显示|透露)",
                sentences[0],
            ):
                continue
            bullet = "".join(sentences[:2]).strip()
            bullet = re.sub(r"\s+", " ", bullet).strip()
            bullet = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", bullet)
            bullet = re.sub(r"^(这一变化意味着|这标志着|目前|不出意外的话|虽然细节不多，但可以看出)", "", bullet)
            bullet = bullet.replace("一项前沿研究方面，", "")
            if len(bullet) >= 18:
                _append_unique_shownote_bullet(bullets, seen_keys, bullet, limit=90)
            if len(bullets) >= target_count:
                break
        # 脚本抽取拿到 3 条以上就直接用，宁缺毋滥：
        # 继续凑数只会引入重复或判断段，而搜索兜底依赖 LLM——能走到这里说明 LLM 已经挂了
        if len(bullets) >= 3:
            print(f"[shownotes] 脚本兜底共 {len(bullets)} 条 (启发式估计 {target_count})")
            return bullets[:SOLO_SHOWNOTE_BULLET_LIMIT]

    # 兜底：从搜索结果提取（仅在脚本解析也失败时才动用，且必须通过英文/垃圾判定）
    if len(bullets) < 3:
        try:
            items = _select_solo_news_items(search_payload, limit=SOLO_SHOWNOTE_BULLET_LIMIT * 2)
        except Exception as exc:
            print(f"[shownotes] 搜索兜底失败: {exc}", file=sys.stderr)
            items = []
        for item in items:
            title = _clean_text(item.get("title", ""))
            if not title or len(title) <= 5:
                continue
            if not _search_fallback_bullet_ok(title):
                continue
            _append_unique_shownote_bullet(bullets, seen_keys, _trim_text(title, limit=50), limit=90)
            if len(bullets) >= SOLO_SHOWNOTE_BULLET_LIMIT:
                break
    if not bullets:
        bullets = ["暂无要点"]
    return bullets[:target_count]


def _concat_wavs_to_mp3(wav_paths: list[Path], output_mp3: Path) -> None:
    """将多个 WAV 切片拼接为一个 MP3 文件（向后兼容用）。"""
    import tempfile as _tf
    with _tf.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for wp in wav_paths:
            f.write(f"file '{wp}'\n")
        list_file = f.name
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file, "-codec:a", "libmp3lame", "-b:a", "192k",
             str(output_mp3)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[solo] WAV→MP3 拼接失败: {result.stderr[-300:]}", file=sys.stderr)
    finally:
        Path(list_file).unlink(missing_ok=True)


def _first_sentence_of_bullet(bullet: str) -> str:
    """从 shownote 要点中提取第一句话作为时间线标签。"""
    cleaned = re.sub(r"^\s*\d+[.)、]\s*", "", (bullet or "").strip())
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?])", cleaned) if s.strip()]
    if sentences:
        return sentences[0]
    return cleaned


def _split_script_into_chunks(
    script_text: str,
    bullet_labels: list[str] | None = None,
) -> list[dict[str, str]]:
    """将口播稿按段落拆分为 TTS 切片列表。

    返回: [{"text": "...", "label": "时间戳标签（空=不打点）", "kind": "opening|content|closing"}, ...]

    当提供 bullet_labels 时，直接用其第一句话作为 content 段的时间线标签，
    而非从脚本正文用正则提取（避免截断产生残句）。
    """
    segments = _script_timeline_segments(script_text)
    label_map = _match_bullet_labels_to_segments(segments, bullet_labels)
    chunks: list[dict[str, str]] = []
    content_idx = 0
    for seg_idx, seg in enumerate(segments):
        kind = seg["kind"]
        text = seg["text"].strip()
        if not text:
            continue
        if kind == "content":
            if label_map is not None:
                # 匹配不上要点的 content 段是判断/边界/总结段，不打时间点
                label = _first_sentence_of_bullet(label_map.get(seg_idx, ""))
            elif bullet_labels and content_idx < len(bullet_labels):
                label = _first_sentence_of_bullet(bullet_labels[content_idx])
            else:
                label = _extract_shownote_bullet_from_paragraph(text, limit=44)
                if not label or _is_shownote_transition_sentence(label) or len(re.sub(r"\s+", "", label)) < 8:
                    fallback_text = re.sub(
                        r"^(?:先看第一条|接下来(?:请关注)?|然后是|此外|最后聊聊|最后一条|第[一二三四五六七八九十\d]+条(?:看|转到|讲|聊聊|聊|关注|说|来到|换到|进入)?)[^。!?！？]*[，。:：\s]*",
                        "",
                        text,
                    ).strip()
                    label = _extract_shownote_bullet_from_paragraph(fallback_text, limit=44)
                    if not label:
                        label = _trim_text(_clean_text(fallback_text), limit=44)
            content_idx += 1
        else:
            label = ""  # 开场/结尾不打时间戳
        chunks.append({"text": text, "label": label, "kind": kind})
    return chunks


def _script_timeline_segments(script_text: str) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    for match in re.finditer(r"\S(?:.*?)(?=\n{2,}|\Z)", script_text or "", flags=re.S):
        paragraph = match.group(0).strip()
        if not paragraph:
            continue
        if any(marker in paragraph for marker in (
            "以上就是今天的AI产业观察",
            "欢迎订阅AI观测站",
            "评论区告诉我",
            "真正跑赢这一轮",
            "如果今天这些新闻必须浓缩成一句话",
        )):
            kind = "closing"
        elif (
            "好，我们一条一条来看" in paragraph
            or paragraph.startswith("嗨，早上好")
            or paragraph.startswith("早上好")
            or paragraph.startswith("观众朋友们")
            or re.search(r"今天是\d{4}年\d{1,2}月\d{1,2}日", paragraph)
        ):
            kind = "opening"
        else:
            kind = "content"
        segments.append({"kind": kind, "text": paragraph, "start_char": str(match.start())})
    return segments


def _spoken_segment_weight(text: str) -> int:
    compact = re.sub(r"\s+", "", text or "")
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", compact))
    latin_tokens = re.findall(r"[A-Za-z]+(?:[-_][A-Za-z]+)*", compact)
    number_tokens = re.findall(r"\d+(?:[.,]\d+)?%?", compact)
    punctuation_count = len(re.findall(r"[，。！？；：,.!?;:]", text or ""))
    latin_weight = sum(max(2, min(len(token), 14)) for token in latin_tokens)
    number_weight = sum(max(2, min(len(token), 8)) for token in number_tokens)
    return max(12, cjk_count + latin_weight + number_weight + punctuation_count)


def _normalize_alignment_text(text: str) -> str:
    compact = str(text or "")
    compact = re.sub(r"<#\d+(?:\.\d+)?#>", "", compact)
    compact = compact.replace("——", "")
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", "", compact)
    return compact.lower().strip()


def _subtitle_char_spans(
    subtitles: list[dict[str, object]],
) -> tuple[str, list[tuple[int, int, int]]]:
    combined_parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for item in subtitles:
        text = _normalize_alignment_text(str(item.get("text", "")))
        if not text:
            continue
        start_ms = max(int(item.get("start_ms", 0) or 0), 0)
        combined_parts.append(text)
        spans.append((cursor, cursor + len(text), start_ms))
        cursor += len(text)
    return "".join(combined_parts), spans


def _alignment_position_to_ms(char_pos: int, spans: list[tuple[int, int, int]]) -> int:
    for start_pos, end_pos, start_ms in spans:
        if start_pos <= char_pos < end_pos:
            return start_ms
    if spans:
        return spans[-1][2]
    return 0


def _segment_alignment_candidates(
    segment_text: str,
    *,
    kind: str,
    bullet_label: str = "",
) -> list[str]:
    candidates: list[str] = []

    def _append_candidate(value: str) -> None:
        normalized = _normalize_alignment_text(value)
        if len(normalized) < 6:
            return
        if normalized in {_normalize_alignment_text(existing) for existing in candidates}:
            return
        candidates.append(value)

    if kind == "opening":
        _append_candidate(segment_text)
        _append_candidate("好，我们一条一条来看。")
        return candidates

    if kind == "closing":
        _append_candidate("以上就是今天的AI产业观察。")
        _append_candidate(segment_text)
        return candidates

    if bullet_label:
        _append_candidate(_first_sentence_of_bullet(bullet_label))

    extracted = _extract_shownote_bullet_from_paragraph(segment_text, limit=44)
    if extracted:
        _append_candidate(extracted)

    for sentence in _split_spoken_sentences(segment_text) or [segment_text]:
        if _is_shownote_transition_sentence(sentence):
            continue
        stripped = _strip_shownote_sentence_prefix(sentence)
        if stripped:
            _append_candidate(stripped)
            break

    cleaned_segment = re.sub(
        r"^(?:先看(?:第?[一二三四五六七八九十\d]+条)?|接下来(?:是|请关注)?第?[一二三四五六七八九十\d]+条|此外|最后看|最后(?:再看)?一条)[，。:：\s]*",
        "",
        segment_text,
    ).strip()
    if cleaned_segment and cleaned_segment != segment_text:
        _append_candidate(cleaned_segment)
    _append_candidate(segment_text)
    return candidates


def _find_alignment_anchor(
    combined_text: str,
    candidates: list[str],
    *,
    start_char: int,
) -> tuple[int | None, int]:
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_alignment_text(candidate)
        if len(normalized) < 6 or normalized in seen:
            continue
        seen.add(normalized)
        anchor_variants: list[str] = [normalized]
        for size in (64, 48, 36, 24, 16, 12, 8):
            if len(normalized) > size:
                anchor_variants.append(normalized[:size])
        for anchor in anchor_variants:
            if len(anchor) < 6:
                continue
            found = combined_text.find(anchor, start_char)
            if found != -1:
                return found, len(anchor)
    return None, 0


def _build_solo_timeline_from_subtitles(
    script_text: str,
    subtitles: list[dict[str, object]] | None,
    *,
    start_offset_ms: int = 0,
    limit: int = SOLO_SHOWNOTE_TIMELINE_LIMIT,
    bullet_labels: list[str] | None = None,
) -> list[dict[str, str]]:
    if not subtitles:
        return []

    combined_text, spans = _subtitle_char_spans(subtitles)
    if not combined_text or not spans:
        return []

    segments = _script_timeline_segments(script_text)
    label_map = _match_bullet_labels_to_segments(segments, bullet_labels)
    timeline: list[dict[str, str]] = []
    cursor = 0
    content_idx = 0
    for seg_idx, segment in enumerate(segments):
        bullet_label = ""
        if segment["kind"] == "content":
            if label_map is not None:
                bullet_label = label_map.get(seg_idx, "")
            elif bullet_labels and content_idx < len(bullet_labels):
                bullet_label = bullet_labels[content_idx]

        candidates = _segment_alignment_candidates(
            segment["text"],
            kind=segment["kind"],
            bullet_label=bullet_label,
        )
        found, anchor_len = _find_alignment_anchor(combined_text, candidates, start_char=cursor)
        if found is not None:
            cursor = found + anchor_len

        if segment["kind"] != "content":
            continue

        if found is None:
            content_idx += 1
            continue

        if label_map is not None and not bullet_label:
            # 匹配不上要点的 content 段是判断/边界/总结段，不打时间点
            content_idx += 1
            continue

        label = _first_sentence_of_bullet(bullet_label) if bullet_label else ""
        if not label:
            label = _extract_shownote_bullet_from_paragraph(segment["text"], limit=44)
        if not label:
            label = _trim_text(_clean_text(segment["text"]), limit=44)
        if label:
            total_seconds = round(
                (_alignment_position_to_ms(found, spans) + max(start_offset_ms, 0)) / 1000
            )
            timeline.append(
                {
                    "time": _format_mmss(total_seconds),
                    "label": label,
                    "method": "tts_subtitle_alignment",
                    "script_excerpt": _trim_text(_clean_text(segment["text"]), limit=90),
                }
            )
            if len(timeline) >= limit:
                break
        content_idx += 1

    return timeline


def _format_srt_timestamp(seconds: float) -> str:
    total_ms = max(round(float(seconds or 0) * 1000), 0)
    hours, rem = divmod(total_ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _write_whisper_transcript_files(
    episode_dir: Path,
    *,
    engine: str,
    model_name: str,
    language: str,
    audio_path: Path,
    duration_seconds: int | None,
    segments: list[dict[str, object]],
) -> tuple[Path, Path]:
    json_path = episode_dir / "transcript_whisper.json"
    srt_path = episode_dir / "transcript_whisper.srt"
    payload = {
        "engine": engine,
        "model": model_name,
        "language": language,
        "audio_path": str(audio_path.resolve()),
        "audio_duration_seconds": duration_seconds,
        "segment_count": len(segments),
        "segments": segments,
    }
    write_json(json_path, payload)

    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        lines.append(str(index))
        lines.append(
            f"{_format_srt_timestamp(float(segment.get('start', 0) or 0))} --> "
            f"{_format_srt_timestamp(float(segment.get('end', 0) or 0))}"
        )
        lines.append(text)
        lines.append("")
    write_text(srt_path, "\n".join(lines).strip() + ("\n" if lines else ""))
    return json_path, srt_path


def _transcribe_audio_with_whisper(audio_path: Path, episode_dir: Path) -> dict[str, object]:
    model_name = os.environ.get("SOLO_WHISPER_MODEL", "small").strip() or "small"
    language = os.environ.get("SOLO_WHISPER_LANGUAGE", "zh").strip() or "zh"
    duration_seconds = _probe_audio_duration_seconds(audio_path)

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError(
            "Whisper 主路径不可用：本地未能导入 faster_whisper。"
            "请安装 faster-whisper，或显式使用 --timecode-mode heuristic。"
        ) from exc

    try:
        model = WhisperModel(model_name, device="auto", compute_type="auto")
        raw_segments, info = model.transcribe(
            str(audio_path),
            language=language,
            vad_filter=True,
            beam_size=5,
        )
        segments: list[dict[str, object]] = []
        for index, segment in enumerate(raw_segments):
            text = str(segment.text or "").strip()
            if not text:
                continue
            segments.append(
                {
                    "id": index,
                    "start": float(segment.start or 0),
                    "end": float(segment.end or 0),
                    "text": text,
                }
            )
    except Exception as exc:
        raise RuntimeError(
            f"Whisper 转写失败：engine=faster_whisper model={model_name} audio={audio_path}"
        ) from exc

    if not segments:
        raise RuntimeError(
            f"Whisper 转写没有生成可用 segment：audio={audio_path}。"
            "默认模式不会静默回退 heuristic。"
        )

    json_path, srt_path = _write_whisper_transcript_files(
        episode_dir,
        engine="faster_whisper",
        model_name=model_name,
        language=getattr(info, "language", language) or language,
        audio_path=audio_path,
        duration_seconds=duration_seconds,
        segments=segments,
    )
    return {
        "engine": "faster_whisper",
        "model": model_name,
        "language": getattr(info, "language", language) or language,
        "duration_seconds": duration_seconds,
        "segments": segments,
        "json_path": json_path,
        "srt_path": srt_path,
    }


def _normalized_overlap_score(needle: str, haystack: str) -> float:
    needle_norm = _normalize_alignment_text(needle)
    haystack_norm = _normalize_alignment_text(haystack)
    if len(needle_norm) < 4 or len(haystack_norm) < 4:
        return 0.0
    if needle_norm in haystack_norm:
        return 1.0
    if haystack_norm in needle_norm:
        return min(0.95, len(haystack_norm) / max(len(needle_norm), 1))

    import difflib

    best = difflib.SequenceMatcher(None, needle_norm, haystack_norm).ratio()
    target_sizes = [min(len(needle_norm), size) for size in (80, 56, 36, 24, 16)]
    for size in target_sizes:
        if size < 8 or len(needle_norm) <= size:
            continue
        fragment = needle_norm[:size]
        best = max(best, difflib.SequenceMatcher(None, fragment, haystack_norm).ratio())
        if fragment in haystack_norm:
            best = max(best, 0.92)
    return best


def _best_transcript_match(
    candidates: list[tuple[str, str]],
    transcript_segments: list[dict[str, object]],
    *,
    start_index: int,
) -> dict[str, object] | None:
    best: dict[str, object] | None = None
    for index in range(max(start_index, 0), len(transcript_segments)):
        window_segments = transcript_segments[index:min(index + 3, len(transcript_segments))]
        window_text = " ".join(str(segment.get("text", "")).strip() for segment in window_segments).strip()
        if not window_text:
            continue
        for source, candidate in candidates:
            score = _normalized_overlap_score(candidate, window_text)
            if best is None or score > float(best.get("score", 0)):
                first = window_segments[0]
                last = window_segments[-1]
                best = {
                    "segment_index": index,
                    "source": source,
                    "score": round(score, 4),
                    "start": float(first.get("start", 0) or 0),
                    "end": float(last.get("end", first.get("end", 0)) or 0),
                    "text": window_text,
                }
    if best and float(best.get("score", 0)) >= 0.42:
        return best
    return None


def _interpolate_unmatched_timeline_items(
    timeline: list[dict[str, object]],
    *,
    audio_duration_seconds: int | None,
) -> None:
    matched_indexes = [i for i, item in enumerate(timeline) if item.get("matched")]
    if not matched_indexes:
        return

    for index, item in enumerate(timeline):
        if item.get("matched"):
            continue
        prev_indexes = [i for i in matched_indexes if i < index]
        next_indexes = [i for i in matched_indexes if i > index]
        if prev_indexes and next_indexes:
            prev_i = prev_indexes[-1]
            next_i = next_indexes[0]
            prev_time = float(timeline[prev_i].get("timestamp_seconds", 0) or 0)
            next_time = float(timeline[next_i].get("timestamp_seconds", prev_time) or prev_time)
            ratio = (index - prev_i) / max(next_i - prev_i, 1)
            seconds = round(prev_time + (next_time - prev_time) * ratio)
        elif prev_indexes:
            prev_i = prev_indexes[-1]
            prev_time = float(timeline[prev_i].get("timestamp_seconds", 0) or 0)
            tail_end = float(audio_duration_seconds or prev_time + 45)
            seconds = round(prev_time + (tail_end - prev_time) * ((index - prev_i) / max(len(timeline) - prev_i, 1)))
        else:
            next_i = next_indexes[0]
            next_time = float(timeline[next_i].get("timestamp_seconds", 0) or 0)
            seconds = round(next_time * ((index + 1) / max(next_i + 1, 1)))
        item["timestamp_seconds"] = max(seconds, 0)
        item["time"] = _format_mmss(int(item["timestamp_seconds"]))
        item["method"] = "whisper_interpolated"
        item["warning"] = "unmatched_interpolated"


def _build_solo_timeline_from_whisper(
    script_text: str,
    transcript_segments: list[dict[str, object]],
    *,
    audio_duration_seconds: int | None,
    limit: int = SOLO_SHOWNOTE_TIMELINE_LIMIT,
    bullet_labels: list[str] | None = None,
) -> list[dict[str, object]]:
    script_segments = [segment for segment in _script_timeline_segments(script_text) if segment["kind"] == "content"]
    labels = (bullet_labels or [])[:limit]
    if not labels:
        labels = [
            _extract_shownote_bullet_from_paragraph(segment["text"], limit=44)
            or _trim_text(_clean_text(segment["text"]), limit=44)
            for segment in script_segments[:limit]
        ]

    timeline: list[dict[str, object]] = []
    cursor = 0
    for index, label in enumerate(labels[:limit]):
        script_segment = script_segments[index] if index < len(script_segments) else {"text": ""}
        candidates = [
            ("shownotes_item", label),
            ("shownotes_label", _first_sentence_of_bullet(label)),
            ("script_paragraph", str(script_segment.get("text", ""))),
        ]
        candidates.extend(
            ("script_sentence", sentence)
            for sentence in _split_spoken_sentences(str(script_segment.get("text", "")))[:3]
            if sentence
        )
        match = _best_transcript_match(candidates, transcript_segments, start_index=cursor)
        if match:
            cursor = int(match["segment_index"]) + 1
            seconds = round(float(match["start"]))
            timeline.append(
                {
                    "time": _format_mmss(seconds),
                    "label": _first_sentence_of_bullet(label),
                    "method": "whisper_segment_match",
                    "timestamp_seconds": seconds,
                    "matched": True,
                    "match_source": match.get("source", ""),
                    "match_score": match.get("score", 0),
                    "matched_text": match.get("text", ""),
                    "matched_start": match.get("start", 0),
                    "matched_end": match.get("end", 0),
                    "script_excerpt": _trim_text(_clean_text(str(script_segment.get("text", ""))), limit=90),
                }
            )
        else:
            timeline.append(
                {
                    "time": "",
                    "label": _first_sentence_of_bullet(label),
                    "method": "whisper_unmatched",
                    "matched": False,
                    "match_score": 0,
                    "matched_text": "",
                    "matched_start": "",
                    "matched_end": "",
                    "script_excerpt": _trim_text(_clean_text(str(script_segment.get("text", ""))), limit=90),
                }
            )

    _interpolate_unmatched_timeline_items(timeline, audio_duration_seconds=audio_duration_seconds)
    return timeline


def _detect_silence_boundary_candidates(audio_path: Path) -> list[int]:
    """Detect likely paragraph boundaries from long silences in the raw TTS audio.

    Returns a list of silence_end positions in milliseconds.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not Path(audio_path).exists():
        return []

    duration_ms = (_probe_audio_duration_seconds(audio_path) or 0) * 1000
    expected_gap_ms = max(duration_ms // 20, 8000) if duration_ms else 8000
    configs = [
        (-35, 0.75),
        (-34, 0.70),
        (-33, 0.65),
        (-32, 0.60),
    ]
    best: list[int] = []

    for noise_db, min_duration in configs:
        proc = subprocess.run(
            [
                ffmpeg,
                "-i",
                str(audio_path),
                "-af",
                f"silencedetect=noise={noise_db}dB:d=0.25",
                "-f",
                "null",
                "-",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        text = f"{proc.stdout}\n{proc.stderr}"
        raw_candidates: list[tuple[int, int]] = []
        for start_text, end_text, duration_text in re.findall(
            r"silence_start:\s*([\d.]+).*?silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)",
            text,
            flags=re.S,
        ):
            try:
                end_ms = round(float(end_text) * 1000)
                duration_ms_candidate = round(float(duration_text) * 1000)
            except ValueError:
                continue
            if duration_ms_candidate >= round(min_duration * 1000):
                raw_candidates.append((end_ms, duration_ms_candidate))

        if not raw_candidates:
            continue

        merged: list[tuple[int, int]] = []
        for end_ms, gap_duration_ms in raw_candidates:
            if not merged or end_ms - merged[-1][0] >= expected_gap_ms:
                merged.append((end_ms, gap_duration_ms))
                continue
            if gap_duration_ms > merged[-1][1]:
                merged[-1] = (end_ms, gap_duration_ms)

        candidates = [end_ms for end_ms, _ in merged]
        if len(candidates) > len(best):
            best = candidates
        if candidates:
            return candidates

    return best


def _estimate_segment_boundaries_ms(script_text: str, total_ms: int) -> list[int]:
    segments = _script_timeline_segments(script_text)
    if len(segments) <= 1 or total_ms <= 0:
        return []
    weights = [_spoken_segment_weight(segment["text"]) for segment in segments]
    total_weight = max(sum(weights), 1)
    cumulative_weight = 0
    boundaries: list[int] = []
    for weight in weights[:-1]:
        cumulative_weight += weight
        boundaries.append(round(total_ms * cumulative_weight / total_weight))
    return boundaries


def _snap_boundaries_to_silences(
    estimated_boundaries_ms: list[int],
    candidates_ms: list[int],
    *,
    tolerance_ms: int,
) -> list[int]:
    if not estimated_boundaries_ms or not candidates_ms:
        return []

    selected: list[int] = []
    used: set[int] = set()
    cursor = -1
    for target_ms in estimated_boundaries_ms:
        viable = [
            candidate
            for candidate in candidates_ms
            if candidate not in used and candidate > cursor and abs(candidate - target_ms) <= tolerance_ms
        ]
        if not viable:
            return []
        chosen = min(viable, key=lambda value: abs(value - target_ms))
        selected.append(chosen)
        used.add(chosen)
        cursor = chosen
    return selected


def _build_solo_timeline_from_audio_silence(
    script_text: str,
    voice_audio_path: Path,
    *,
    start_offset_ms: int = 0,
    limit: int = SOLO_SHOWNOTE_TIMELINE_LIMIT,
    bullet_labels: list[str] | None = None,
) -> list[dict[str, str]]:
    segments = _script_timeline_segments(script_text)
    if len(segments) <= 1 or not Path(voice_audio_path).exists():
        return []

    total_seconds = _probe_audio_duration_seconds(voice_audio_path)
    if not total_seconds:
        return []

    candidate_boundaries_ms = _detect_silence_boundary_candidates(voice_audio_path)
    estimated_boundaries_ms = _estimate_segment_boundaries_ms(script_text, total_seconds * 1000)
    if len(candidate_boundaries_ms) < len(segments) - 1 or len(estimated_boundaries_ms) != len(segments) - 1:
        return []

    tolerance_ms = max(int(total_seconds * 1000 / max(len(segments), 1) * 0.45), 9000)
    boundaries_ms = _snap_boundaries_to_silences(
        estimated_boundaries_ms,
        candidate_boundaries_ms,
        tolerance_ms=tolerance_ms,
    )
    if len(boundaries_ms) != len(segments) - 1:
        return []

    segment_starts_ms = [0, *boundaries_ms]
    label_map = _match_bullet_labels_to_segments(segments, bullet_labels)
    timeline: list[dict[str, str]] = []
    content_idx = 0
    for segment_idx, segment in enumerate(segments):
        if segment["kind"] != "content":
            continue

        bullet_label = ""
        if label_map is not None:
            bullet_label = label_map.get(segment_idx, "")
            if not bullet_label:
                # 匹配不上要点的 content 段是判断/边界/总结段，不打时间点
                content_idx += 1
                continue
        elif bullet_labels and content_idx < len(bullet_labels):
            bullet_label = bullet_labels[content_idx]

        label = _first_sentence_of_bullet(bullet_label) if bullet_label else ""
        if not label:
            label = _extract_shownote_bullet_from_paragraph(segment["text"], limit=44)
        if not label:
            label = _trim_text(_clean_text(segment["text"]), limit=44)
        if label:
            total_seconds_with_offset = round(
                (segment_starts_ms[segment_idx] + max(start_offset_ms, 0)) / 1000
            )
            timeline.append(
                {
                    "time": _format_mmss(total_seconds_with_offset),
                    "label": label,
                    "method": "audio_silence_boundaries",
                    "script_excerpt": _trim_text(_clean_text(segment["text"]), limit=90),
                }
            )
            if len(timeline) >= limit:
                break
        content_idx += 1

    return timeline


def _build_solo_timeline(
    script_text: str,
    *,
    audio_duration_seconds: int | None = None,
    limit: int = SOLO_SHOWNOTE_TIMELINE_LIMIT,
    bullet_labels: list[str] | None = None,
) -> list[dict[str, str]]:
    segments = _script_timeline_segments(script_text)
    if not segments:
        return []

    weights = [_spoken_segment_weight(segment["text"]) for segment in segments]
    if audio_duration_seconds is None:
        total_seconds = max(240, round(sum(weights) * 0.42))
    else:
        total_seconds = max(int(audio_duration_seconds), 60)
    total_chars = max(len(script_text), 1)
    label_map = _match_bullet_labels_to_segments(segments, bullet_labels)
    content_idx = 0
    timeline: list[dict[str, str]] = []
    for seg_idx, (segment, weight) in enumerate(zip(segments, weights)):
        try:
            start_char = max(int(segment.get("start_char", "0")), 0)
        except ValueError:
            start_char = 0
        segment_start_seconds = round(total_seconds * min(start_char, total_chars) / total_chars)
        if segment["kind"] == "content":
            if label_map is not None:
                label = _first_sentence_of_bullet(label_map.get(seg_idx, ""))
                if not label:
                    # 匹配不上要点的 content 段是判断/边界/总结段，不打时间点
                    content_idx += 1
                    continue
            elif bullet_labels and content_idx < len(bullet_labels):
                label = _first_sentence_of_bullet(bullet_labels[content_idx])
            else:
                label = _extract_shownote_bullet_from_paragraph(segment["text"], limit=44)
                if not label or _is_shownote_transition_sentence(label) or len(re.sub(r"\s+", "", label)) < 8:
                    fallback_text = re.sub(
                        r"^(?:先看第一条|接下来(?:请关注)?|然后是|此外|最后聊聊|最后一条|第三条|第四条|第五条|第六条)[，。:：\s]*",
                        "",
                        segment["text"],
                    ).strip()
                    label = _extract_shownote_bullet_from_paragraph(fallback_text, limit=44)
                    if not label:
                        label = _trim_text(_clean_text(fallback_text), limit=44)
            content_idx += 1
            if label:
                timeline.append(
                    {
                        "time": _format_mmss(segment_start_seconds),
                        "label": label,
                        "method": "heuristic_script_char_position",
                        "start_char": start_char,
                        "script_excerpt": _trim_text(_clean_text(segment["text"]), limit=90),
                    }
                )
                if len(timeline) >= limit:
                    break
    return timeline


def _render_solo_shownotes(bullets: list[str], timeline: list[dict[str, str]] | None = None) -> str:
    lines = ["本期要点"]
    for index, bullet in enumerate((bullets or ["暂无要点"])[:SOLO_SHOWNOTE_BULLET_LIMIT], start=1):
        lines.append(f"{index}) {bullet}")
        lines.append("")

    if timeline:
        if lines and not lines[-1]:
            lines.pop()
        lines.append("")
        lines.append("时间线")
        for item in timeline:
            timestamp = str(item.get("time", "")).strip()
            label = str(item.get("label", "")).strip()
            if timestamp and label:
                lines.append(f"{timestamp} {label}")
        lines.append("")

    return "\n".join(lines).strip()


__all__ = [name for name in globals() if not name.startswith("__")]
