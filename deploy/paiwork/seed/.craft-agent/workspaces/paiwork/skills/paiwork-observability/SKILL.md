---
name: paiwork-observability
description: >
  PaiWork 内网观测与迭代分析 skill。用于通过 Observability Gateway 的
  /api/internal/v1 接口检索历史问句、查看单轮
  QueryAgentTabs 上下文、挖掘 badcase/goodcase/投诉/新需求线索、读取聚合
  metrics/facets、做 AI 任务分类与质量评分、生成聚合统计/AI 分析/完整日报并发布到飞书。
  以下场景应触发：用户要求分析 PaiWork 历史任务、排查某个 session/question、
  统计成功率/耗时/失败/工具/skill/用户机构分布、挖掘负面反馈/正向反馈/无能力
  案例、生成评测种子集、定期完整日报，或明确提到 30100、6193、PaiWork、
  observability、QueryAgentTabs、paiobs、PaiWork 内网 API。
---

# PaiWork Observability

## 渐进披露机制

这个 `SKILL.md` 是入口和路由层：先读这里判断任务类型、默认口径、该打开哪份 reference。不要一开始批量读取所有资料；只有当用户的问题落到具体链路时，才按下方“任务路由”加载对应 reference。

使用顺序：

1. 先按“核心原则”和“任务路由”确定默认脚本、统计口径和需要的产物。
2. 只打开当前链路需要的 reference；需要 API 字段、profile、scope 或 metrics 时再打开 `references/internal_api.md`。
3. 执行脚本时从当前 skill 根目录运行，也就是包含 `SKILL.md`、`scripts/`、`references/` 的目录；不要假设任何固定安装路径存在。
4. 报告、CSV、JSONL、飞书发布等交付物要回传本地路径或飞书链接；外传前只保留完成判断所需的最小用户证据。

## 核心原则

优先通过 Observability Gateway 读取数据，不直连 SelectDB/SLS/VM，除非用户明确要求或 API 缺能力。发布/外发默认走 `release` profile：`http://192.168.15.57:30100`；本地调试走 `local`：`http://localhost:6193`。`--base-url` 或 `PAI_OBS_BASE_URL` 显式覆盖 profile。

清晰 workflow 要一气呵成。用户要求“完整日报”“跑通全流程”“测试性完整日报”“发飞书”等已经有明确编排入口的任务时，直接运行对应 orchestrator（如 `scripts/paiobs_daily_report.py`）到结束，并以 manifest/发布回执为准交付结果；不要先拆成 quickstat/search/doctor 等子流程来回试探，也不要因为非致命的预检差异中断正在运行的 workflow。只有命令返回失败、缺少必要凭据/权限、用户明确要求 dry-run/抽样/不发布，或用户中途打断时，才停止主流程。

统计口径统一使用聚合/统计接口。凡是问数量、占比、分布、成功率、耗时、Token、Credit/研究值、TopN、趋势或多维拆分，都优先走 `paiobs_task_stats.py` 或 `paiobs_quick_stats_report.py`；`paiobs_task_query.py search` 只用于检索明细、抽样、定位具体 query/session/task、关键词命中和补证据，不把 search 返回页的 `count` 当权威统计口径。只有用户明确要求“环比/同比/对比/上一周期”时才用 `compare`。

默认先取小包再加深：检索用 `lite`，快速判断用 `summary`，深度归因才取 `context` 或 bundle。任务执行步骤、输入输出文件、数据源、工具/skill 记录通常已经在 `summary/context/bundle` 的 JSON 里；其中 `summary.evidence` 和 `summary.agent_tabs` 会直接暴露已恢复的 SQL、ES DSL、工具入参、delegate/subagent instruction。只有这些数据包解释不了时，才考虑 `full` 或 raw log；不要在检查这些字段前断言「只能去 SLS 原始日志拿入参」。

批量补证据时优先走网关 batch 能力：`batch --profile summary` 用于批量 summary/evidence，`batch --profile qa` 用于批量完整问答，`batch --profile context` 用于批量 QueryAgentTabs。不要在本地循环调用单任务接口，除非只查一两条或旧网关缺少 `/history/questions/batch`。

