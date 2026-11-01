"""Top-level orchestration and CLI entrypoints for the solo pipeline."""
from __future__ import annotations

from .shared import *
from .solo_script import *
from .solo_title import *
from .solo_shownotes import *
from .solo_publish import *


def build_solo_episode(args: argparse.Namespace) -> int:
    # Schedule check
    if not args.force:
        try:
            from db.schema import should_run_today
            if not should_run_today("solo"):
                print("今天不在单人口播调度日，跳过。使用 --force 强制执行。")
                return 0
        except Exception:
            pass

    episode = args.episode or next_episode_id()
    ensure_dir(EPISODES_DIR)
    episode_dir = EPISODES_DIR / episode
    ensure_dir(episode_dir)
    _daily_log_set_episode(episode, episode_dir)
    current_step = "初始化"
    title_for_notify = ""
    cover_retry_meta: dict[str, object] | None = None

    try:
        write_text(episode_dir / "flow_type.txt", "solo")
        _daily_log_stage(
            "pipeline_start",
            episode=episode,
            args=vars(args) if hasattr(args, "__dict__") else {},
        )

        current_step = "新闻抓取"
        if args.search_file:
            search_payload = read_json(Path(args.search_file))
            # 外部 search_file 同样必须过一遍硬性时间截断
            search_payload = hard_time_cutoff_filter(search_payload)
        else:
            search_payload = collect_search_data()

        # 注入 follow-builder 补充数据源
        search_payload = inject_follow_builder_into_payload(search_payload)

        # ===================================================================
        # E1: 漏斗统计 funnel_stats
        # 在每一级过滤前后记录候选总数，便于事后定位漏斗瓶颈
        # ===================================================================
        funnel_stats: dict[str, int] = {}
        funnel_stats["raw_after_collect"] = count_payload_candidates(search_payload)

        # B2: 加载 coverage 索引（供"未播报"判定）
        coverage_index = load_coverage_index()

        # 注：collect_search_data 内部已执行 hard_time_cutoff_filter 并把
        # 完整丢弃池写入 _time_cutoff_pool（随缓存持久化）。
        # 这里只需要确认它存在；不要再次调用过滤器，否则会清空池。
        funnel_stats["after_hard_time"] = count_payload_candidates(search_payload)

        # B2: 复活池中 URL/title 不在 coverage 索引里的旧闻
        search_payload = revive_unbroadcast_from_pools(search_payload, coverage_index)
        funnel_stats["after_b2_revive"] = count_payload_candidates(search_payload)

        # 硬性历史拦截：读取最近 N 期脚本的实体（带 C3 TTL 衰减），拦截重复选题
        blocked_entities = extract_recent_script_entities()
        search_payload = hard_history_block_filter(search_payload, blocked_entities)
        funnel_stats["after_hard_history"] = count_payload_candidates(search_payload)

        # B3: backlog 注入未播报候选
        search_payload = inject_backlog_candidates(search_payload, coverage_index)
        funnel_stats["after_b3_backlog"] = count_payload_candidates(search_payload)

        # 把本轮新丢弃的池写入 backlog（供下一轮 B3 复活）
        persist_drop_pools_to_backlog(search_payload)

        # 统计缺省降级标志（在 legacy filter 之前判断更准确）
        candidate_total_after_hard = count_payload_candidates(search_payload)
        scarce_mode = candidate_total_after_hard < 3
        if scarce_mode:
            print(
                f"\033[1;33m[缺省降级] 硬过滤后合格候选仅 {candidate_total_after_hard} 条，"
                f"将在 LLM Context 中注入平淡日提示\033[0m",
                file=sys.stderr,
            )

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

        # 终端打印漏斗
        print(
            "\033[1;36m[漏斗] "
            + " → ".join(f"{k}={v}" for k, v in funnel_stats.items())
            + "\033[0m",
            file=sys.stderr,
        )

        search_payload["quality_summary"] = summarize_search_quality(search_payload)
        _daily_log_stage(
            "funnel",
            episode=episode,
            funnel=dict(funnel_stats),
            quality_summary=dict(search_payload.get("quality_summary", {})),
            b2_revived=(search_payload.get("b2_revival") or {}).get("revived_count", 0),
            b3_backlog=(search_payload.get("b3_backlog") or {}).get("injected_count", 0),
        )
        # 在写入磁盘前剔除内部临时键（_time_cutoff_pool / _history_block_pool 太大）
        for _internal_key in ("_time_cutoff_pool", "_history_block_pool"):
            search_payload.pop(_internal_key, None)
        write_json(episode_dir / "search_results.json", search_payload)
        removed_count = len(search_payload.get("history_filter", {}).get("removed_items", []))
        ai_search_strategy = search_payload.get("ai_news", {}).get("search_strategy", {})
        product_search_strategy = search_payload.get("product_hunt", {}).get("search_strategy", {})
        query_repair_list = []
        for query in (
            search_payload.get("ai_news", {}).get("dynamic_queries", [])
            + search_payload.get("ai_news", {}).get("brave_dynamic_queries", [])
            + ai_search_strategy.get("backfill_queries", [])
            + product_search_strategy.get("supplement_queries", [])
        ):
            query = str(query).strip()
            if query and query not in query_repair_list:
                query_repair_list.append(query)
        ai_result_count = len(bucket_result_items(search_payload.get("ai_news", {})))
        product_result_count = len(bucket_result_items(search_payload.get("product_hunt", {})))
        quality_summary = search_payload.get("quality_summary", {})
        _notify_feishu(
            "AI观测站 · 新闻抓取完成",
            (
                f"期号：{episode}\n"
                f"AI 新闻条目：{ai_result_count}\n"
                f"产品/社区条目：{product_result_count}\n"
                f"历史去重移除：{removed_count}\n"
                f"补漏查询：{len(query_repair_list)}\n"
                f"科技：{quality_summary.get('tech', 0)}\n"
                f"商业：{quality_summary.get('business', 0)}\n"
                f"AI产品：{quality_summary.get('ai_product', 0)}\n"
                f"大模型：{quality_summary.get('model', 0)}\n"
                f"无关内容：{quality_summary.get('irrelevant_total', 0)}"
            ),
        )

        current_step = "研究文档生成"
        research_md = create_solo_research_markdown(search_payload, recent_context)

        current_step = "口播文案生成"
        script_text = create_solo_script(
            research_md,
            search_payload,
            episode_dir=episode_dir,
            scarce=scarce_mode,
        )

        current_step = "标题生成"
        if args.cover_provider == "none":
            # Codex 06:50 会覆盖 title.txt，跳过 LLM 调用，用日期占位符
            title_text = f"AI产业观察 {now_local().strftime('%Y-%m-%d')}"
            print(f"[solo] 跳过 LLM 标题生成（cover_provider=none），占位符: {title_text}")
        else:
            title_text = _generate_solo_title(search_payload, script_text, episode_dir)
        title_for_notify = title_text
        research_md = _replace_research_title(research_md, title_text)
        metadata = parse_research_metadata(research_md)
        metadata["title"] = title_text

        research_json = {
            "meta": {
                "date": now_local().strftime("%Y-%m-%d"),
                "title": metadata["title"],
                "flow_type": "solo",
            },
            "trend_titles": metadata["trend_titles"],
            "products": [{"name": name} for name in metadata["product_titles"]],
            "sources": (
                summarize_sources(search_payload["ai_news"])
                + summarize_sources(search_payload["product_hunt"])
            ),
        }

        write_text(episode_dir / "research.md", research_md)
        _write_solo_script_files(episode_dir, script_text)
        write_text(episode_dir / "title.txt", metadata["title"])
        write_json(episode_dir / "research.json", research_json)

        # B2: 把本期实际消耗的候选写入 coverage 索引（供未来"未播报"判定）
        try:
            broadcast_items = bucket_result_items(search_payload.get("ai_news", {}) or {}) + \
                              bucket_result_items(search_payload.get("product_hunt", {}) or {})
            update_coverage_index_for_episode(episode, broadcast_items)
        except Exception as exc:
            print(f"[B2] 更新 coverage 索引失败: {exc}", file=sys.stderr)

        current_step = "Shownotes 生成"
        shownote_bullets = _build_solo_shownote_bullets(search_payload, script_text)
        write_json(episode_dir / "shownotes_bullets.json", shownote_bullets)
        shownotes_text = _render_solo_shownotes(shownote_bullets)
        write_text(episode_dir / "shownotes.txt", shownotes_text)

        cover_path = ""
        cover_brief = ""
        title_regen_payload: dict[str, object] = {}
        if args.cover_provider != "none":
            current_step = "封面生成"
            print("[solo] 生成封面图片...")
            temp_record = {
                "标题": metadata["title"],
                "三大趋势": "\n".join(metadata["trend_titles"]),
                "产品洞察": "\n".join(metadata["product_titles"]),
                "flow_type": "solo",
            }
            write_json(episode_dir / "record.json", temp_record)
            regen_result = regenerate_cover_for_episode(episode_dir, provider=args.cover_provider)
            if regen_result.get("cover_path"):
                cover_path = regen_result["cover_path"]
                title_regen = generate_episode_title_after_cover(episode_dir, episode_dir / "podcast_script.txt")
                metadata["title"] = str(title_regen["new_title"])
                title_for_notify = metadata["title"]
                title_regen_payload = title_regen
                cover_brief = _cover_brief_from_meta(episode_dir)
            else:
                cover_retry_meta = _schedule_cover_retry(
                    episode_dir,
                    provider=args.cover_provider,
                    publish_after_retry=bool(args.publish),
                )
                _update_record(
                    episode_dir,
                    发布状态="cover_retry_pending",
                    封面路径="",
                    flow_type="solo",
                )
                retry_time = str(cover_retry_meta.get("scheduled_at", "")).replace("T", " ")
                _notify_step_failure(
                    episode=episode,
                    step="封面生成",
                    error=str(regen_result.get("error", "封面生成失败")),
                    title=metadata["title"],
                    next_step=(
                        f"已安排 {retry_time} 自动重试封面。"
                        + (" 重试成功后会继续提交小宇宙。" if args.publish else " 重试成功后等待人工确认发布。")
                    ),
                )
                print("[solo] 封面生成失败，已安排 1 小时后自动重试", file=sys.stderr)

        draft_status = "draft_assets_ready" if cover_path else "cover_retry_pending"
        if args.draft_only:
            draft_record = {
                "日期": now_local().strftime("%Y-%m-%d"),
                "期数": episode,
                "标题": metadata["title"],
                "三大趋势": "\n".join(metadata["trend_titles"]),
                "产品洞察": "\n".join(metadata["product_titles"]),
                "研究文档路径": str(episode_dir / "research.md"),
                "口播稿路径": str(episode_dir / "podcast_script.txt"),
                "音频路径": "",
                "TTS音色": "",
                "封面路径": cover_path,
                "Shownotes路径": str(episode_dir / "shownotes.txt"),
                "发布状态": draft_status,
                "ListenHub作品链接": "",
                "ListenHub标题": "",
                "flow_type": "solo",
            }
            if title_regen_payload:
                draft_record.update(
                    {
                        "title_regenerated_by_codex": True,
                        "old_title": title_regen_payload.get("old_title", ""),
                        "new_title": title_regen_payload.get("new_title", ""),
                        "title_source_script_path": title_regen_payload.get("title_source_script_path", ""),
                        "title_generated_at": title_regen_payload.get("title_generated_at", ""),
                        "title_generation_fallback": bool(title_regen_payload.get("title_generation_fallback")),
                    }
                )
            write_json(episode_dir / "record.json", draft_record)
            try:
                from db.schema import upsert_episode
                upsert_episode({
                    "id": episode,
                    "date": draft_record["日期"],
                    "flow_type": "solo",
                    "title": draft_record["标题"],
                    "trends": json.dumps(metadata["trend_titles"], ensure_ascii=False),
                    "products": json.dumps(metadata["product_titles"], ensure_ascii=False),
                    "script_path": draft_record["口播稿路径"],
                    "audio_path": "",
                    "cover_path": draft_record["封面路径"],
                    "status": "draft_ready" if cover_path else "cover_retry_pending",
                })
            except Exception as exc:
                print(f"[solo] SQLite 写入跳过: {exc}", file=sys.stderr)
            _notify_episode_stage(
                stage_title="AI观测站 · 草稿素材已就绪",
                progress_text="标题、口播稿、shownotes已完成。"
                + (" 封面已完成，TTS 未开始。" if cover_path else " 封面失败，已安排 1 小时后自动重试。"),
                episode=episode,
                title=metadata["title"],
                shownotes=shownotes_text,
                cover_brief=cover_brief,
                next_step=(
                    "人工确认后生成 TTS；后续内容稳定后可直接切换为自动发布。"
                    if cover_path
                    else "等待封面自动重试；重试成功后再确认并发布。"
                ),
            )
            # 手机端审稿需要看到完整内容：口播稿全文分段跟在草稿通知后面。
            # 注意这是 pipeline 初稿，codex 凌晨重写后由 notify-final-script 再推终稿。
            try:
                from mobile_notify import notify_feishu_long_text
                notify_feishu_long_text(
                    f"AI观测站 · {episode} 口播稿全文（初稿）",
                    script_text,
                )
            except Exception as exc:
                print(f"[solo] 口播稿全文推送失败: {exc}", file=sys.stderr)
            # 记录初稿 hash：notify-final-script 据此区分"codex 终稿"和"初稿未被改写"
            try:
                import hashlib as _hashlib
                write_json(
                    episode_dir / "script_notify_state.json",
                    {
                        "draft_sha256": _hashlib.sha256(script_text.strip().encode("utf-8")).hexdigest(),
                        "draft_notified_at": now_local().isoformat(),
                    },
                )
            except Exception as exc:
                print(f"[solo] 初稿通知状态写入失败: {exc}", file=sys.stderr)
            print(episode_dir)
            return 0

        current_step = "TTS 合成（整段模式）"
        audio_path = episode_dir / "podcast_solo.mp3"
        # 语速：默认 1.30（"资讯快语速版"，Ann 2026-04 确认为每日正式版本）
        # 如需临时回退，设置环境变量 SOLO_TTS_SPEED=1.10 即可
        import os as _os
        tts_speed = float(_os.environ.get("SOLO_TTS_SPEED", "1.30"))
        print(f"[solo] 整段口播稿调用 MiniMax TTS (speed={tts_speed})...")

        voice_path = episode_dir / "podcast_voice_raw.mp3"
        tts_result = minimax_tts_with_alignment(
            script_text,
            voice_path,
            voice_id=args.voice_id or None,
            rotation_key=episode,
            speed=tts_speed,
        )
        selected_voice_id = tts_result.voice_id
        subtitle_count = len(tts_result.subtitles)
        if subtitle_count:
            print(f"[solo] MiniMax 返回 {subtitle_count} 条字幕片段，shownotes 时间线走字幕对齐模式。")
        else:
            print("[solo] MiniMax 未返回可用字幕，shownotes 时间线将回退到估算模式。", file=sys.stderr)

        current_step = "音频混音（整段模式）"
        print("[solo] 整段混音：保留开场/底垫/尾声...")
        audio_path = mix_podcast_audio(
            voice_path,
            audio_path,
        )
        timeline = _build_solo_timeline_from_subtitles(
            script_text,
            tts_result.subtitles,
            start_offset_ms=voice_start_delay_ms(),
            bullet_labels=shownote_bullets,
        )
        if not timeline:
            timeline = _build_solo_timeline_from_audio_silence(
                script_text,
                voice_path,
                start_offset_ms=voice_start_delay_ms(),
                bullet_labels=shownote_bullets,
            )
        if not timeline:
            audio_duration_seconds = _probe_audio_duration_seconds(audio_path)
            timeline = _build_solo_timeline(
                script_text,
                audio_duration_seconds=audio_duration_seconds,
                bullet_labels=shownote_bullets,
            )

        current_step = "精华片段生成"
        highlight_path = episode_dir / "highlight_15s.mp3"
        print("[solo] 生成15秒精华片段...")
        generate_highlight_clip(audio_path, highlight_path)

        shownotes_text = _render_solo_shownotes(shownote_bullets, timeline)
        write_text(episode_dir / "shownotes.txt", shownotes_text)

        publish_requested = bool(args.publish and cover_path)
        row = {
            "日期": now_local().strftime("%Y-%m-%d"),
            "期数": episode,
            "标题": metadata["title"],
            "三大趋势": "\n".join(metadata["trend_titles"]),
            "产品洞察": "\n".join(metadata["product_titles"]),
            "研究文档路径": str(episode_dir / "research.md"),
            "口播稿路径": str(episode_dir / "podcast_script.txt"),
            "音频路径": str(audio_path),
            "TTS音色": selected_voice_id,
            "封面路径": cover_path,
            "Shownotes路径": str(episode_dir / "shownotes.txt"),
            "发布状态": (
                "publishing"
                if publish_requested
                else ("cover_retry_pending" if cover_retry_meta else "pending_confirmation")
            ),
            "ListenHub作品链接": "",
            "ListenHub标题": "",
            "flow_type": "solo",
        }
        if title_regen_payload:
            row.update(
                {
                    "title_regenerated_by_codex": True,
                    "old_title": title_regen_payload.get("old_title", ""),
                    "new_title": title_regen_payload.get("new_title", ""),
                    "title_source_script_path": title_regen_payload.get("title_source_script_path", ""),
                    "title_generated_at": title_regen_payload.get("title_generated_at", ""),
                    "title_generation_fallback": bool(title_regen_payload.get("title_generation_fallback")),
                }
            )
        append_record_csv(
            {
                "日期": row["日期"],
                "期数": row["期数"],
                "标题": row["标题"],
                "三大趋势": row["三大趋势"],
                "产品洞察": row["产品洞察"],
                "研究文档路径": row["研究文档路径"],
                "口播稿路径": row["口播稿路径"],
                "音频路径": row["音频路径"],
                "TTS音色": row["TTS音色"],
                "封面路径": row["封面路径"],
                "ListenHub作品链接": row["ListenHub作品链接"],
                "ListenHub标题": row["ListenHub标题"],
                "flow_type": row["flow_type"],
            }
        )
        write_json(episode_dir / "record.json", row)

        try:
            from db.schema import upsert_episode
            upsert_episode({
                "id": episode,
                "date": row["日期"],
                "flow_type": "solo",
                "title": row["标题"],
                "trends": json.dumps(metadata["trend_titles"], ensure_ascii=False),
                "products": json.dumps(metadata["product_titles"], ensure_ascii=False),
                "script_path": row["口播稿路径"],
                "audio_path": row["音频路径"],
                "cover_path": row["封面路径"],
                "status": row["发布状态"],
            })
        except Exception as exc:
            print(f"[solo] SQLite 写入跳过: {exc}", file=sys.stderr)

        _notify_episode_stage(
            stage_title="AI观测站 · TTS 已完成",
            progress_text=(
                "标题、shownotes、封面、TTS 已完成。正在提交小宇宙。"
                if publish_requested
                else (
                    "标题、shownotes、TTS 已完成；封面失败，已安排自动重试。"
                    if cover_retry_meta
                    else "标题、shownotes、封面、TTS 已完成，当前等待人工确认发布。"
                )
            ),
            episode=episode,
            title=metadata["title"],
            shownotes=shownotes_text,
            cover_brief=cover_brief,
            audio_path=audio_path,
            next_step=(
                "小宇宙发布成功后会继续推送状态。"
                if publish_requested
                else (
                    "封面自动重试成功后会继续提交小宇宙。"
                    if cover_retry_meta and args.publish
                    else "默认不自动发布；内容稳定后可切到自动发布。"
                )
            ),
        )
        # TTS 消费的就是 codex 终稿——确认发布前把实际会播出的全文推到飞书
        try:
            from mobile_notify import notify_feishu_long_text
            final_script = (episode_dir / "podcast_script.txt").read_text(encoding="utf-8")
            notify_feishu_long_text(
                f"AI观测站 · {episode} 口播稿全文（终稿，已合成音频）",
                final_script,
            )
        except Exception as exc:
            print(f"[solo] 终稿全文推送失败: {exc}", file=sys.stderr)

        _daily_log_stage(
            "shownotes_ready",
            episode=episode,
            title=metadata.get("title", ""),
            shownotes_bullets=len([l for l in shownotes_text.splitlines() if re.match(r"^\d+\)", l.strip())]),
            audio=str(audio_path.name) if audio_path.exists() else "",
        )

        if publish_requested:
            current_step = "提交小宇宙"
            print("[solo] 发布到小宇宙...")
            publish_script = ROOT_DIR / "xiaoyuzhou_prepare_episode.sh"
            if publish_script.exists():
                result = subprocess.run(
                    ["zsh", str(publish_script), str(episode_dir)],
                    cwd=str(ROOT_DIR),
                )
                if result.returncode != 0:
                    _daily_log_stage(
                        "publish",
                        episode=episode,
                        result="fail",
                        exit_code=result.returncode,
                    )
                    raise RuntimeError(f"小宇宙发布脚本退出码 {result.returncode}")
                _daily_log_stage(
                    "publish",
                    episode=episode,
                    result="ok",
                    title=metadata.get("title", ""),
                )
                try:
                    update_coverage_index_for_episode(episode, broadcast_items)
                except Exception as exc:
                    print(f"[B2] 发布后刷新 coverage 索引失败: {exc}", file=sys.stderr)
            else:
                raise RuntimeError("未找到 xiaoyuzhou_prepare_episode.sh")
        elif args.publish and cover_retry_meta:
            print("[solo] 当前跳过小宇宙发布，等待封面自动重试成功后补发。")
            _daily_log_stage(
                "publish",
                episode=episode,
                result="deferred",
                reason="cover_retry_pending",
            )

        print(episode_dir)
        return 0
    except Exception as exc:
        _daily_log_error("pipeline", exc, episode=episode, step=current_step)
        _notify_step_failure(
            episode=episode,
            step=current_step,
            error=str(exc),
            title=title_for_notify,
            next_step="请检查日志并重试当前步骤。",
        )
        raise


