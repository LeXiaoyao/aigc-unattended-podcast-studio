# 📝 开源提示词库与 Codex 自动化任务（Prompts & Codex Automations）

在真实生产环境中，**单人口播日报（Solo E1）的出稿主力与封面主力均为 Codex 定时自动化任务（Codex Automations）**，而非传统的 Python Qwen/DeepSeek 初稿拼接链路。

## 为什么采用「Codex 从 `search_results.json` 直出终稿」架构？

早期使用 Python 脚本串联 Qwen/DeepSeek 生成的初稿容易出现「AI 腔重、句式机械重复、跨期撞题、口径边界模糊」等问题。因此生产架构演进为 **「Python 负责确定性新闻搜集与音频工程，Codex Agent 负责总编级选品、直出终稿与社论海报创作」**：

1. **03:30 候选新闻落盘（`run_solo_podcast_daily.sh`）**：运行 `solo_pipeline.py build-episode --draft-only --cover-provider none`，经过 8 大信源抓取与 6 层漏斗清洗，产出结构化候选池 `episodes/YYYYMMDDE1/search_results.json`。
2. **04:30 Codex 从 JSON 直出终稿（[`automations/solo_6_codex_script.toml`](../automations/solo_6_codex_script.toml) + [`solo_daily_script_prompt.md`](./solo_daily_script_prompt.md)）**：
   - **完全绕过/覆盖** Python 初稿与 `research.md`，由 Codex Agent 直接读取 `search_results.json` 与近 7 期 `episodes/*/podcast_script.txt`。
   - 严格执行 **P0–P3 选品、近 7 期同主体×同事件去重、5 大开场范式轮换、四步口播法（事实→口径边界→判断→影响）、钩子句、冲突动词标题**，一步直出：
     - `episodes/YYYYMMDDE1/podcast_script.txt`（终稿）
     - `episodes/YYYYMMDDE1/title.txt`（单行强动词标题）
     - `episodes/YYYYMMDDE1/script_quality_report.md`（21 项硬性自检报告）
   - 写完通过自检后执行 `python3 solo_pipeline.py notify-final-script --episode YYYYMMDDE1` 推送飞书手机审稿。
3. **05:00 Codex 社论海报封面定制（[`automations/codex_shadow_mode_cover.toml`](../automations/codex_shadow_mode_cover.toml) + [`solo_editorial_cover_prompt.md`](./solo_editorial_cover_prompt.md)）**：
   - 读取刚出炉的 Codex 终稿 `podcast_script.txt`，提取当日总主题、中心问题、3–6 个冲突阵营公司名与 4–8 个中文关键词，直接生成 1:1 讽刺社论杂志风「图文一体」封面 `cover.jpeg`（无需本地二次叠字），并执行 `notify-cover-ready` 推送飞书。
4. **05:15 自动合成 TTS 与发布（`webapp/auto_publish.py` → `solo_pipeline.py synthesize-tts`）**：
   - 编排器检测到 Codex 生成的 `script_quality_report.md` 通过且 `cover.jpeg` 就绪后，自动触发 MiniMax `speech-02-hd` 分段 TTS 合成、`audio_mixer.py` 广播级混音（`-14 LUFS`）、真实音频时间轴对齐生成 Shownotes，并一键发布小宇宙 & 更新标准 Podcast RSS 2.0。

---

## 目录清单

| 文件 | 对应定时任务 | 用途说明 |
| :--- | :--- | :--- |
| [`solo_daily_script_prompt.md`](./solo_daily_script_prompt.md) | 每日 `04:30` (`solo-6`) | **单口主力出稿 Prompt**：从 `search_results.json` 直出口播终稿、标题与 21 项质检报告 |
| [`solo_editorial_cover_prompt.md`](./solo_editorial_cover_prompt.md) | 每日 `05:00` (`codex-shadow-mode`) | **单口主力封面 Prompt**：基于 Codex 终稿直出 1:1 讽刺社论杂志风「图文一体」封面 `cover.jpeg` |
| [`query_zero_sum_maintenance_prompt.md`](./query_zero_sum_maintenance_prompt.md) | 每周一 `09:00` (`ai-2`) | **检索词零和维护 Prompt**：每周动态纳新 AI 新势力、淘汰过时词条，锁死 Tavily/Brave API 配额不膨胀 |
| [`../automations/*.toml`](../automations/) | Codex Automation 配置 | 可直接导入 `~/.codex/automations/` 的 3 个生产级定时任务 TOML 模板 |
