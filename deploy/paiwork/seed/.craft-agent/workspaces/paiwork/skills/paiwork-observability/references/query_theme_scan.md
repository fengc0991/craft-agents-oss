# Query Theme Scan

`paiobs_query_theme_scan.py` 用于一类介于 search 和 AI 批量分析之间的问题：

- 不能只靠网关 `keyword` 搜索，因为主题可能由多种写法表达，或者要从原问里抽结构化值。
- 不值得上 LLM 批量评分，因为目标是可审计的主题检索、计数、Top 值、样例 ref。
- 需要先把某个时间段内的 query 文本尽量完整取回，再由 Agent 或本地规则做指定主题扫描。

典型例子：最近 1 天用户提到哪些 X/Twitter 账号、哪些命名数据源/行业站、哪些明确数据意向、哪些邮箱域名、哪些竞品、哪些自动化诉求、哪些报告交付格式。

## 推荐链路

1. 先用 `task-stats` 或 `overview` 确认窗口规模。
2. 用 `collect` 按时间片全量拉 `summary` query 语料。网关 search 单次返回可能有上限，脚本会在某个时间片命中 `page_limit` 时继续二分拆片，直到低于上限或到秒级下限。
3. 用内置扫描器或 `scan-regex` 在本地抽取目标主题。
4. 对 Top 值或可疑样例，用 `session`、`trace`、`task --profile summary/context` 定点补证据。

这个链路保持只读，不直连 SelectDB/SLS，不输出 API key。

## Collect

最近 24 小时拉成 JSONL：

```bash
# 先进入当前 skill 根目录：包含 SKILL.md、scripts/、references/ 的目录
python3.11 scripts/paiobs_query_theme_scan.py collect \
  --hours 24 \
  --profile summary \
  --initial-window-minutes 5 \
  --page-limit 100 \
  --max-workers 8 \
  --format jsonl \
  --output /tmp/paiobs_query_corpus.jsonl
```

指定绝对时间：

```bash
python3.11 scripts/paiobs_query_theme_scan.py collect \
  --start-time "2026-05-14 17:09:35" \
  --end-time "2026-05-15 17:09:35" \
  --format jsonl \
  --output /tmp/paiobs_query_corpus.jsonl
```

输出里的每条 item 会补一个 `query_text` 字段，默认按以下字段去重拼接：

`latest_question, first_question, question, question_text, question_preview, match_preview`

如果只想用完整原问，减少 preview 截断干扰：

```bash
python3.11 scripts/paiobs_query_theme_scan.py collect \
  --text-fields latest_question,first_question \
  --format jsonl \
  --output /tmp/paiobs_query_corpus.jsonl
```

## X/Twitter 账号扫描

一条命令完成拉取和扫描：

```bash
python3.11 scripts/paiobs_query_theme_scan.py x-accounts \
  --start-time "2026-05-14 17:09:35" \
  --end-time "2026-05-15 17:09:35" \
  --format table \
  --output /tmp/paiobs_x_accounts.csv
```

复用已有 corpus：

```bash
python3.11 scripts/paiobs_query_theme_scan.py x-accounts \
  --input /tmp/paiobs_query_corpus.jsonl \
  --format markdown \
  --top-n 50
```

默认只统计包含 X/Twitter 语境或 `x.com/twitter.com` 链接的 query，并过滤 `@_user_1` 这类平台 mention 占位。需要扫描所有 `@handle` 时：

```bash
python3.11 scripts/paiobs_query_theme_scan.py x-accounts \
  --input /tmp/paiobs_query_corpus.jsonl \
  --no-require-x-context
```

## 数据意向扫描

`data-intent` 用于抓出用户原文里明确点名外部数据源、数据库、行业站、资讯平台或 URL 的 query，并在 Markdown 报告里总结这些命名数据源需求。它会统计：

- 通过“来源/接入/抓取/采集/访问/查询 + 命名站点形态”抽取到的点名数据源、垂类网站、数据库、资讯平台或 URL TopN。
- 对已命中命名来源的 query，辅助统计数据意向分类，例如价格行情、产业供需/产销存、公司财务/公告、医药研发/审批、煤炭、化工/大宗商品、宏观官方统计等。
- 对已命中命名来源的 query，辅助统计数据主题/指标 TopN，例如价格、库存、产量、进出口、财报、药品临床等。
- 一段“用户数据需求总结”，用于直接放到主题统计报告或复盘结论中。

默认链路是 gateway-side `theme-search`：把 include/exclude 规则传给网关 SQL 层，只返回疑似明确点名来源的候选 query，再在本地抽取来源名称、归类和 Markdown 汇总。不要默认走 `collect` 全量语料；只有需要调试召回边界时才加 `--local-collect`。

一条命令完成拉取和报告：

```bash
python3.11 scripts/paiobs_query_theme_scan.py data-intent \
  --start-time "2026-05-16 00:00:00" \
  --end-time "2026-05-18 23:59:59" \
  --format markdown \
  --max-items 500 \
  --top-n 30 \
  --output /tmp/paiobs_data_intent_report.md
```