def retry_cover_mode(args: argparse.Namespace) -> int:
    episode_dir = EPISODES_DIR / args.episode
    if not episode_dir.is_dir():
        raise RuntimeError(f"目录不存在: {episode_dir}")

    meta_path = _cover_retry_meta_path(episode_dir)
    retry_meta = read_json(meta_path) if meta_path.exists() else {}
    if args.delay_seconds > 0:
        time.sleep(args.delay_seconds)

    retry_meta.update(
        {
            "status": "running",
            "episode": args.episode,
            "provider": args.provider,
            "publish_after_retry": bool(args.publish_after_retry or retry_meta.get("publish_after_retry")),
            "started_at": now_local().isoformat(timespec="seconds"),
        }
    )
    write_json(meta_path, retry_meta)

    regen_result = regenerate_cover_for_episode(episode_dir, provider=args.provider)
    publish_after_retry = bool(retry_meta.get("publish_after_retry"))
    audio_path = episode_dir / "podcast_solo.mp3"

    if regen_result.get("cover_path"):
        retry_meta.update(
            {
                "status": "success",
                "completed_at": now_local().isoformat(timespec="seconds"),
                "cover_path": regen_result.get("cover_path", ""),
            }
        )
        write_json(meta_path, retry_meta)
        _update_record(
            episode_dir,
            发布状态="publishing" if publish_after_retry and audio_path.exists() else "pending_confirmation",
            封面路径=regen_result.get("cover_path", ""),
            flow_type="solo",
        )
        title_regen = generate_episode_title_after_cover(episode_dir)

        try:
            from db.schema import get_episode, upsert_episode
            existing = get_episode(args.episode) or {}
            upsert_episode(
                {
                    "id": args.episode,
                    "date": existing.get("date") or now_local().strftime("%Y-%m-%d"),
                    "flow_type": existing.get("flow_type") or "solo",
                    "title": str(title_regen.get("new_title") or existing.get("title") or ""),
                    "trends": existing.get("trends"),
                    "products": existing.get("products"),
                    "script_path": existing.get("script_path") or str(episode_dir / "podcast_script.txt"),
                    "audio_path": existing.get("audio_path") or (str(audio_path) if audio_path.exists() else ""),
                    "cover_path": regen_result.get("cover_path", ""),
                    "status": "publishing" if publish_after_retry and audio_path.exists() else "pending_confirmation",
                }
            )
        except Exception as exc:
            print(f"[solo] cover retry SQLite 写入跳过: {exc}", file=sys.stderr)

        sections = [
            {"label": "期号", "text": args.episode},
            {"label": "结果", "text": "封面重试成功"},
            {"label": "下一步", "text": "继续自动发布小宇宙。" if publish_after_retry and audio_path.exists() else "封面已补齐，可继续人工确认发布。"},
        ]
        cover_brief = _cover_brief_from_meta(episode_dir)
        if cover_brief:
            sections.append({"label": "封面构图", "text": cover_brief})
        _notify_feishu_rich("AI观测站 · 封面重试成功", sections, desktop=False)

        if publish_after_retry and audio_path.exists():
            try:
                publish_script = ROOT_DIR / "xiaoyuzhou_prepare_episode.sh"
                if not publish_script.exists():
                    raise RuntimeError("封面重试成功，但缺少 xiaoyuzhou_prepare_episode.sh")
                result = subprocess.run(
                    ["zsh", str(publish_script), str(episode_dir)],
                    cwd=str(ROOT_DIR),
                )
                if result.returncode != 0:
                    raise RuntimeError(f"封面重试成功，但小宇宙发布脚本退出码 {result.returncode}")
                try:
                    sr = read_json(episode_dir / "search_results.json")
                    published_items = bucket_result_items(sr.get("ai_news", {}) or {}) + \
                                      bucket_result_items(sr.get("product_hunt", {}) or {})
                    update_coverage_index_for_episode(args.episode, published_items)
                except Exception as exc:
                    print(f"[B2] 封面重试发布后刷新 coverage 索引失败: {exc}", file=sys.stderr)
                retry_meta.update(
                    {
                        "status": "published_after_retry",
                        "published_at": now_local().isoformat(timespec="seconds"),
                    }
                )
                write_json(meta_path, retry_meta)
            except Exception as exc:
                retry_meta.update(
                    {
                        "status": "publish_failed_after_cover_retry",
                        "error": str(exc),
                        "completed_at": now_local().isoformat(timespec="seconds"),
                    }
                )
                write_json(meta_path, retry_meta)
                _notify_step_failure(
                    episode=args.episode,
                    step="封面重试后自动发布",
                    error=str(exc),
                    title=read_json(episode_dir / "record.json").get("标题", ""),
                    next_step="封面已补齐，请检查小宇宙发布日志后手动重试。",
                )
                return 1
        return 0

    retry_meta.update(
        {
            "status": "failed",
            "completed_at": now_local().isoformat(timespec="seconds"),
            "error": str(regen_result.get("error", "封面重试失败")),
        }
    )
    write_json(meta_path, retry_meta)
    _update_record(episode_dir, 发布状态="cover_retry_failed", flow_type="solo")
    _notify_step_failure(
        episode=args.episode,
        step="封面重试",
        error=str(regen_result.get("error", "封面重试失败")),
        title=read_json(episode_dir / "record.json").get("标题", ""),
        next_step="请手动检查封面提示词、图片接口和日志。",
    )
    return 1