把 session 当成多轮对话容器，把 query/task 当成 session 下的一轮问题。用户问到非首轮任务时，先用 `session` 或 `trace` 看同一 session 前序 queries，必要时按 task_index 递增追溯上下文，避免把当前问题孤立分析。

遵守只读和审计边界：不要输出 API key、数据库密码、AK/SK、token；不要把凭据文件提交到仓库或贴到报告里；导出、LLM analysis、飞书发送需要确认 scope 和授权可用。

## 任务路由

| 用户意图 | 默认入口 | 需要细节时读取 | 关键口径 |
| --- | --- | --- | --- |
| 健康检查、配置、API 字段、scope、profile | `scripts/paiobs.py health/meta` | `references/internal_api.md` | API key 从 `PAI_OBS_API_KEY` 或 `--api-key` 传入；不要泄露凭据。 |
| 历史问句检索、关键词、投诉/无能力/新需求线索、session/task 定位 | `scripts/paiobs_task_query.py` | `references/query_workflows.md`、必要时 `references/internal_api.md` | 查询脚本只做明细和证据，不做统计总量口径。 |
| Query 文本主题扫描、抽取 X/Twitter 账号、文件格式、竞品、命名数据源/数据意向等 | `scripts/paiobs_query_theme_scan.py` | `references/query_theme_scan.md` | 数据意向默认只抓用户明确点名的外部数据源/行业站/资讯平台，先用网关 `theme-search` 在 SQL 层召回候选，再本地总结；其他主题必要时 `collect` 全量语料后扫描。 |
| 简单/快速聚合统计：过去 1h、今天、最近 N 天任务 | `scripts/paiobs_quick_stats_report.py` | `references/stats_workflows.md`、`references/stats_report_template.md` | 默认生成 quickstat 11 章报告；成功率按已结束任务计算。 |
| 自定义聚合、耗时、Token、Credit、周期对比、明细 TopN | `scripts/paiobs_task_stats.py` | `references/stats_workflows.md`、必要时 `references/internal_api.md` | 标准字段走 SQL 聚合；detail 维度用 `task-stats`；成功率按已结束任务计算。 |
| AI 分析、质量评分、低分归因、批量标注 | `scripts/paiobs_ai_analysis.py` | `references/quality_workflows.md` | 本地逐条标注，默认 4 路并发并输出清晰进度；默认 `openai-compatible + openai-gpt-4.1`，日报链路只分析已结束任务。 |
| 每日 01:00 完整日报、指定窗口完整日报、测试性完整日报 | `scripts/paiobs_daily_report.py` | `references/daily_report_workflow.md` | 单一 orchestrator：聚合统计和关注任务候选走报告级网关 API，AI 分析本地并发标注已结束任务，最后生成日报/关注任务表格并发布飞书。 |
| 飞书文档/表格/机器人发送、发布故障排查 | `scripts/paiobs_lark.py` 或 quickstat `--publish-lark` | `references/delivery_lark.md` | 用户说“发飞书”默认是创建文档并发送链接；关注任务表格的 query、原因、建议、证据等长文本按普通文本字段发布，query 链接和最终文件产物按 URL 字段发布。 |

## 核心脚本

本 skill 按 6 个正交主模块和 1 个快捷编排入口拆分。新任务优先使用对应专用脚本；`paiobs.py` 只保留基础组件和兼容入口。`scripts/paiobs_query_theme_scan.py` 是查询模块的历史兼容辅助，用于全量语料主题扫描，不作为新的主入口。

