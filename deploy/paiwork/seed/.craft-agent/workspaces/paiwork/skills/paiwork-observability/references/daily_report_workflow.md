# PaiWork 每日完整日报工作流

## 调度约定

定时能力由 agent 系统提供，本 skill 只提供可调用入口。每天 01:00 调用：

```bash
# 先进入当前 skill 根目录：包含 SKILL.md、scripts/、references/ 的目录
python3.11 scripts/paiobs_daily_report.py
```

默认窗口是 Asia/Shanghai 时区上一自然日：

- `start_time = 昨天 00:00:00`
- `end_time = 今天 00:00:00`
- `ai_limit = 0`，表示对窗口内所有已结束任务做 AI 标注
- `ai_workers = 4`，表示 AI 标注默认 4 路并发；常规建议 3-5
- `recipient = fengchao`，默认创建飞书文档和关注任务多维表格后私信发送
- 关注任务固定阈值：高耗时 `>= 900` 秒，高 token `>= 300000`，低分 `结果评分 < 5`；高耗时、高 token、低分均排除失败任务

## 完整链路

`paiobs_daily_report.py` 是定时任务的单一入口，内部按顺序编排已有模块：

1. 计算时间窗口：默认上一自然日 24h，也支持 `--target-date` 或 `--start-time/--end-time` 覆盖。
2. 运行 `paiobs_quick_stats_report.py` 生成聚合统计：
   - 优先一次调用 Gateway `/api/internal/v1/reports/aggregate`，旧网关才回退到多接口聚合。
   - 输出 quickstat 11 章 Markdown 和原始 JSON。
3. 调用 Gateway `/api/internal/v1/reports/focus-tasks` 生成关注任务候选：
   - Gateway 在服务端收集窗口内已结束任务并补全 summary 字段，返回 `失败任务`、`用户抱怨任务`、`高耗时任务`、`高token消耗任务` 四个 sheet 的现成行。
   - 默认阈值：高耗时 `>= 900` 秒，高 token `>= 300000`；高耗时和高 token 不包含失败任务；只包含已结束任务，running/pending 不进入关注任务候选。
4. 运行本地 `paiobs_ai_analysis.py analyze-batch`：
   - 保留本地单条任务标注语义，默认 4 路并发，并输出清晰 stderr 进度。
   - 默认传 `--ended-only`，仅分析 success/failed/timeout/cancelled 等已结束任务，running/pending 不进入 AI 标注。
   - `--ended-only` 按保守终态判断：只有明确终态 status/end_type/task_status_desc、response_time 或 success=true 才进入 AI；缺少终态信号的 search ref 按未结束处理。
   - `--ai-limit 0` 时按 `--max-tasks` 全量收集 refs，不把 search 首页当全量。
   - 输出 AI JSONL 明细；结果按输入顺序写出，终端进度按完成顺序显示。
5. 本地汇总完整日报 Markdown、关注任务 xlsx 和兼容低分 CSV：
   - 关注任务 xlsx 包含 5 个 sheet：失败任务、用户抱怨任务、高耗时任务、高token消耗任务、低分任务。
   - 本地 AI JSONL 会按 queryid 或 `session_id + task_index` 回填到所有关注 sheet，不只低分 sheet；查询分类、问题归因、结果评分、token/credit、低分原因、改进建议和证据等 AI 分析字段都尽量补齐。
   - 低分任务 sheet 来自本地 AI 标注，默认 `结果评分 < 5`，且不包含失败任务。
6. 运行 `paiobs_lark.py publish`：
   - 将 Markdown 报告创建为飞书文档。
   - 将关注任务 xlsx 创建为飞书多维表格，并把 query、answer、原因、建议、证据、责任人、用户/机构等文本字段更新为普通文本，把 `query链接` 和 `最终文件产物` 更新为 URL 字段；查询一级/二级分类和问题一级/二级归因更新为单选标签字段，分类值按批量更新回写。责任人列保留文本，允许一个单元格写多个 @ 人。
   - 私信发送给 `fengchao`，消息包含报告和表格链接；发送、scope、fallback 规则见 `references/delivery_lark.md`。
7. 写出 workflow manifest JSON，记录窗口、产物路径、AI 标注数、关注任务数、低分数和飞书发布结果。

## 执行纪律

收到“完整日报”“跑通全流程”“测试性完整日报”“过去 N 分钟/小时完整日报”这类请求时，直接计算时间窗口并运行 `paiobs_daily_report.py` 到命令结束。不要先跑 quickstat/search 预检来决定是否继续；不要因为预估数量、分页返回、doctor 的非致命提示或中途日志看起来异常就停止主流程。疑点在 workflow 完成后通过 manifest、报告、JSONL 行数和发布回执复核，并在最终回复里说明。

“测试性完整日报”仍然表示真实跑通完整链路：生成聚合统计、Gateway 关注任务结果、AI JSONL、完整日报 Markdown、关注任务 xlsx、manifest，并按默认语义发布飞书和发送链接。除非用户明确说“dry-run”“只抽样”“不发布”或指定 `ai-limit`，否则不要加 `--dry-run`、`--no-publish`、`--no-send`，也不要把 `--ai-limit` 改成 1。

运行中只做两件事：定期给用户简短进度更新，或在子命令失败时处理失败。不要主动 kill 已启动的日报流程来改跑别的组合命令；如果必须重跑，先说明上一次失败的明确错误，并保留旧输出目录供排查。

完整链路的交付判定：

