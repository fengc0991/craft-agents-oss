# PaiWork AI 分析、分类与质量评分工作流

## AI 标注默认链路

AI 标注用 `paiobs_ai_analysis.py`。默认 AI 任务分析模型统一使用 `openai-compatible` provider 的 `openai-gpt-4.1`。脚本仍通过 Observability Gateway 读取 search refs、summary/context/qa 和文件预览；批量 context 拉取优先走 `/history/questions/batch`，质量判断仍在本地按单条任务执行，默认 4 路并发，不走服务端 `/analysis/batch`。

只有需要复验服务端分析接口或对比网关专用链路时，才显式传 `--ai-provider gateway`；gateway provider 也按单条任务调用 `/analysis/query`，同样受 `--workers` 并发数控制。

默认评分只看四类信息：

- 基本数据
- 用户输入
- 最终输出
- 运行指标

不要看过程步骤、tool calls、Agent 委托链路、research_step、内部执行日志；只有用户明确要求“深挖过程/复盘链路/排查某个 case”时才拉 `context/full/bundle`。

## 标注输出契约

统一 prompt 输出 JSON，包含：

- query 1-2 级分类：参考 `userlogs/docs/test_collection/classify_standard.md` 的金融研究任务 taxonomy。
- `result_score`：满分 10 分，只看用户需求与最终 answer/文件产物的 end-to-end 满足度。
- `overall_score`：为兼容历史下游保留，必须等于 `result_score`。
- 问题归因：`issue_level1`、`issue_level2`、`owner`。所有已结束任务都输出 1-2 级问题归因和责任人。
- `low_score_reason` 和 `improvement_suggestion`。这两个字段只要求低分任务填写，非低分任务可留空。
- 运行消耗：必须从 context/summary 原样带出 `token_total`、`prompt_tokens`、`completion_tokens`、`credit_total`；credit/研究值来自 `saas.saas_point_freeze_order.consumed_points`，网关按 `business_no = question_id/feedback_question_id` 关联并输出为 `credit_usage.total_credits`。网关没有返回 credit 时写空或“未返回”，不要估算。

问题归因分类必须使用新的中文 taxonomy：一级分类只能是 `数据类`、`算法类`、`业务类`；二级分类只输出分类名称，不要附带责任人，责任人列保留 @ 前缀且允许一个单元格多个 @ 人。数据类：`外资研报`、`外资纪要` 归 `@周宛蓉`，`会议纪要` 归 `@王宝涵 @王意荃`，`路演日历`、`研报`、`固收` 归 `@王宝涵`，`个股基本面`、`调研、策略会日历`、`公告`、`未分类` 归 `@陆雯`。算法类统一用 `技术问题`，责任人 `@王能`，覆盖搜索设计、tool 调用入参、SQL/DSL、接口、文件读写、联网检索、规划/路由等 tools 层问题。业务类：`国内股票类` 归 `@韩字杰`，`海外类` 归 `@凤超`，`基金、蓝宝书类` 归 `@齐涤非`，`其他未归类` 归 `@王峰伊`，覆盖 skill 执行流程、金融行业规范、业务口径和交付不符合研究习惯等问题。

分类名称必须使用中文：`query_major_category`、`query_minor_category`、`issue_level1`、`issue_level2`、`owner` 输出和下游 CSV/飞书表都不要保留英文枚举名。脚本会对历史英文枚举做中文归一，并按 `issue_level2` 强制派生 `issue_level1` 和 `owner`。

低分问题模板是硬约束：凡是输出低分明细、低分 CSV、低分样本表、飞书关注任务表格的低分 sheet 或临时手工产物，都必须拆列包含 queryid、session_id、task_index、query 内容、query 链接、answer 内容、最终文件产物 URL、请求/响应时间、状态、错误信息、入口、调度类型、是否联网、用户、机构、产品、`query_major_category`、`query_minor_category`、`issue_level1`、`issue_level2`、`owner`、结果评分、消耗 token 和消耗 credit，不允许只合并成一个问题分类字段。完整日报关注任务表格的所有 sheet 都要从全量已结束任务 AI JSONL 回填这些 AI 字段，不只低分 sheet。列顺序优先放定位信息与 query/query 链接，`最终文件产物` 紧跟 `answer内容` 后面；调度类型按聚合报告口径输出 `scheduled/manual`，是否联网输出 `true/false`。低分 sheet 不包含失败任务，失败/超时/异常任务只进入失败任务 sheet。0 分、0 token、False 等有效零值必须原样输出，不能被空字符串吞掉。

失败、超时、异常任务默认直接 0 分，不消耗 LLM；需要关闭时传 `--no-shortcut-failed`。

## Provider 和进度

默认 provider 是 `openai-compatible`，默认模型是 `openai-gpt-4.1`。API key 优先读取 `PAI_OBS_AI_API_KEY`、`GLOBAL_DATA_LLM_API_KEY`、`OVERSEADATA_LLM_API_KEY`、`OPENAI_API_KEY`，仍未配置时尝试读取 `/root/rabit/deeptask/overseadata-server/config.yaml`。不要把 key 写入报告或提交到仓库。

服务端 Gateway 分析接口保留为显式 fallback：需要时传 `--ai-provider gateway`，它会按单条任务调用 Observability `/analysis/query`，模型由服务端配置决定。

批量任务必须持续输出逐条进度到 stderr，避免空等。脚本默认 `--progress`，会在每条任务开始和结束时输出 `index/total`、进度百分比、record/session/task、成功、失败或跳过、单条耗时、累计成功/错误/跳过数、总体耗时、速率、ETA、评分和低分标记；静默运行才显式传 `--no-progress`。

