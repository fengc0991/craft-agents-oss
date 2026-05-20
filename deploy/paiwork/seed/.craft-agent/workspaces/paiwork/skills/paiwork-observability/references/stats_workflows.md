# PaiWork 聚合统计工作流

## 默认决策

统计类功能统一走网关聚合/统计接口，不用 search 明细接口算数量或分布，也不做本地抓取。用户问“过去 1h/今天/最近 N 天任务，做个简单/快速/聚合统计”时，默认直接使用 `scripts/paiobs_quick_stats_report.py`。

`paiobs_quick_stats_report.py` 是聚合统计报告默认入口：优先一次调用 Gateway `/api/internal/v1/reports/aggregate`，由网关一次性返回基础聚合、detail `task-stats`、Token 总计和覆盖信息；旧网关才回退到并行请求基础聚合和 detail `task-stats`。脚本直接生成固定 11 章报告，避免手工跑 overview、detail JSON、Markdown 和本地拼表。

`paiobs_task_stats.py` 是底层统计工具：`aggregate` 适合最快总量/基础指标；`overview` 适合自定义多维截面；`task-stats` 用于 skill/tool/model/数据源/文件类型等明细派生维度；`duration-stats` 只用于明确要求耗时分维度、慢任务或慢 skill/tool/model 分析；`token-stats` 用于单独补跑整体 Token 总计或用户明确要求 Token 分维度时展开；`credit-stats` 用于单独补跑整体 Credit/研究值总计或用户明确要求研究值消耗时展开；`compare` 仅在用户明确要求周期对比时使用。

执行约束：

- 不依赖 `jq`、`yq` 等外部 JSON 工具；需要解析 JSON 时优先用 Python 标准库或本 skill 脚本内置输出。
- 简单聚合统计必须优先走快捷脚本，不要先跑 Markdown、再跑 JSON、再本地抽 TopN。
- quickstat Markdown 必须按 `references/stats_report_template.md` 的 11 章结构组织。
- 报告中的成功率统一指“已结束任务成功率”：`success_count / (question_count - running_count)`；running 行没有已结束任务时成功率留空。
- 耗时和 Token 都单独成章且靠前展示。
- 不要只摘 Overall 几行，也不要改成“总览/主要分布/亮点”这类自定义摘要结构。

## 性能口径

- 标准字段（日期、小时、状态、入口、用户、机构、角色、联网搜索、联网 query 语言等）走 SQL 聚合，不走 task 明细 JSON 扫描。
- quickstat 默认由 `/reports/aggregate` 一次完成；fallback 时基础聚合和 detail `task-stats` 并行执行。detail `task-stats` 默认带 `include_token_stats=true`，Token 总计优先从同一次 detail 响应的 `token_metrics` 读取。
- 只有网关未返回 `token_metrics` 时，快捷脚本才 fallback 补跑一次 `token-stats --group-by none --token-dimensions none`。
- `overview` 默认不拉 detail buckets；只有需要自定义拆分或排查快捷脚本结果时，才回退到 `overview + task-stats` 两步。
- `duration-stats` 不作为“简单聚合统计”的默认数据源；耗时总计直接使用 `overview` 的整体 `avg_duration/p50_duration/p90_duration/p95_duration`。
- `token-stats` 走 task-stats 的服务端 JSON 解析，统计任务执行过程中的 LLM usage；默认只取整体总计。需要新版网关支持 `include_token_stats`。
- `credit-stats` 走 task-stats 的服务端 SQL 关联，统计 `saas.saas_point_freeze_order` 中 `business_no = question_id/feedback_question_id` 的研究值订单；`consumed_points` 是实际消耗并输出为 `total_credits`，`frozen_points` 是预扣冻结值。
- `task-stats` 明细扫描受网关默认/硬上限约束；月级别全量统计优先直接提高 `--max-tasks`，若仍超限再按天/周拆片。具体上限和环境变量见 `references/internal_api.md` 的 Analytics 部分。
- `PAI_OBS_STATS_AGGREGATE_WORKERS` 控制 CLI overview/duration 并发聚合请求数。

## 常用维度

基础维度：`day`、`hour`、`status`、`entry_scene`、`scheduled`、`is_web_search`、`end_type`、`user`、`user_id`、`institution`、`institution_nature`、`inst_type`、`product_type`、`user_type`、`user_role`

明细维度：`skill`、`tool_name`、`tool_type`、`model`、`data_source_type`、`source_provider`、`source_domain`、`source_title`、`source_id`、`file_type`、`file_role`

数据源口径：

