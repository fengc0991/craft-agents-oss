# PaiWork 查询与 Case 排查工作流

## 适用范围

`paiobs_task_query.py` 只用于检索明细、抽样、定位具体 query/session/task、关键词命中和补证据，不用于统计总量或分布。凡是数量、占比、成功率、TopN、趋势或多维拆分，切到 `references/stats_workflows.md`。

历史问句过滤维度对齐 WebUI：`session_id`、`question_id`、`user_id`、`username`、`institution`、`institution_nature`、`inst_type`、`product_type`、`user_type`、`user_role`、`keyword`、`answer_keyword`、`entry_scene`、`status`、`scheduled`、`end_type`、`is_web_search`、`start_time`、`end_time`。

## 常规检索

```bash
python3.11 scripts/paiobs_task_query.py search \
  --username "凤超" \
  --institution "证券" \
  --answer-keyword "无法访问" \
  --status failed \
  --start-time "2026-05-01 00:00:00" \
  --end-time "2026-05-14 23:59:59" \
  --limit 100 \
  --format table \
  --output /tmp/paiobs_search.csv
```

多个关键词按 OR 查询并去重：

```bash
python3.11 scripts/paiobs_task_query.py search \
  --keywords "格式不对,没有按照,数据不对" \
  --start-time "2026-05-01 00:00:00" \
  --end-time "2026-05-14 23:59:59" \
  --limit-per-keyword 50 \
  --format table
```

如果 table 里的 `question` 仍是服务端 `question_preview` 而非完整原问，不要整页切 JSON；少量定点用 `session`、`trace` 或 `task --profile summary` 补证据，成批补完整问答用 `batch --profile qa`。

## 关键词 Buckets 和 Case Mining

内置 buckets：

```bash
python3.11 scripts/paiobs_task_query.py keyword-buckets --format table
python3.11 scripts/paiobs_task_query.py keyword-search complaint --via search --start-time "2026-05-01 00:00:00" --format table
python3.11 scripts/paiobs_task_query.py keyword-search inability --via mine --limit 100 --format table
```

当前 buckets 包括 `satisfaction`、`complaint`、`pointed_error`、`inability`、`continue`、`new_requirement`。`complaint` 覆盖常见中英文抱怨、追责、脏话/辱骂词和质量差表达，并维护行业语境排除词以降低误伤。`--via search` 走历史问句关键词；`--via mine` 走 `/mining/cases`，适合无能力、投诉等需要 answer/evidence 的线索。

需要交付给业务方或继续人工分析时，优先加 `--detail-csv` 输出统一明细 CSV。字段对齐“用户抱怨问题”表格，包括时间窗口、分类、用户/机构、状态、严重度、关键词、session/task/question 和原文。CSV 使用 UTF-8 with BOM 写入，方便 Excel 直接打开。

```bash
python3.11 scripts/paiobs_task_query.py keyword-search complaint \
  --via mine \
  --start-time "2026-05-16 00:00:00" \
  --end-time "2026-05-18 23:59:59" \
  --limit 100 \
  --detail-csv \
  --output /tmp/用户抱怨问题_20260516_20260518.csv
```

`search --keywords ...` 和通用 `mine <kind>` 也支持 `--detail-csv`。

## 单任务和 Session 深挖

按深度递进：

```bash
python3.11 scripts/paiobs_task_query.py session <session_id> --format table
python3.11 scripts/paiobs_task_query.py trace <session_id> <task_index> --format table
python3.11 scripts/paiobs_task_query.py task <session_id> <task_index> --profile summary --output summary.json
python3.11 scripts/paiobs_task_query.py inspect --context-json summary.json --format json
python3.11 scripts/paiobs_task_query.py context <session_id> <task_index> --output context.json
python3.11 scripts/paiobs_task_query.py inspect --context-json context.json --format table
python3.11 scripts/paiobs_task_query.py files --context-json context.json --format table
python3.11 scripts/paiobs_task_query.py skills --context-json context.json --format table
```

排查非首轮任务时，先看同一 session 前序 queries，再按 task_index 递增追溯上下文。任务执行步骤、输入输出文件、数据源、工具/skill 记录通常在 `summary/context/bundle` 中；`summary.evidence.tool_inputs` / `summary.evidence.subagent_instructions` 是查委托子 Agent 入参、SQL 和 ES DSL 的首选位置，`inspect` 会把这些字段汇总到 `tool_inputs`。只有这些包解释不了时，再考虑 `full` 或 raw log。

## 批量任务信息

成批获取任务 summary、完整问答或 context 时，优先使用网关 batch 接口，避免本地循环打单任务接口：

```bash
python3.11 scripts/paiobs_task_query.py batch \
  <session_id_1>:<task_index_1> \
  <session_id_2>:<task_index_2> \
  --profile qa \
  --output /tmp/paiobs_batch_qa.json
```

也可以直接用检索条件让网关先召回 refs，再并发补详情：

```bash
python3.11 scripts/paiobs_task_query.py batch \
  --start-time "2026-05-19 15:00:00" \
  --end-time "2026-05-19 16:00:00" \
  --status failed \
  --profile summary \
  --max-items 100 \
  --include-search-item \
  --output /tmp/paiobs_failed_summary.json
```

`summary` 和 `qa` 最多 100 条；`context` 和 `full` 最多 20 条。旧命令 `batch-context` 仍可用，默认 `profile=context`，但新批量读取优先用 `batch`。

## 主题扫描分流

如果问题不是纯 keyword search 能解决，而是要先抽结构化主题，例如“最近 1 天用户提到哪些 X/Twitter 账号”“哪些 query 明确点名了外部数据源或竞品”“用户提到哪些命名行业站/资讯平台”“用户常要求输出哪些文件格式”，使用 `scripts/paiobs_query_theme_scan.py`，细节见 `references/query_theme_scan.md`。其中数据意向/数据源需求用 `data-intent`，默认走网关 `theme-search` 先按 include/exclude 规则在 SQL 层返回候选，再本地抽取命名数据源并轻量总结；报告会输出点名数据源/网站 TopN、辅助数据意向分类、数据主题/指标和用户需求总结。候选数据源/网站需后续 agent 继续分析筛选。