| 脚本 | 职责 |
| --- | --- |
| `scripts/paiobs.py` | 基础 API 封装、环境配置读取、输出渲染、context/file/skill 解析组件；兼容旧 CLI。 |
| `scripts/paiobs_task_query.py` | 查询类功能：历史问句组合过滤、session/trace/task/context/bundle、文件/skill 检查、关键词 buckets、case mining/export。 |
| `scripts/paiobs_task_stats.py` | 聚合统计：`aggregate`、`facets`、`task-stats`、`duration-stats`、`token-stats`、`credit-stats`、`overview`、`compare`；统一走网关 `/analytics/*`。 |
| `scripts/paiobs_quick_stats_report.py` | 聚合统计报告快捷入口：优先一次调用网关 `/reports/aggregate`，旧网关才回退到并行聚合 + detail `task-stats`，直接渲染固定 11 章 Markdown。 |
| `scripts/paiobs_ai_analysis.py` | AI 分类与质量评分：逐条通过 Gateway 拉取 refs/context，本地默认 4 路并发标注；失败/超时 0 分快捷路径；输出逐条进度和 JSONL。 |
| `scripts/paiobs_lark.py` | 飞书发布：报告转飞书文档，关注任务 xlsx 转飞书多维表格，并通过机器人或 user 通道发送。 |
| `scripts/paiobs_daily_report.py` | 完整日报编排：默认上一自然日 24h，聚合统计 + Gateway 关注任务候选 + 本地已结束任务 AI 标注 + 关注任务表格 + manifest，并发布到飞书。 |

## 快速入口

```bash
# 先进入当前 skill 根目录：包含 SKILL.md、scripts/、references/ 的目录
python3.11 scripts/paiobs.py health
python3.11 scripts/paiobs.py meta --format table
python3.11 scripts/paiobs.py --gateway-profile local health
```

过去 1h 简单统计默认走 quickstat：

```bash
python3.11 scripts/paiobs_quick_stats_report.py --last-hours 1 --format json
```

单个 session/task 排查从浅到深：

```bash
python3.11 scripts/paiobs_task_query.py session <session_id> --format table
python3.11 scripts/paiobs_task_query.py trace <session_id> <task_index> --format table
python3.11 scripts/paiobs_task_query.py task <session_id> <task_index> --profile summary --output summary.json
python3.11 scripts/paiobs_task_query.py context <session_id> <task_index> --output context.json
python3.11 scripts/paiobs_task_query.py batch <session_id>:<task_index> --profile qa --output batch_qa.json
```

指定窗口 AI 分析：

```bash
python3.11 scripts/paiobs_ai_analysis.py analyze-batch \
  --start-time "2026-05-13 00:00:00" \
  --end-time "2026-05-14 00:00:00" \
  --ended-only \
  --workers 4 \
  --limit 1000 \
  --output /tmp/paiobs_ai_analysis.jsonl
```

每日完整日报：

```bash
python3.11 scripts/paiobs_daily_report.py --target-date 2026-05-17
```

## 报告契约

简单聚合统计必须按 `references/stats_report_template.md` 的 11 章结构输出：总览、耗时总计、Token 总计、状态、入口、调度与联网、用户画像、产品类型、机构 Top10、明细 TopN、简要结论。聊天窗口口头汇总也先生成 quickstat 报告，再贴核心内容；没有数据的章节写“无/未返回”，不能省略。

报告类工作流只有三类：聚合统计、AI 分析、完整日报。日报、周报、质量分布和临时窗口 AI 分析必须只针对已结束任务；`running`、`pending`、`queued`、`processing`、`in_progress`、`进行中`、`运行中`、`排队`、`待处理` 任务一律不进入 AI 标注、低分池或飞书关注任务表格。`--ended-only` 是硬约束：只有明确终态 status/end_type/task_status_desc、response_time 或 success=true 的任务才允许标注；缺少终态信号时按未结束处理，不能因为 status 为空就放行。日报关注任务表格必须包含 5 个 sheet：失败任务、用户抱怨任务、高耗时任务、高token消耗任务、低分任务；失败/用户抱怨/高耗时/高 token 由 Gateway `/reports/focus-tasks` 按已结束任务查询并补全字段，用户抱怨按 query 内容命中抱怨关键词进入，高耗时默认阈值 900 秒，高 token 默认阈值 300000，且高耗时/高 token 不包含失败任务；低分任务由本地 AI 标注按结果评分 `<5` 进入，且不包含失败任务。关注任务表格所有 sheet 都必须按 queryid 或 session_id + task_index 回填本地 AI 标注字段，不只低分任务：中文查询一级/二级分类、中文问题一级/二级归因、责任人、结果评分、消耗 token、消耗 credit、低分原因、改进建议和证据等分析字段都要尽量补齐。关注任务表格/低分明细必须拆列包含 queryid、session_id、task_index、query 原文、query 链接、answer 内容、最终文件产物 URL、请求/响应时间、状态、错误信息、入口、调度类型、是否联网、用户、机构、产品、中文查询一级/二级分类、中文问题一级/二级归因、责任人、结果评分、消耗 token、消耗 credit、低分原因和改进建议；全空列（如错误信息、缓存token、LLM调用数等每条数据都无值的列）不写入 xlsx 以保持表格简洁；列顺序优先放定位信息与 query/query 链接，`最终文件产物` 紧跟 `answer内容` 后面；调度类型按聚合报告口径输出 `scheduled/manual`，是否联网输出 `true/false`；飞书多维表格中查询一级/二级分类、问题一级/二级归因、用户类型、用户角色、产品类型按单选标签字段发布，责任人在配置了 `PAI_OBS_OWNER_OPEN_IDS` 时按 people 类型字段发布以显示蓝色 @ 链接，否则按普通文本字段发布；内部复盘表不对 query、answer 或最终文件产物 URL 做脱敏或截断；credit 来自网关/订单关联结果，未返回时留空或写“未返回”，不要估算。生成关注任务表格时必须保留 0 分、0 token、False 等有效零值，不能用空字符串覆盖。