def _compose_cover(bg_path: Path, out_path: Path, title: str) -> None:
    """Overlay title text on cover background using compose_cover_title module."""
    try:
        sys.path.insert(0, str(ROOT_DIR))
        from compose_cover_title import compose_cover
        compose_cover(str(bg_path), str(out_path), title)
    except Exception as exc:
        print(f"[solo] 封面合成失败 (non-fatal): {exc}", file=sys.stderr)
        import shutil
        shutil.copy(str(bg_path), str(out_path))


def synthesize_tts_mode(args: argparse.Namespace) -> int:
    """从 Codex 稿（或原稿）合成 TTS，并重建带时间线的 shownotes。"""
    episode_dir = EPISODES_DIR / args.episode
    if not episode_dir.is_dir():
        print(f"[tts] 目录不存在: {episode_dir}", file=sys.stderr)
        return 1

    codex_script = episode_dir / "podcast_script_codex.txt"
    original_script = episode_dir / "podcast_script.txt"
    script_path = codex_script if codex_script.exists() else original_script
    if not script_path.exists():
        print(f"[tts] 找不到口播稿: {episode_dir}", file=sys.stderr)
        return 1

    script_text = script_path.read_text(encoding="utf-8").strip()
    print(f"[tts] 使用稿件: {script_path.name}（{len(script_text)} 字符）")

    import os as _os
    tts_speed = float(_os.environ.get("SOLO_TTS_SPEED", "1.30"))

    voice_path = episode_dir / "podcast_voice_raw.mp3"
    audio_path = episode_dir / "podcast_solo.mp3"
    highlight_path = episode_dir / "highlight_15s.mp3"

    # 备份旧音频（如果已有）
    for src in [voice_path, audio_path, highlight_path]:
        if src.exists():
            bak = src.with_name(src.stem + "_before_regen" + src.suffix)
            if not bak.exists():
                src.rename(bak)
                print(f"[tts] 备份: {src.name} → {bak.name}")

    # 备份旧 shownotes
    sn_path = episode_dir / "shownotes.txt"
    if sn_path.exists():
        bak_sn = episode_dir / "shownotes_before_regen.txt"
        if not bak_sn.exists():
            shutil.copy(str(sn_path), str(bak_sn))

    # 重建 shownotes bullet 列表（先做，以便给 chunk_labels 提供新闻段计数参考）
    # 优先复用初次生成时保存的要点，避免重复调用 LLM（与 regen_shownotes 路径保持一致）
    # 例外：若 podcast_script.txt 比缓存更新（Codex 改稿），强制重新提炼
    bullets_cache_path = episode_dir / "shownotes_bullets.json"
    _tts_script_mtime = script_path.stat().st_mtime if script_path.exists() else 0
    _tts_bullets_mtime = bullets_cache_path.stat().st_mtime if bullets_cache_path.exists() else 0
    _tts_bullets_stale = _tts_script_mtime > _tts_bullets_mtime + 5
    shownote_bullets: list[str] = []
    if bullets_cache_path.exists() and not _tts_bullets_stale:
        _cached = read_json(bullets_cache_path)
        if _cached and isinstance(_cached, list):
            shownote_bullets = [str(b) for b in _cached if b]
            print(f"[tts] 使用已保存的要点（{len(shownote_bullets)} 条，跳过 LLM 重新生成）")
    if not shownote_bullets:
        if _tts_bullets_stale:
            print(f"[tts] 口播稿比要点缓存更新（Codex 改稿），强制重新提炼要点")
        search_results_path = episode_dir / "search_results.json"
        search_payload = read_json(search_results_path) if search_results_path.exists() else {}
        shownote_bullets = _build_solo_shownote_bullets(search_payload, script_text)
        write_json(bullets_cache_path, shownote_bullets)

    # 按段落拆分脚本 → 逐段 TTS → SFX 转场混音
    _SKIP_MARKERS = (
        "嗨，早上好", "早上好，今天是", "以上就是今天", "欢迎订阅",
        "今天的播报到这里结束", "我们明天", "好，我们一条一条来看",
        "真正跑赢这一轮", "如果今天这些新闻必须浓缩成一句话",
    )
    all_paragraphs = [p.strip() for p in re.split(r"\n{2,}", script_text) if p.strip()]
    # 开场/结尾段标空标签（不进 timeline），新闻段取第一个完整句子
    chunk_labels: list[str] = []
    for para in all_paragraphs:
        if any(m in para for m in _SKIP_MARKERS) or len(re.sub(r"\s+", "", para)) < 20:
            chunk_labels.append("")
        else:
            chunk_labels.append(_first_sentence_of_bullet(para))
    print(f"[tts] 脚本共 {len(all_paragraphs)} 段，其中 {sum(1 for l in chunk_labels if l)} 段进入时间线")

    # 续接段落前缀：这类段落是对上一段新闻的补充分析，不插 SFX 转场
    _CONTINUATION_PREFIXES = (
        "这个", "这里", "这条", "这份", "这还", "这一", "这不",
        "但", "不过", "然而",
    )

    def _is_continuation(para: str) -> bool:
        return any(para.startswith(p) for p in _CONTINUATION_PREFIXES)

    # sfx_at：在 chunk i 和 chunk i+1 之间插 SFX，当且仅当 i+1 是新话题起始段
    sfx_at: set[int] = set()
    for i in range(len(all_paragraphs) - 1):
        next_para = all_paragraphs[i + 1]
        next_label = chunk_labels[i + 1]
        if next_label and not _is_continuation(next_para):
            sfx_at.add(i)
    print(f"[tts] SFX 边界：{len(sfx_at)} 个（共 {len(all_paragraphs)-1} 个段间隔）")

    print(f"[tts] 调用 MiniMax TTS 切片模式 (speed={tts_speed})...")
    chunks_dir = episode_dir / "tts_chunks"
    chunks_dir.mkdir(exist_ok=True)
    wav_paths, selected_voice_id = minimax_tts_chunks(
        all_paragraphs,
        chunks_dir,
        voice_id=args.voice_id or None,
        rotation_key=args.episode,
        speed=tts_speed,
    )
    print(f"[tts] 切片 TTS 完成，声音={selected_voice_id}，{len(wav_paths)} 个 WAV")

    print("[tts] 切片混音（SFX 转场 + BGM）...")
    audio_path, chunk_timeline = mix_podcast_from_chunks(
        wav_paths,
        chunk_labels,
        audio_path,
        sfx_at=sfx_at,
    )

    # 清理临时 WAV 切片
    shutil.rmtree(str(chunks_dir), ignore_errors=True)

    # 持久化 chunk_timeline 供 regen-shownotes 复用（精准 SFX 时间戳）
    chunk_timeline_path = episode_dir / "tts_chunk_timeline.json"
    write_json(chunk_timeline_path, chunk_timeline)
    print(f"[tts] chunk_timeline 已保存: {chunk_timeline_path.name}（{len(chunk_timeline)} 条）")

    print("[tts] 生成15秒精华片段...")
    generate_highlight_clip(audio_path, highlight_path)

    # chunk_timeline 已有按实际音频时长计算的精准时间戳，直接用于 shownotes
    # timeline 条数 = 非空标签段数，与 shownote_bullets 按 index 对应
    timeline = chunk_timeline
    if not timeline:
        audio_duration_seconds = _probe_audio_duration_seconds(audio_path)
        timeline = _build_solo_timeline(
            script_text, audio_duration_seconds=audio_duration_seconds,
            bullet_labels=shownote_bullets,
        )

    shownotes_text = _render_solo_shownotes(shownote_bullets, timeline)
    write_text(sn_path, shownotes_text)
    print(f"[tts] shownotes 已重建（{len(timeline)} 个时间点）")

    # 同步 codex 终版标题到 record/DB：title.txt 由 codex 凌晨改写，
    # 不同步的话 TTS 后各处（通知卡片、后台列表）仍显示 build 阶段的占位标题
    title_path = episode_dir / "title.txt"
    final_title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
    record_updates: dict[str, str] = {
        "音频路径": str(audio_path),
        "TTS音色": selected_voice_id,
        "口播稿路径": str(script_path),
        "发布状态": "pending_confirmation",
    }
    if final_title:
        record_updates["标题"] = final_title
        print(f"[tts] record 标题已同步: {final_title}")
    _update_record(episode_dir, **record_updates)
    try:
        from db.schema import get_episode, upsert_episode
        existing = get_episode(args.episode) or {}
        upsert_episode({
            "id": args.episode,
            "date": existing.get("date") or now_local().strftime("%Y-%m-%d"),
            "flow_type": "solo",
            "title": final_title or existing.get("title") or "",
            "audio_path": str(audio_path),
            "cover_path": existing.get("cover_path") or "",
            "script_path": str(script_path),
            "status": "pending_confirmation",
        })
    except Exception as exc:
        print(f"[tts] SQLite 写入跳过: {exc}", file=sys.stderr)

    # 与后台「更新Shownotes」按钮完全一致地再跑一遍 regen（复用 chunk 时间线和要点缓存，
    # 无额外 LLM 调用），保证飞书链路产物与后台手工流程零差异
    print("[tts] 串行执行 regen-shownotes（与后台按钮同一路径）...")
    regen_args = argparse.Namespace(
        episode=args.episode, timecode_mode="whisper", force_recompute_timecodes=False,
    )
    regen_code = regen_shownotes_mode(regen_args)
    if regen_code != 0:
        print(f"[tts] WARN: regen-shownotes 返回 {regen_code}（TTS 本身已成功）", file=sys.stderr)

    print(f"[tts] 完成：{args.episode} → 待发布")
    return 0