执行约束：常规批量分析应使用默认 `openai-compatible + openai-gpt-4.1` 模式和默认 `--complete-search`。AI 标注阶段默认 `--workers 4`，建议常规设置在 3-5 之间；`--workers 1` 可退回串行排查。`--gateway-batch-size` 仅保留为兼容参数，不使用服务端 batch 分析。不要用 `prepare-contexts -> analyze-batch --context-jsonl` 作为批量默认路径；那条路径只用于离线复验或需要固定输入快照的实验。

## 标准批量用法

```bash
python3.11 scripts/paiobs_ai_analysis.py analyze-batch \
  --start-time "2026-05-13 00:00:00" \
  --end-time "2026-05-14 00:00:00" \
  --ended-only \
  --workers 4 \
  --limit 1000 \
  --output /tmp/paiobs_ai_analysis.jsonl
```

如果 search 单页上限导致明细不足，保留默认 `--complete-search`。脚本会按时间片自适应拆分 search，把 refs 去重后拉取 summary/context，再用 `openai-gpt-4.1` 按单条任务并发做 JSON 分类评分；不要把 search 首页 `count` 当作全量。

日报、周报、质量分布统计和临时窗口分析默认加 `--ended-only`，只分析 success/failed/timeout/cancelled 等已结束任务；running/pending/queued/processing/in_progress 以及中文“进行中/运行中/排队/待处理”任务等待后续截面，不进入本轮 AI 低分池。

`--ended-only` 必须保守执行：只有明确终态 `status`/`end_type`/`task_status_desc`、`response_time` 或 `success=true` 的任务才允许进入 AI 标注。缺少终态信号时按未结束处理，不得因为 `status` 为空、`success=false` 或 search refs 字段不全而放行。若需要分析正在运行的任务，只能走人工排查链路读取 session/trace/context，不能调用 AI 标注或生成关注任务表格。

两层分析模式：

1. 大批量扫描、日报/周报、质量分布统计：默认用 `openai-compatible + openai-gpt-4.1`，对基本数据、输入、最终输出和指标做 JSON 分类评分。
2. 低分/典型 case 需要复盘时，再由 Agent 定点深挖：拉 `session`、`trace`、`task --profile context/full`、`context`、`bundle`、`inspect/files/skills`，查看完整 QueryAgentTabs、过程步骤、文件、工具、skill 和原始证据。
3. 完整日报和复盘时先用默认 AI 标注得到已结束任务的分类、评分、低分池和代表样本；只对少量代表样本做 deep explore。若要比较服务端 Gateway 分析链路，再显式传 `--ai-provider gateway`。

对单个典型 case 做带过程的深度归因时，再显式加深：

```bash
python3.11 scripts/paiobs_ai_analysis.py analyze-batch \
  <session_id>:<task_index> \
  --context-profile context \
  --include-process \
  --include-raw-context \
  --max-context-chars 6000 \
  --ai-provider openai-compatible \
  --output /tmp/paiobs_case_deep_analysis.jsonl
```

如果不需要 LLM 二次判断，而是由当前 Agent 手工排查，优先用：

```bash
python3.11 scripts/paiobs_task_query.py session <session_id> --format table
python3.11 scripts/paiobs_task_query.py trace <session_id> <task_index> --format table
python3.11 scripts/paiobs_task_query.py context <session_id> <task_index> --output /tmp/paiobs_case_context.json
python3.11 scripts/paiobs_task_query.py inspect --context-json /tmp/paiobs_case_context.json --format table
python3.11 scripts/paiobs_task_query.py files --context-json /tmp/paiobs_case_context.json --format table
python3.11 scripts/paiobs_task_query.py skills --context-json /tmp/paiobs_case_context.json --format table
```

需要临时覆盖默认 AI 标注 provider 或模型时：

```bash
export PAI_OBS_AI_PROVIDER=openai-compatible
export PAI_OBS_AI_BASE_URL=https://test-llm.rabyte.cn
export PAI_OBS_AI_API_KEY=sk_xxx
export PAI_OBS_AI_MODEL=openai-gpt-4.1
export PAI_OBS_AI_FALLBACK_MODELS=kimi-k2.6,tencent-gpt-5.4
export PAI_OBS_AI_TIMEOUT=45
export PAI_OBS_AI_RETRIES=1
```

只想让当前 Agent 自己读 prompt/JSONL 标注，传 `--ai-provider none`，脚本会输出每条任务的完整 prompt。

## 下游使用

本文件只描述 AI 分析。独立评分报告入口已移除；需要完整日报时使用 `scripts/paiobs_daily_report.py`，它会先跑聚合统计和 Gateway 关注任务查询，再调用本地 `paiobs_ai_analysis.py analyze-batch --ended-only --workers <N>` 并发标注并逐条输出进度，最后在本地汇总日报、关注任务 xlsx 和兼容低分 CSV。

低分 CSV 字段必须包括：queryid、session_id、task_index、query 内容、query 链接、answer 内容、最终文件产物、请求时间、响应时间、状态、错误信息、成功、入口、调度类型、是否联网、用户 ID、用户名、用户机构、机构性质、机构类型、用户类型、用户角色、产品类型、耗时秒、结果评分、中文查询一级分类、中文查询二级分类、中文问题一级分类、中文问题二级分类、责任人、消耗 token、消耗 credit、低分原因、改进建议、证据、skills、是否有附件。`query链接` 使用 `http://192.168.15.57:30100/?queryid=<queryid>` 格式，优先取 `query_id/queryid/question_id/feedback_question_id` 中可用的任务 id；`最终文件产物` 只保留文件产物的 `content_url`（无名称、路径或其他说明），没有 `content_url` 时才回退 `preview_url`。query/answer 内容按内部复盘口径保留原文，不做脱敏或截断。如果 credit 未返回，保留 `消耗credit` 列并填空或“未返回”，不要删除该列。