- `paiwork_daily_<window>.md` 已生成。
- `paiwork_daily_<window>_aggregate_stats.md/json` 已生成。
- `paiwork_daily_<window>_focus_tasks.xlsx` 已生成，包含失败任务、用户抱怨任务、高耗时任务、高token消耗任务、低分任务 5 个 sheet，允许各 sheet 为空。
- `paiwork_daily_<window>_analysis.jsonl` 已生成；完整 AI 标注时以 manifest 记录的 `analysis_count` 和 JSONL 行数为准，允许已结束任务为 0 时为空文件。
- `paiwork_daily_<window>_low_scores.csv` 作为兼容低分明细已生成，允许低分为空表。
- `paiwork_daily_<window>_manifest.json` 已生成，并记录飞书发布成功、发送成功、fallback 或失败原因。

## 产物

默认本地输出目录：

```text
/tmp/paiobs_daily_reports/<start>_to_<end>/
```

每次运行会生成：

- `paiwork_daily_<window>.md`：最终完整日报，包含聚合统计和已结束任务 AI 分析摘要。
- `paiwork_daily_<window>_aggregate_stats.md`：单独落盘的 quickstat 11 章聚合统计报告。
- `paiwork_daily_<window>_aggregate_stats.json`：聚合统计原始 JSON。
- `paiwork_daily_<window>_focus_tasks.xlsx`：关注任务表格，包含 5 个 sheet：失败任务、用户抱怨任务、高耗时任务、高token消耗任务、低分任务。各 sheet 都优先放置 queryid、session_id、task_index、query 内容、query 链接、answer 内容、最终文件产物 URL，再包含时间、状态、错误信息、入口、调度类型、是否联网、用户、机构、产品、中文查询分类、中文问题归因、责任人、token、credit、结果评分、证据和建议列；这些 AI 分析字段会从全量已结束任务标注结果回填到所有 sheet。
- `paiwork_daily_<window>_low_scores.csv`：兼容低分任务 CSV，字段与低分 sheet 保持一致。
- `paiwork_daily_<window>_analysis.jsonl`：已结束任务逐条 AI 标注原始结果。
- `paiwork_daily_<window>_manifest.json`：编排结果和发布回执。

默认飞书路径：

```text
tmp/<target-date>_paiwork_daily_report.md
tmp/<target-date>_paiwork_focus_tasks.bitable.xlsx
```

## 常用参数

重跑某一天：

```bash
python3.11 scripts/paiobs_daily_report.py --target-date 2026-05-17
```

指定任意窗口：

```bash
python3.11 scripts/paiobs_daily_report.py \
  --start-time "2026-05-17 00:00:00" \
  --end-time "2026-05-18 00:00:00"
```

小流量真实链路测试，仍会发送给 fengchao：

```bash
python3.11 scripts/paiobs_daily_report.py \
  --start-time "2026-05-18 14:00:00" \
  --end-time "2026-05-18 14:10:00" \
  --ai-limit 1 \
  --ai-workers 4 \
  --max-tasks 20
```

过去 10 分钟的测试性完整日报，默认全量标注已结束任务并发布：

```bash
START_TIME=$(TZ=Asia/Shanghai date -d '10 minutes ago' '+%Y-%m-%d %H:%M:%S')
END_TIME=$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S')
python3.11 scripts/paiobs_daily_report.py \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --ai-limit 0 \
  --ai-workers 4 \
  --max-tasks 1000000 \
  --send-as bot
```

只验证本地产物和飞书命令，不实际创建飞书对象或发送消息：

```bash
python3.11 scripts/paiobs_daily_report.py \
  --target-date 2026-05-17 \
  --ai-limit 1 \
  --dry-run
```

只生成报告，不发布飞书：

```bash
python3.11 scripts/paiobs_daily_report.py --target-date 2026-05-17 --no-publish
```

飞书 fallback、`--no-fallback-files` 和发送语义见 `references/delivery_lark.md`。

## 环境变量

通用 Gateway 配置见 `references/internal_api.md`，AI provider 配置见 `references/quality_workflows.md`，飞书 profile/send 配置见 `references/delivery_lark.md`。

日报入口新增可选变量：

- `PAI_OBS_DAILY_TIMEZONE`：默认 `Asia/Shanghai`
- `PAI_OBS_DAILY_OUTPUT_DIR`：默认 `/tmp/paiobs_daily_reports`
- `PAI_OBS_DAILY_REMOTE_DIR`：默认 `tmp`，这是当前 `feishu-sync` 已同步目录。生产如要使用 `paiwork_reports` 或子目录，需先确保该目录已被 `feishu-sync` 同步。
- `PAI_OBS_DAILY_RECIPIENT`：默认 `fengchao`
- `PAI_OBS_DAILY_AI_LIMIT`：默认 `0`，即全量标注已结束任务
- `PAI_OBS_DAILY_MAX_TASKS`：默认 `1000000`
- `PAI_OBS_DAILY_HIGH_DURATION_SECONDS`：关注任务高耗时阈值，默认 `900`
- `PAI_OBS_DAILY_HIGH_TOKEN_THRESHOLD`：关注任务高 token 阈值，默认 `300000`
- `PAI_OBS_DAILY_LOW_SCORE_THRESHOLD`：关注任务低分阈值，默认 `5`

## 运行前检查

```bash
python3.11 scripts/paiobs.py health
python3.11 scripts/paiobs_lark.py doctor --format json
python3.11 scripts/paiobs_daily_report.py --help
```

`paiobs_lark.py doctor` 的 scope 解释、profile 切换和 quota 处理见 `references/delivery_lark.md`。