def gen_cover_title_mode(args: argparse.Namespace) -> int:
    """生成封面和标题（不重新合成TTS）。"""
    episode_dir = EPISODES_DIR / args.episode
    if not episode_dir.is_dir():
        print(f"[gen] 目录不存在: {episode_dir}", file=sys.stderr)
        return 1

    # ── Codex 封面优先：只要 cover_codex_candidate_1.png 存在，
    #    无论 --force 是否设置，都直接提升并刷新标题，不再调用 Gemini。
    codex_cover_candidate = episode_dir / "cover_codex_candidate_1.png"
    if codex_cover_candidate.exists():
        cover_jpeg = episode_dir / "cover.jpeg"
        backup = episode_dir / "cover_before_codex.jpeg"
        # 幂等备份：仅首次提升时备份旧封面
        if cover_jpeg.exists() and not backup.exists():
            cover_jpeg.rename(backup)
            print(f"[gen] 旧封面已备份: {backup.name}")
        subprocess.run(
            ["sips", "-s", "format", "jpeg",
             str(codex_cover_candidate), "--out", str(cover_jpeg)],
            check=True, capture_output=True,
        )
        cover_path_str = str(cover_jpeg)
        print(f"[gen] Codex 封面已提升为正式封面: {cover_jpeg.name}")

        # 优先使用 Codex 已写好的 title.txt，质检通过则直接用，不重复调 LLM
        title_path = episode_dir / "title.txt"
        existing_title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
        if existing_title and _codex_regen_title_quality_ok(existing_title):
            title = existing_title
            print(f"[gen] 使用 Codex 已生成的标题（跳过 LLM）: {title}")
        else:
            print(f"[gen] title.txt 为空或质检不过（{existing_title!r}），调 LLM 重新生成...")
            title_regen = generate_episode_title_after_cover(episode_dir)
            title = str(title_regen.get("new_title") or "").strip()
            if not title:
                record_path = episode_dir / "record.json"
                title = (read_json(record_path) if record_path.exists() else {}).get("标题", "")
        print(f"[gen] 最终标题: {title}")
        _update_record(episode_dir, 标题=title, 封面路径=cover_path_str)
        try:
            from db.schema import get_episode, upsert_episode
            existing_db = get_episode(args.episode) or {}
            upsert_episode({
                "id": args.episode,
                "date": existing_db.get("date") or now_local().strftime("%Y-%m-%d"),
                "flow_type": existing_db.get("flow_type") or "solo",
                "title": title,
                "audio_path": existing_db.get("audio_path") or "",
                "cover_path": cover_path_str,
                "script_path": existing_db.get("script_path") or "",
                "status": existing_db.get("status") or "pending_confirmation",
            })
        except Exception as exc:
            print(f"[gen] SQLite 写入跳过: {exc}", file=sys.stderr)
        print("[gen] 完成：Codex 封面和标题已就绪（未调用 Gemini）")
        return 0

    record_path = episode_dir / "record.json"
    record = read_json(record_path) if record_path.exists() else {}

    existing_title = record.get("标题", "").strip()
    existing_cover = record.get("封面路径", "").strip()
    cover_exists = bool(existing_cover and Path(existing_cover).exists())

    need_title = not existing_title or args.force
    need_cover = not cover_exists or args.force

    if not need_title and not need_cover:
        print("[gen] 标题和封面均已存在（使用 --force 覆盖）")
        print(f"[gen] 标题: {existing_title}")
        print(f"[gen] 封面: {existing_cover}")
        return 0

    codex_script = episode_dir / "podcast_script_codex.txt"
    original_script = episode_dir / "podcast_script.txt"
    script_path = codex_script if codex_script.exists() else original_script
    if not script_path.exists():
        print(f"[gen] 找不到口播稿: {episode_dir}", file=sys.stderr)
        return 1

    script_text = script_path.read_text(encoding="utf-8").strip()
    search_results_path = episode_dir / "search_results.json"
    search_payload = read_json(search_results_path) if search_results_path.exists() else {}

    if need_title:
        print("[gen] 通过 LLM 生成标题...")
        title = _generate_solo_title(search_payload, script_text, episode_dir)
        print(f"[gen] 新标题: {title}")
        record["标题"] = title
        write_json(record_path, record)
    else:
        title = existing_title
        print(f"[gen] 保留现有标题: {title}")

    cover_path_str = existing_cover
    if need_cover:
        print(f"[gen] 生成封面（{args.provider}）...")
        regen_result = regenerate_cover_for_episode(episode_dir, provider=args.provider)
        if regen_result.get("cover_path"):
            cover_path_str = regen_result["cover_path"]
            print(f"[gen] 封面已生成: {cover_path_str}")
            title_regen = generate_episode_title_after_cover(episode_dir, script_path)
            title = str(title_regen["new_title"])
            print(f"[gen] Codex 后置标题: {title}")
        else:
            print(f"[gen] 封面生成失败: {regen_result.get('error', '未知错误')}", file=sys.stderr)
            return 1
    else:
        print(f"[gen] 保留现有封面: {existing_cover}")

    _update_record(episode_dir, 标题=title, 封面路径=cover_path_str)
    try:
        from db.schema import get_episode, upsert_episode
        existing = get_episode(args.episode) or {}
        upsert_episode({
            "id": args.episode,
            "date": existing.get("date") or record.get("日期") or now_local().strftime("%Y-%m-%d"),
            "flow_type": existing.get("flow_type") or "solo",
            "title": title,
            "audio_path": existing.get("audio_path") or record.get("音频路径", ""),
            "cover_path": cover_path_str,
            "script_path": existing.get("script_path") or str(script_path),
            "status": existing.get("status") or record.get("发布状态") or "pending_confirmation",
        })
    except Exception as exc:
        print(f"[gen] SQLite 写入跳过: {exc}", file=sys.stderr)

    print("[gen] 完成：封面和标题已更新")
    return 0


