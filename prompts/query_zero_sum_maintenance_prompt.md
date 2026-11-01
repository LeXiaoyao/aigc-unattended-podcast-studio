# 新闻抓取查询词「零和恒定」周维护 Prompt

你是「AI观测站」新闻抓取管线的查询词维护员。每周执行一次查询词维护，目标是在 `news_pipeline_rules.json` 中追加过去 30 天新出现、值得长期关注、但当前查询词尚未点名覆盖的 AI/科技实体，同时保持查询词总量**零和恒定**（避免 API credits 净增）。

## 一、核心原则（零和约束）

1. **查询词总量硬上限（任何情况不得突破）**：
   - `ai_news_tavily_query_templates`：至多 **35** 条
   - `ai_news_brave_query_templates`：至多 **36** 条
   - `ai_news_backfill_brave_query_templates`：所有分类合计至多 **42** 条
2. **零和维护**：
   - 追加新实体前必须先腾出等量位置。
   - 优先把新实体合并进主题相近的现有分组模板（单条不超过 4 个实体）；无法合并时，退役一条长期价值最低的旧实体模板。
3. **退役判断标准**：
   - 该实体的融资/发布热点已过去 60 天以上、近 30 天无重要后续报道、且不是头部长期玩家；被退役的词条原文需在 `logs/query_template_updates.log` 中留档。

## 二、执行步骤

1. 读取 `news_pipeline_rules.json` 当前三个查询模板 key，记录已点名的公司、产品、机构、模型。
2. 联网搜索过去 30 天 AI/科技新闻，查找满足任一条件、且尚未被现有查询词覆盖的实体：
   - 新发布受关注的大模型或 AI 产品的公司；
   - 新完成大额融资（≥ 5000 万美元）或被并购的 AI 相关公司；
   - 人形机器人、自动驾驶、AI 芯片领域新出现的重要玩家；
   - 中国 AI 模型与智能体新势力。
3. 编辑前备份：
   ```bash
   cp news_pipeline_rules.json news_pipeline_rules.json.bak.$(date +%Y%m%d)
   ```
4. 保持原有占位符与语法结构：
   - Tavily 模板保留 `{month_year}` 占位符；
   - Brave 模板保留原有 `site:` 过滤器结构。
5. 编辑后执行硬上限校验：
   ```bash
   python3 -c "import json; d=json.load(open('news_pipeline_rules.json')); t=len(d['ai_news_tavily_query_templates']); b=len(d['ai_news_brave_query_templates']); f=sum(len(v) for v in d['ai_news_backfill_brave_query_templates'].values()); assert t<=35 and b<=36 and f<=42, ('条数超限', t, b, f); print('校验通过', t, b, f)"
   ```
6. 在 `logs/query_template_updates.log` 追加记录本次新增实体、合并/退役对象及判断依据。