复用已有 corpus：

```bash
python3.11 scripts/paiobs_query_theme_scan.py data-intent \
  --input /tmp/paiobs_query_corpus.jsonl \
  --format markdown \
  --top-n 50
```

默认不内置任何具体数据源名称，避免把产品判断写死在规则里。脚本只按“命名来源形态 + 来源语境”抽取候选，例如品牌/机构名后跟资讯、信息网、数据库、数据平台、终端、统计局、交易所、化工、魔方等泛化后缀；后续再由 agent 或人工筛选确认。确实要临时强制关注某个名称时，可显式重复传 `--source`：

```bash
python3.11 scripts/paiobs_query_theme_scan.py data-intent \
  --input /tmp/paiobs_query_corpus.jsonl \
  --source "<候选数据源名称>" \
  --format markdown
```

需要交付明细 query 时，加 `--detail-csv`：

```bash
python3.11 scripts/paiobs_query_theme_scan.py data-intent \
  --start-time "2026-05-16 00:00:00" \
  --end-time "2026-05-18 23:59:59" \
  --detail-csv \
  --output /tmp/数据意向问题_20260516_20260518.csv
```

口径说明：

- 命中条件是用户原问里明确点名某个外部来源、行业站、资讯平台、数据库或 URL；泛泛的“要价格/指标/财报/研报数据”不再单独计入。
- 该统计是 query 原文主题扫描，不读取 answer、工具入参或最终文件；只反映用户显式点名的数据来源需求。
- `matched_task_count` 是明确点名来源的 query 数；`source_matched_task_count` 是其中抽取到候选数据源/网站/平台/URL 名称的 query 数。
- 候选数据源/网站不是最终判定，报告中保留 `sample_ref` 供后续 agent 继续分析筛选。
- `coverage.mode=gateway_theme_search` 表示走网关候选召回；`coverage.elapsed_seconds`/`gateway_elapsed_seconds` 可用来观察耗时。

## 通用 Regex 扫描

抽取用户要求的邮箱域名：

```bash
python3.11 scripts/paiobs_query_theme_scan.py scan-regex \
  --input /tmp/paiobs_query_corpus.jsonl \
  --theme email_domains \
  --pattern '@([A-Za-z0-9.-]+\.[A-Za-z]{2,})' \
  --normalize lower \
  --format table
```

抽取常见文件交付格式：

```bash
python3.11 scripts/paiobs_query_theme_scan.py scan-regex \
  --input /tmp/paiobs_query_corpus.jsonl \
  --theme deliverable_format \
  --pattern '\b(markdown|md|excel|xlsx|csv|pptx|pdf|word|docx)\b' \
  --normalize lower \
  --min-task-count 2 \
  --format markdown
```

只在带特定语境的 query 里扫描，例如“监控/日报/推送”场景下出现的账号：

```bash
python3.11 scripts/paiobs_query_theme_scan.py scan-regex \
  --input /tmp/paiobs_query_corpus.jsonl \
  --theme monitor_handles \
  --context-pattern '监控|日报|推送|跟踪' \
  --pattern '@([A-Za-z0-9_]{1,15})' \
  --format table
```

`scan-regex` 输出字段：

| 字段 | 含义 |
| --- | --- |
| `value` | 抽取到的值 |
| `task_count` | 出现该值的去重 query 数 |
| `mention_count` | 文本中出现次数 |
| `first_seen` / `last_seen` | 时间窗口内首次/末次出现时间 |
| `source_types` | 来源类型计数，如 `regex:3`、`@:5;url:2` |
| `sample_ref` | 可用于继续 `trace/session/task` 的样例 ref |

需要交付相关 query 明细时，加 `--detail-csv`，会输出与 keyword bucket 明细一致的 CSV 字段，并把命中的正则值写入 `matched_keywords`：

```bash
python3.11 scripts/paiobs_query_theme_scan.py scan-regex \
  --start-time "2026-05-16 00:00:00" \
  --end-time "2026-05-18 23:59:59" \
  --theme user_complaint \
  --pattern '不满意|没有按照|还是错|太差|wtf|fuck|bullshit' \
  --detail-csv \
  --output /tmp/用户抱怨问题_20260516_20260518.csv
```

## Agent 使用建议

- 高召回优先：先 `collect` 全量语料，再在本地反复试规则，避免多次打 search API 丢长尾。
- 审计优先：报告里优先给 `sample_ref` 和计数，不贴大量用户原文；需要证据时再定点拉单条 summary/context。
- 规则先窄后宽：先加 `--context-pattern` 限定语境，再放开长尾；避免把邮箱、内部 mention、模板占位误当目标实体。
- 遇到高峰时间段：观察 `coverage.capped_windows_count`。如果非 0，缩小 `--min-slice-seconds` 或时间窗口后重跑。
- 扫描结果不能直接等同用户真实意图：它是候选集合。产品判断或工程归因前，应抽样查看 `trace` 或 `task --profile summary`。