def regen_title_after_cover_mode(args: argparse.Namespace) -> int:
    episode_dir = EPISODES_DIR / args.episode
    if not episode_dir.is_dir():
        print(f"[title] 目录不存在: {episode_dir}", file=sys.stderr)
        return 1
    script_path = Path(args.script_path).expanduser() if args.script_path else None
    result = generate_episode_title_after_cover(episode_dir, script_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _shownotes_file_summary(path: Path) -> dict[str, object]:
    import hashlib

    if not path.exists():
        return {
            "path": str(path.resolve()),
            "exists": False,
            "mtime": "",
            "sha256": "",
            "timecodes": [],
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "path": str(path.resolve()),
        "exists": True,
        "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "timecodes": re.findall(r"(?<!\d)(\d{1,3}:[0-5]\d)(?!\d)", text),
    }


def regen_shownotes_mode(args: argparse.Namespace) -> int:
    """从已有音频和口播稿重建 shownotes 时间线。"""
    import hashlib

    episode_dir = EPISODES_DIR / args.episode
    if not episode_dir.is_dir():
        print(f"[sn] 目录不存在: {episode_dir}", file=sys.stderr)
        return 1

    audio_path = episode_dir / "podcast_solo.mp3"
    if not audio_path.exists():
        for name in ("podcast.mp3",):
            p = episode_dir / name
            if p.exists():
                audio_path = p
                break
        else:
            print("[sn] 找不到音频文件，请先合成 TTS", file=sys.stderr)
            return 1

    script_path = episode_dir / "podcast_script.txt"
    if not script_path.exists():
        print(f"[sn] 找不到口播稿: {episode_dir}", file=sys.stderr)
        return 1

    script_text = script_path.read_text(encoding="utf-8").strip()
    total_script_chars = len(script_text)
    print(f"[sn] 稿件: {script_path.name}（{len(script_text)} 字符）")
    print(f"[sn] script_path={script_path.resolve()}")
    print(f"[sn] 音频: {audio_path.name}")
    print(f"[sn] force_recompute_timecodes={bool(args.force_recompute_timecodes)}")
    print(f"[sn] timecode_mode={args.timecode_mode}")

    sn_path = episode_dir / "shownotes.txt"
    before_summary = _shownotes_file_summary(sn_path)
    print("[sn] regen 前 shownotes:")
    print(json.dumps(before_summary, ensure_ascii=False, indent=2))
    if args.force_recompute_timecodes:
        print("[sn] force 模式：禁止读取旧 shownotes 时间戳，只允许读取最新口播稿和真实音频。")

    # 优先使用初次生成时保存的要点，避免重复调用 LLM
    # 例外：若 podcast_script.txt 比缓存更新（Codex 改稿），强制重新提炼
    bullets_cache_path = episode_dir / "shownotes_bullets.json"
    _script_mtime = script_path.stat().st_mtime if script_path.exists() else 0
    _bullets_mtime = bullets_cache_path.stat().st_mtime if bullets_cache_path.exists() else 0
    _bullets_stale = _script_mtime > _bullets_mtime + 5  # 5秒容差
    if bullets_cache_path.exists() and not _bullets_stale:
        _saved_bullets = read_json(bullets_cache_path)
        if _saved_bullets and isinstance(_saved_bullets, list) and len(_saved_bullets) >= 1:
            shownote_bullets = [str(b) for b in _saved_bullets if b]
            print(f"[sn] 使用已保存的要点（{len(shownote_bullets)} 条，跳过 LLM 重新生成）")
        else:
            search_results_path = episode_dir / "search_results.json"
            search_payload = read_json(search_results_path) if search_results_path.exists() else {}
            shownote_bullets = _build_solo_shownote_bullets(search_payload, script_text)
            write_json(bullets_cache_path, shownote_bullets)
    else:
        if _bullets_stale:
            print(f"[sn] 口播稿比要点缓存更新（脚本已被 Codex 改写），强制重新提炼要点")
        search_results_path = episode_dir / "search_results.json"
        search_payload = read_json(search_results_path) if search_results_path.exists() else {}
        shownote_bullets = _build_solo_shownote_bullets(search_payload, script_text)
        write_json(bullets_cache_path, shownote_bullets)

    alignment_candidates = [
        p
        for p in episode_dir.iterdir()
        if p.is_file()
        and re.search(r"(subtitle|alignment|transcript|segments)", p.name, flags=re.I)
    ]
    if alignment_candidates:
        print("[sn] 本地 alignment/transcript 候选:")
        for path in alignment_candidates:
            print(f"[sn] - {path.name}")
    else:
        print("[sn] 未找到本地 transcript/alignment/segments 文件。")

    # 优先使用 SFX 切片时间线（合成 TTS 时由 mix_podcast_from_chunks 生成的精准时间戳）
    chunk_timeline_path = episode_dir / "tts_chunk_timeline.json"
    if chunk_timeline_path.exists() and not args.force_recompute_timecodes:
        saved = read_json(chunk_timeline_path)
        if saved and isinstance(saved, list) and len(saved) >= 1:
            timeline = saved
            print(f"[sn] 使用 SFX chunk 时间线（{len(timeline)} 条精准时间戳，跳过 Whisper/heuristic）")
            print(json.dumps({
                "source": "tts_chunk_timeline.json",
                "timestamp_count": len(timeline),
                "timestamps": [item.get("time", "") for item in timeline],
            }, ensure_ascii=False, indent=2))

            if sn_path.exists():
                bak = episode_dir / "shownotes_before_regen.txt"
                if not bak.exists():
                    import shutil
                    shutil.copy(str(sn_path), str(bak))

            shownotes_text = _render_solo_shownotes(shownote_bullets, timeline)
            write_text(sn_path, shownotes_text)
            print(f"[sn] shownotes 已重建（{len(timeline)} 个时间点）")
            after_summary = _shownotes_file_summary(sn_path)
            print("[sn] regen 后 shownotes:")
            print(json.dumps(after_summary, ensure_ascii=False, indent=2))
            return 0

    transcript_meta: dict[str, object] | None = None
    if args.timecode_mode == "whisper":
        print("[sn] Whisper 转写真实音频并构建时间线...")
        try:
            transcript_meta = _transcribe_audio_with_whisper(audio_path, episode_dir)
        except Exception as exc:
            print(f"[sn] ERROR: {exc}", file=sys.stderr)
            print("[sn] 默认 whisper 模式禁止静默 fallback。需要估算时间戳时请显式传 --timecode-mode heuristic。", file=sys.stderr)
            return 1

        timeline = _build_solo_timeline_from_whisper(
            script_text,
            transcript_meta["segments"],
            audio_duration_seconds=transcript_meta.get("duration_seconds"),
            bullet_labels=shownote_bullets,
        )
        if not any(item.get("matched") for item in timeline):
            print("[sn] ERROR: Whisper 已生成 transcript，但 shownotes item 没有匹配到任何 segment。", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "episode_id": args.episode,
                    "audio_path": str(audio_path.resolve()),
                    "audio_duration_seconds": transcript_meta.get("duration_seconds"),
                    "transcript_path": str(transcript_meta["json_path"]),
                    "transcript_srt_path": str(transcript_meta["srt_path"]),
                    "whisper_engine": transcript_meta.get("engine"),
                    "whisper_model": transcript_meta.get("model"),
                    "transcript_segment_count": len(transcript_meta["segments"]),
                    "old_timestamps": before_summary.get("timecodes"),
                    "new_timestamps_preview": [item.get("time", "") for item in timeline],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("[sn] heuristic 模式：静音检测构建时间线...")
        timeline = _build_solo_timeline_from_audio_silence(
            script_text, audio_path,
            start_offset_ms=voice_start_delay_ms(),
            bullet_labels=shownote_bullets,
        )
        if not timeline:
            print("[sn] 静音检测无结果，使用真实音频时长 + 最新口播稿段落权重 heuristic 重算时间线...")
            audio_duration_seconds = _probe_audio_duration_seconds(audio_path)
            timeline = _build_solo_timeline(
                script_text, audio_duration_seconds=audio_duration_seconds,
                bullet_labels=shownote_bullets,
            )
            print(
                json.dumps(
                    {
                        "script_path": str(script_path.resolve()),
                        "audio_duration_seconds": audio_duration_seconds,
                        "total_script_chars": total_script_chars,
                        "detected_segment_count": len(timeline),
                        "timecode_method": "heuristic_script_char_position",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
    if not timeline:
        print("[sn] ERROR: 未能生成 shownotes 时间线。", file=sys.stderr)
        return 1
    if args.timecode_mode == "heuristic" and not any(item.get("method") == "heuristic_script_char_position" for item in timeline):
        audio_duration_seconds = _probe_audio_duration_seconds(audio_path)
        print(
            json.dumps(
                {
                    "script_path": str(script_path.resolve()),
                    "audio_duration_seconds": audio_duration_seconds,
                    "total_script_chars": total_script_chars,
                    "detected_segment_count": len(timeline),
                    "timecode_method": timeline[0].get("method", "heuristic"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    if sn_path.exists():
        bak = episode_dir / "shownotes_before_regen.txt"
        if not bak.exists():
            import shutil
            shutil.copy(str(sn_path), str(bak))

    shownotes_text = _render_solo_shownotes(shownote_bullets, timeline)
    write_text(sn_path, shownotes_text)
    print(f"[sn] shownotes 已重建（{len(timeline)} 个时间点）")
    after_summary = _shownotes_file_summary(sn_path)
    print("[sn] regen 后 shownotes:")
    print(json.dumps(after_summary, ensure_ascii=False, indent=2))
    if before_summary.get("timecodes") == after_summary.get("timecodes"):
        if args.timecode_mode == "whisper":
            print("[sn] WARNING: 更新时间戳列表完全不变。Whisper 本次匹配结果与旧 shownotes 一致。", file=sys.stderr)
        else:
            print(
                "[sn] WARNING: 更新时间戳列表完全不变。原因：当前无 transcript/alignment，"
                "且 heuristic 输入（真实音频时长 + 最新口播稿段落权重）与上次一致；"
                "这不是精准对齐，只是可复现的 heuristic。",
                file=sys.stderr,
            )
    else:
        print("[sn] 时间戳 diff:")
        print(json.dumps({"old": before_summary.get("timecodes"), "new": after_summary.get("timecodes")}, ensure_ascii=False, indent=2))

    timestamps = [
        int(match.group(1)) * 60 + int(match.group(2))
        for match in re.finditer(r"(?<!\d)(\d{1,3}):([0-5]\d)(?!\d)", shownotes_text)
    ]
    audio_duration_seconds = _probe_audio_duration_seconds(audio_path)
    last_timestamp = timestamps[-1] if timestamps else None
    stat = sn_path.stat()
    summary = {
        "shownotes_path": str(sn_path.resolve()),
        "shownotes_sha256": hashlib.sha256(sn_path.read_bytes()).hexdigest(),
        "shownotes_mtime": dt.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "shownotes_head_300": re.sub(r"\s+", " ", shownotes_text[:300]).strip(),
        "shownotes_tail_300": re.sub(r"\s+", " ", shownotes_text[-300:]).strip(),
        "timestamp_count": len(timestamps),
        "last_timestamp": f"{last_timestamp // 60:02d}:{last_timestamp % 60:02d}" if last_timestamp is not None else "",
        "audio_duration_seconds": audio_duration_seconds,
    }
    print("[sn] 最终 shownotes source-of-truth:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("[sn] 时间线分配明细:")
    for index, item in enumerate(timeline, start=1):
        print(
            json.dumps(
                {
                    "index": index,
                    "item_text": item.get("label", ""),
                    "time": item.get("time", ""),
                    "method": item.get("method", "unknown"),
                    "shownotes_label": item.get("label", ""),
                    "matched_transcript_text": item.get("matched_text", ""),
                    "matched_segment_start": item.get("matched_start", ""),
                    "matched_segment_end": item.get("matched_end", ""),
                    "confidence": item.get("match_score", ""),
                    "match_score": item.get("match_score", ""),
                    "match_source": item.get("match_source", ""),
                    "script_excerpt": item.get("script_excerpt", ""),
                    "start_char": item.get("start_char", ""),
                },
                ensure_ascii=False,
            )
        )
        if item.get("warning"):
            print(f"[sn] WARNING: 第 {index} 条未匹配到 Whisper segment，已插值: {item.get('label', '')}", file=sys.stderr)
    if audio_duration_seconds and last_timestamp is not None and last_timestamp < audio_duration_seconds * 0.6:
        print(
            f"[sn] WARNING: 最后时间戳明显早于真实音频 last={last_timestamp}s audio={audio_duration_seconds}s",
            file=sys.stderr,
        )
    return 0


def notify_final_script_mode(args: argparse.Namespace) -> int:
    """推送 codex 终稿全文到飞书。带 hash 去重：codex 收尾调用 + launchd 兜底不会重复推送。"""
    import hashlib

    episode = args.episode or f"{now_local().strftime('%Y%m%d')}E1"
    episode_dir = EPISODES_DIR / episode
    script_path = episode_dir / "podcast_script.txt"
    if not script_path.exists():
        print(f"[notify] 口播稿不存在: {script_path}", file=sys.stderr)
        return 1
    script_text = script_path.read_text(encoding="utf-8").strip()
    title_path = episode_dir / "title.txt"
    title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
    sha = hashlib.sha256(script_text.encode("utf-8")).hexdigest()

    state_path = episode_dir / "script_notify_state.json"
    state = read_json(state_path) if state_path.exists() else {}

    if state.get("final_notified_sha") == sha and not args.force:
        print("[notify] 终稿全文此前已推送（hash 相同），跳过。--force 可强制重发。")
        return 0

    if state.get("draft_sha256") == sha:
        # codex 还没改写过稿件：推告警，不把初稿冒充终稿
        _notify_feishu_rich(
            "AI观测站 · 终稿未就绪告警",
            [
                {"label": "期号", "text": episode},
                {"label": "情况", "text": "口播稿与凌晨初稿完全相同，codex 终稿改写可能未执行或失败。"},
                {"label": "排查", "text": "查看 codex automation solo-6 运行记录。初稿全文此前已推送，可人工判断是否直接用初稿合成。"},
            ],
        )
        print("[notify] 稿件与初稿相同，已推送终稿未就绪告警。", file=sys.stderr)
        return 0

    cover_path = episode_dir / "cover.jpeg"
    cover_state = (
        "已生成（codex 定制封面完成后会单独推图）" if cover_path.exists() else "尚未生成，等 codex 封面定制或后台重试"
    )
    _notify_feishu_rich(
        "AI观测站 · codex 终稿已就绪",
        [
            {"label": "期号", "text": episode},
            {"label": "标题", "text": title or "(title.txt 缺失)"},
            {"label": "字数", "text": f"{len(script_text)} 字"},
            {"label": "封面", "text": cover_state},
            {"label": "下一步", "text": f"审完全文后回复 合成TTS {episode}"},
        ],
    )
    try:
        from mobile_notify import notify_feishu_long_text

        notify_feishu_long_text(f"AI观测站 · {episode} 口播稿全文（codex 终稿）", script_text)
    except Exception as exc:
        print(f"[notify] 终稿全文推送失败: {exc}", file=sys.stderr)
        return 1
    state["final_notified_sha"] = sha
    state["final_notified_at"] = now_local().isoformat()
    write_json(state_path, state)
    print("[notify] 终稿全文已推送飞书。")
    return 0


def notify_cover_ready_mode(args: argparse.Namespace) -> int:
    """codex 封面定制完成后推送封面图：标志单口素材全部就绪，可在手机上开始三步发布。"""
    episode = args.episode or f"{now_local().strftime('%Y%m%d')}E1"
    episode_dir = EPISODES_DIR / episode
    cover_path = episode_dir / "cover.jpeg"
    if not cover_path.exists():
        print(f"[notify] 封面不存在: {cover_path}", file=sys.stderr)
        return 1
    title_path = episode_dir / "title.txt"
    title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""

    _notify_feishu_rich(
        "AI观测站 · 封面已就绪，单口素材全齐",
        [
            {"label": "期号", "text": episode},
            {"label": "标题", "text": title or "(title.txt 缺失)"},
            {"label": "封面", "text": "codex 定制封面见下图"},
            {"label": "下一步", "text": f"确认后回复 合成TTS {episode}，TTS 完成再回复 确认发布小宇宙 {episode}"},
        ],
    )
    try:
        from mobile_notify import notify_feishu_image

        notify_feishu_image(str(cover_path), f"AI观测站 · {episode} 封面")
    except Exception as exc:
        print(f"[notify] 封面图片推送失败: {exc}", file=sys.stderr)
    print("[notify] 封面就绪通知已推送。")
    return 0


def _push_shownotes_full_text(episode_dir: Path, episode: str) -> None:
    """shownotes 全文分段推送（手机审稿不接受截断预览）。"""
    shownotes_path = episode_dir / "shownotes.txt"
    if not shownotes_path.exists():
        print(f"[notify] shownotes 全文推送跳过：{shownotes_path} 不存在", file=sys.stderr)
        return
    text = shownotes_path.read_text(encoding="utf-8").strip()
    if not text:
        print(f"[notify] shownotes 全文推送跳过：文件为空", file=sys.stderr)
        return
    try:
        from mobile_notify import notify_feishu_long_text

        ok = notify_feishu_long_text(f"AI观测站 · {episode} shownotes 全文", text)
        if ok:
            print(f"[notify] shownotes 全文推送成功（{len(text)} 字符）")
        else:
            print(f"[notify] shownotes 全文推送失败（飞书返回非成功或频率限制）", file=sys.stderr)
    except Exception as exc:
        print(f"[notify] shownotes 全文推送异常: {exc}", file=sys.stderr)


def _notify_synthesize_result(episode: str, exit_code: int) -> None:
    """synthesize-tts 的成功/失败回执：飞书链路三步中的第一步必须有结果信号。"""
    episode_dir = EPISODES_DIR / episode
    try:
        if exit_code == 0:
            title_path = episode_dir / "title.txt"
            title = title_path.read_text(encoding="utf-8").strip() if title_path.exists() else ""
            duration_txt = ""
            audio_path = episode_dir / "podcast_solo.mp3"
            if audio_path.exists():
                seconds = _probe_audio_duration_seconds(audio_path)
                if seconds:
                    duration_txt = f"{seconds // 60} 分 {seconds % 60} 秒"
            _notify_feishu_rich(
                "AI观测站 · TTS 已完成，shownotes 已更新",
                [
                    {"label": "期号", "text": episode},
                    {"label": "标题", "text": title or "(title.txt 缺失)"},
                    {"label": "时长", "text": duration_txt or "未知"},
                    {"label": "下一步", "text": f"看完下方 shownotes 全文后回复 确认发布小宇宙 {episode}"},
                ],
            )
            time.sleep(1.5)
            _push_shownotes_full_text(episode_dir, episode)
        else:
            _notify_feishu_rich(
                "AI观测站 · TTS 合成失败",
                [
                    {"label": "期号", "text": episode},
                    {"label": "排查", "text": f"查看 logs/synthesize_tts_{episode}.log"},
                ],
            )
    except Exception as exc:
        print(f"[tts] 回执推送失败: {exc}", file=sys.stderr)


def _notify_regen_shownotes_result(episode: str, exit_code: int) -> None:
    """regen-shownotes 的成功/失败回执：手机端飞书触发后需要知道结果。"""
    episode_dir = EPISODES_DIR / episode
    try:
        if exit_code == 0:
            _notify_feishu_rich(
                "AI观测站 · Shownotes 已更新",
                [
                    {"label": "期号", "text": episode},
                    {"label": "下一步", "text": f"看完下方 shownotes 全文后可回复 确认发布小宇宙 {episode}"},
                ],
            )
            time.sleep(1.5)
            _push_shownotes_full_text(episode_dir, episode)
        else:
            _notify_feishu_rich(
                "AI观测站 · Shownotes 更新失败",
                [
                    {"label": "期号", "text": episode},
                    {"label": "排查", "text": f"查看 logs/regen_shownotes_{episode}.log"},
                ],
            )
    except Exception as exc:
        print(f"[sn] 回执推送失败: {exc}", file=sys.stderr)


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="单人口播流水线")
    subparsers = parser.add_subparsers(dest="command")

    build_parser = subparsers.add_parser("build-episode", help="生成一期单人口播")
    build_parser.add_argument("--episode", help="指定期数，如 20260330E2")
    build_parser.add_argument("--search-file", help="使用已有的搜索结果 JSON")
    build_parser.add_argument("--voice-id", help="MiniMax 声音 ID（默认普通话新闻女主播）")
    build_parser.add_argument(
        "--cover-provider",
        choices=["gemini", "codex", "none"],
        default="gemini",
        help="封面生成方式",
    )
    build_parser.add_argument("--draft-only", action="store_true", help="只生成标题、脚本和封面，暂不合成 TTS")
    build_parser.add_argument("--publish", action="store_true", help="生成后自动发布小宇宙")
    build_parser.add_argument("--force", action="store_true", help="忽略调度配置，强制执行")

    retry_cover_parser = subparsers.add_parser("retry-cover", help="延迟重试单集封面")
    retry_cover_parser.add_argument("--episode", required=True, help="指定期数，如 20260405E1")
    retry_cover_parser.add_argument("--provider", choices=["gemini", "codex"], default="gemini")
    retry_cover_parser.add_argument("--delay-seconds", type=int, default=0, help="延迟后执行重试")
    retry_cover_parser.add_argument("--publish-after-retry", action="store_true", help="封面补齐后自动继续发布小宇宙")

    tts_parser = subparsers.add_parser("synthesize-tts", help="从 Codex 稿合成 TTS 并重建 shownotes")
    tts_parser.add_argument("--episode", required=True, help="指定期数，如 20260502E1")
    tts_parser.add_argument("--voice-id", help="MiniMax 声音 ID（默认普通话新闻女主播）")

    cover_parser = subparsers.add_parser("gen-cover-title", help="生成封面和标题（不重新合成 TTS）")
    cover_parser.add_argument("--episode", required=True, help="指定期数，如 20260502E1")
    cover_parser.add_argument("--force", action="store_true", help="即使已有标题/封面也强制重新生成")
    cover_parser.add_argument("--provider", choices=["gemini", "codex", "none"], default="gemini", help="封面生成方式")

    title_parser = subparsers.add_parser("regen-title-after-cover", help="封面完成后基于最终口播稿刷新标题")
    title_parser.add_argument("--episode", required=True, help="指定期数，如 20260502E1")
    title_parser.add_argument("--script-path", help="显式指定最终口播稿路径")

    nfs_parser = subparsers.add_parser("notify-final-script", help="推送 codex 终稿全文到飞书（带去重）")
    nfs_parser.add_argument("--episode", help="期号，默认当天 E1")
    nfs_parser.add_argument("--force", action="store_true", help="忽略去重强制重发")

    ncr_parser = subparsers.add_parser("notify-cover-ready", help="推送 codex 封面图到飞书（素材全就绪信号）")
    ncr_parser.add_argument("--episode", help="期号，默认当天 E1")

    sn_parser = subparsers.add_parser("regen-shownotes", help="从已有音频和口播稿重建 shownotes")
    sn_parser.add_argument("--episode", required=True, help="指定期数，如 20260502E1")
    sn_parser.add_argument(
        "--force-recompute-timecodes",
        action="store_true",
        help="强制基于最新口播稿和真实音频重算时间戳，禁止沿用旧 shownotes 时间戳",
    )
    sn_parser.add_argument(
        "--timecode-mode",
        choices=["whisper", "heuristic"],
        default="whisper",
        help="时间戳生成方式：默认 whisper 真实音频转写；heuristic 仅显式 fallback 使用",
    )

    args = parser.parse_args()
    if args.command == "build-episode":
        return build_solo_episode(args)
    elif args.command == "retry-cover":
        return retry_cover_mode(args)
    elif args.command == "synthesize-tts":
        try:
            code = synthesize_tts_mode(args)
        except Exception:
            _notify_synthesize_result(args.episode, 1)
            raise
        _notify_synthesize_result(args.episode, code)
        return code
    elif args.command == "gen-cover-title":
        return gen_cover_title_mode(args)
    elif args.command == "regen-title-after-cover":
        return regen_title_after_cover_mode(args)
    elif args.command == "regen-shownotes":
        code = regen_shownotes_mode(args)
        _notify_regen_shownotes_result(args.episode, code)
        return code
    elif args.command == "notify-final-script":
        return notify_final_script_mode(args)
    elif args.command == "notify-cover-ready":
        return notify_cover_ready_mode(args)
    else:
        parser.print_help()
        return 1


__all__ = [name for name in globals() if not name.startswith("__")]