问题归因必须使用新的一级/二级分类和责任人机制。一级分类只能是 `数据类`、`算法类`、`业务类`。二级分类直接决定责任人，责任人列保留 @ 前缀，支持多个 @ 人：数据类中 `外资研报`、`外资纪要` 归 `@周宛蓉`，`会议纪要` 归 `@王宝涵 @王意荃`，`路演日历`、`研报`、`固收` 归 `@王宝涵`，`个股基本面`、`调研、策略会日历`、`公告`、`未分类` 归 `@陆雯`；算法类统一用 `技术问题`，责任人 `@王能`；业务类中 `国内股票类` 归 `@韩字杰`，`海外类` 归 `@凤超`，`基金、蓝宝书类` 归 `@齐涤非`，`其他未归类` 归 `@王峰伊`。算法类用于搜索设计、tool 调用入参、SQL/DSL、接口、文件读写、联网检索、规划/路由等 tools 层问题；业务类用于 skill 执行流程、金融行业规范、业务口径和交付不符合研究习惯等问题。

## 目录结构

```text
paiwork-observability/
  SKILL.md
  .paiobs.env                         # 可选，本地私有配置；不要提交或外传
  agents/openai.yaml                  # skill 展示元信息
  scripts/
    paiobs.py
    paiobs_task_query.py
    paiobs_task_stats.py
    paiobs_quick_stats_report.py
    paiobs_query_theme_scan.py
    paiobs_ai_analysis.py
    paiobs_lark.py
    paiobs_daily_report.py
  references/
    internal_api.md
    query_workflows.md
    query_theme_scan.md
    stats_workflows.md
    stats_report_template.md
    quality_workflows.md
    daily_report_workflow.md
    delivery_lark.md
```

## 资源索引

- `references/internal_api.md`：Gateway connection、profile、scope、输出格式、endpoint、search filter、analytics metrics/facets、case kind。
- `references/query_workflows.md`：历史问句查询、关键词 OR、bucket、投诉/无能力/新需求挖掘、detail CSV、session/task 深挖命令。
- `references/query_theme_scan.md`：全量 query 语料 collect、X/Twitter 账号扫描、通用 regex 主题扫描、detail CSV 和审计建议。
- `references/stats_workflows.md`：quickstat 默认链路、自定义聚合、性能口径、常用维度、耗时/Token/Credit/compare 命令。
- `references/stats_report_template.md`：quickstat Markdown 的 11 章模板和字段位置。
- `references/quality_workflows.md`：AI 分类评分、低分模板、批量标注和深度 case 复盘。
- `references/daily_report_workflow.md`：每日 01:00 完整日报的调度、参数、产物、manifest 和运行前检查。
- `references/delivery_lark.md`：飞书文档、关注任务表格、机器人/user 发送、scope、profile、quota fallback 和 dry-run。