- `data_source_type` 是内容/数据类型（如 `web`、`report`、`comment`、`edb`、`ann`、`roadshow`、`social_media`），回答“用了什么类别的数据”。
- `source_provider` 是平台/渠道（如今日头条、新浪财经、东方财富网），回答“来自哪里”。
- `source_domain` 是 URL 域名。

文件口径：`file_type` 是任务携带、读取、生成或变更的文件/材料类型（如 `md`、`xlsx`、`docx`、`png`、`generated_report`，也可能有 `report`、`web` 这类材料类型）；它和数据源统计不同，回答“任务里有哪些文件/材料”，不回答来源平台。

## Quickstat 快捷入口

标准简单聚合统计：

```bash
python3.11 scripts/paiobs_quick_stats_report.py \
  --start-time "2026-05-01 00:00:00" \
  --end-time "2026-05-14 23:59:59" \
  --display-top-n 10 \
  --detail-top-n 10 \
  --max-tasks 100000 \
  --report-md /tmp/paiobs_stats_report.md \
  --json-output /tmp/paiobs_stats_report.json \
  --format json
```

过去 1h 可以省略时间窗，快捷脚本默认按当前本机时间往前 1 小时统计：

```bash
python3.11 scripts/paiobs_quick_stats_report.py --last-hours 1 --format json
```

如果用户只要口头/聊天窗口里的“简单聚合统计”，也先用快捷脚本生成报告，再把报告中的 1-11 章核心内容贴回对话。为控制篇幅，可以每张高基数表只展示 Top 5 或 Top 10，但 11 个章节都必须出现；没有数据的章节要写“无/未返回”，不能省略。

回复里必须包含整体 query/session/用户/已结束任务成功率、失败和 running、耗时总计 P50/P90/P95、Token 总计和任务级 Token P50/P90/P95、入口/调度/联网、联网英文/其他 query、用户画像、产品类型、机构 Top10 和明细 TopN。如果 Token 部分为空或网关不支持 `include_token_stats`，快捷脚本会自动 fallback 补跑一次 Token 总计，报告里要说明。

## 明细 TopN

```bash
python3.11 scripts/paiobs_task_stats.py task-stats \
  --start-time "2026-05-13 00:00:00" \
  --end-time "2026-05-14 00:00:00" \
  --dimensions skill,tool_name,model,data_source_type,source_provider,file_type \
  --top-n 20 \
  --max-tasks 100000 \
  --format table
```

## 耗时专题

用户明确要求耗时分维度或慢任务专题时，单独运行 `duration-stats`：

```bash
python3.11 scripts/paiobs_task_stats.py duration-stats \
  --start-time "2026-05-13 00:00:00" \
  --end-time "2026-05-14 00:00:00" \
  --group-by status,entry_scene,user_role \
  --format markdown
```

需要慢 skill/tool/model 桶时显式打开：

```bash
python3.11 scripts/paiobs_task_stats.py duration-stats \
  --start-time "2026-05-13 00:00:00" \
  --end-time "2026-05-14 00:00:00" \
  --group-by status,entry_scene,user_role \
  --slow-dimensions skill,tool_name,model \
  --max-tasks 100000 \
  --format markdown
```

## Token 和 Credit

单独补跑默认 Token 总计：

```bash
python3.11 scripts/paiobs_task_stats.py token-stats \
  --start-time "2026-05-13 00:00:00" \
  --end-time "2026-05-14 00:00:00" \
  --group-by none \
  --token-dimensions none \
  --max-tasks 100000 \
  --format markdown
```

只有用户明确要求“按模型/状态/入口拆 token”时，才把 `--group-by` 或 `--token-dimensions` 改成具体维度。

单独补跑 Credit/研究值总计：

```bash
python3.11 scripts/paiobs_task_stats.py credit-stats \
  --start-time "2026-05-13 00:00:00" \
  --end-time "2026-05-14 00:00:00" \
  --group-by none \
  --credit-dimensions none \
  --max-tasks 100000 \
  --format markdown
```

Credit 口径：网关从 `saas.saas_point_freeze_order` 按 `business_no = question_id/feedback_question_id` 关联任务，`consumed_points` 为实际消耗研究值并输出为 `total_credits`；不要用 token 估算 credit。

## 周期对比

只有用户明确要求和上一等长周期比较时才使用：

```bash
python3.11 scripts/paiobs_task_stats.py compare \
  --start-time "2026-05-08 00:00:00" \
  --end-time "2026-05-15 00:00:00" \
  --format markdown
```

## 飞书发布

需要创建飞书文档并发送链接时，quickstat 支持 `--publish-lark --send`。通用命令、scope、fallback 和调试方式统一见 `references/delivery_lark.md`。
