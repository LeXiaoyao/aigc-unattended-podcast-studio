# competitor_radar

独立竞品选题雷达模块，用来读取竞品播客最新主题，并与本地 episode 产物做离线对比。

## 做什么

- 抓取 Vivi Podcast、Sheng FM / Espresso、小宇宙竞品账号的最新节目。
- 把竞品标题、摘要、shownotes 归一成结构化字段。
- 读取本地 `episodes/{EPISODE_ID}` 的 `search_results.json`、`research.json`、`research.md`、`podcast_script.txt`、`shownotes.txt`。
- 对比是否已播、近期已播、新闻源有但被过滤、新闻源没有、低价值不跟进。
- 生成 Markdown 和 JSON 报告。
- 可在显式允许时，把关键词和来源建议写入 `config/suggested_news_keywords.yaml`，状态为 `pending_review`。

## 不做什么

- 不改当天口播稿。
- 不调用 TTS。
- 不发布到小宇宙。
- 不修改主新闻抓取配置。
- 不把我们自己的小宇宙账号当竞品。

## 手动运行

```bash
python3 tools/competitor_radar/competitor_radar.py --episode-id 20260429E2 --write-report
```

报告输出到：

```text
tools/competitor_radar/reports/{EPISODE_ID}_competitor_radar.md
tools/competitor_radar/reports/{EPISODE_ID}_competitor_radar.json
```

