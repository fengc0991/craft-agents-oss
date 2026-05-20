# PaiWork Observability Internal API

## Connection

Default gateway profile: `release`

- `release`, `prod`, `product`, `production`, `publish`, `published`: `http://192.168.15.57:30100`
- `local`, `localhost`, `test`, `testing`, `debug`, `dev`: `http://localhost:6193`

`--base-url` and `PAI_OBS_BASE_URL` explicitly override the profile.

Environment variables:

- `PAI_OBS_GATEWAY_PROFILE`: gateway profile switch. Defaults to `release`. Use `local` for local debugging on `localhost:6193`.
- `PAI_OBS_PROFILE`: compatibility alias for `PAI_OBS_GATEWAY_PROFILE`.
- `PAI_OBS_BASE_URL`: explicit gateway base URL override.
- `PAI_OBS_API_KEY`: API key used as `Authorization: Bearer <key>`.
- `PAI_OBS_ENV`: default data environment, usually `product`.
- `PAI_OBS_TIMEOUT`: request timeout seconds, default `60`.
- `PAI_OBS_FILE_AUTH_TOKEN`: optional request-side PaiWork token for file preview/content proxy.
- `PAI_OBS_SKILL_ROOTS`: optional local or mounted skill roots, separated by `:` or newlines.

The gateway also accepts `X-PAI-OBS-API-Key`. The CLI uses `Authorization`.

If the skill root contains `.paiobs.env` or `paiobs.env`, CLI commands load missing environment values from it. Supported local env keys include `PAI_OBS_GATEWAY_PROFILE`, `PAI_OBS_BASE_URL`, `PAI_OBS_API_KEY`, `PAI_OBS_ENV`, and `PAI_OBS_TIMEOUT`. Treat these files as private credentials and do not commit or paste them into reports.

## Scopes

- `read`: `meta`, history search, task summary/context/qa, batch task payloads.
- `metrics`: aggregate, facets, case mining, jobs.
- `analysis`: single and batch LLM analysis.
- `export`: bundle zip and export jobs.
- `admin`: scope override on the server side.

If an operation returns 403, report the missing scope instead of bypassing the gateway.

## Profiles

- `lite`: search result fields and previews for triage.
- `summary`: task summary, input, process outline, result excerpt, evaluation, agents, plus compact `evidence` and `agent_tabs` containing recovered tool/subagent inputs.
- `qa`: full restored question and final answer, with answer files/sources/token/credit metadata, without the full process payload.
- `context`: full `query-agent-tabs/v1` package.
- `full`: raw restored detail.
- `raw`: raw search item.

Use `lite` for search, `summary` for initial investigation, and `context` for evidence-heavy diagnosis. `summary.evidence` exposes `sql`, `es_dsl`, `tool_inputs`, `subagent_instructions`, and `signals`; `summary.agent_tabs[*].files` mirrors the QueryAgentTabs tab files in a compact form so clients can inspect subagent input/process tabs without immediately downloading the full context.

For ordinary task process investigation, prefer `summary/context/bundle` before `full`. Do not conclude that SQL, ES DSL, or delegate/subagent instruction text is unavailable until checking `evidence`, `agent_tabs`, `context`, or `bundle`. `full` contains restored detail and can include raw text; use it only when the compact data package is missing a necessary field.

## CLI Output Formats

The CLI should use `--format table` as the default operator-facing format. In this skill, `table` means CSV-compatible rows: comma separated, first row as headers, and values quoted by Python's CSV writer when needed. It is intended for quick inspection and `.csv` files.

Use `--output result.csv` and read the file in chunks when the terminal transcript folds or truncates long output:

```bash
sed -n '1,80p' result.csv
sed -n '81,160p' result.csv
```

`--format table` does not truncate cells by default. Add `--max-cell-chars 200` for a short console view, or use `--format pretty` for the older aligned table layout. Reserve `--format json` for nested payload persistence or Python stdlib processing, and `--format jsonl` for streaming list items.

## Endpoints

| Method | Path | Scope | Purpose |
| --- | --- | --- | --- |
| GET | `/api/internal/v1/health` | auth only | Gateway liveness. |
| GET | `/api/internal/v1/meta` | read | Environments, limits, profiles, schemas, features. |
| GET | `/api/internal/v1/skills/content` | read | Read a skill body by `skill_name`, optionally for one `user_id`. |
| POST | `/api/internal/v1/history/questions/search` | read | Search historical questions. |
| POST | `/api/internal/v1/history/questions/theme-search` | read | Gateway-side theme candidate search with include/exclude keyword and regex groups. |
| GET | `/api/internal/v1/history/sessions/{session_id}/tasks/{task_index}` | read | Task summary/full/context by `profile`. |
| GET | `/api/internal/v1/history/sessions/{session_id}/tasks/{task_index}/context` | read | `query-agent-tabs/v1` context. |
| GET | `/api/internal/v1/history/sessions/{session_id}/tasks/{task_index}/bundle.zip` | export | ZIP bundle for one task. |
| POST | `/api/internal/v1/history/sessions/{session_id}/tasks/{task_index}/analysis` | analysis | LLM analysis for one task. |
| POST | `/api/internal/v1/history/questions/batch` | read | Batch task payloads by refs or search filters; `summary`/`qa` up to 100, `context`/`full` up to 20. |
| POST | `/api/internal/v1/history/questions/batch-context` | read | Compatibility alias for batch task payloads, default `profile=context`. |
| POST | `/api/internal/v1/analysis/query` | analysis | LLM analysis by `session_id` and `task_index`. |
| POST | `/api/internal/v1/analysis/batch` | analysis | Sync analysis, up to 20 tasks. |
| POST | `/api/internal/v1/analytics/aggregate` | metrics | Aggregate metrics. |
| POST | `/api/internal/v1/analytics/facets` | metrics | Facets and sampled detail dimensions. |
| POST | `/api/internal/v1/analytics/task-stats` | metrics | Server-side task stats for detail dimensions such as tools, parsed directly from history tables. |
| POST | `/api/internal/v1/reports/aggregate` | metrics | One-shot aggregate report data: SQL aggregates plus detail `task-stats`, token/credit metrics, and coverage. |
| POST | `/api/internal/v1/reports/focus-tasks` | metrics | Focus task workbook rows for ended failed, user-complaint, high-duration, and high-token tasks with summary fields filled by the gateway. |
| POST | `/api/internal/v1/reports/daily` | metrics | Daily report data pack: aggregate report data, optional refs, and optional focus task rows when `include_focus_tasks=true`. It does not run AI. |
| POST | `/api/internal/v1/mining/cases` | metrics | Case candidates. |
| POST | `/api/internal/v1/exports` | export | Create `search_json`, `search_jsonl`, `search_csv`, or `context_zip`. |
| GET | `/api/internal/v1/exports/{export_id}` | export | Export metadata. |
| GET | `/api/internal/v1/exports/{export_id}/download` | export | Export file download. |
| POST | `/api/internal/v1/jobs` | metrics | Async `analytics.aggregate`, `analytics.facets`, or `analytics.task_stats`. |
| GET | `/api/internal/v1/jobs/{job_id}` | metrics | Job status. |
| GET | `/api/internal/v1/jobs/{job_id}/result` | metrics | Completed job result. |

## Batch Task APIs

`POST /api/internal/v1/history/questions/batch` returns `query-batch/v1`. It extends the single-task detail endpoint to multiple queries in one gateway call.

Typical refs request:

```json
{
  "env": "product",
  "items": [
    {"session_id": "...", "task_index": 1},
    {"session_id": "...", "task_index": 2}
  ],
  "profile": "qa",
  "max_items": 20,
  "max_concurrency": 8
}
```

Typical filter request:

```json
{
  "env": "product",
  "filters": {
    "start_time": "2026-05-19 15:00:00",
    "end_time": "2026-05-19 16:00:00",
    "status": "failed"
  },
  "profile": "summary",
  "max_items": 100,
  "include_search_item": true
}
```

Profiles:

- `summary`: compact task summary and evidence, same shape as the single task `profile=summary`.
- `qa`: full question/answer payload, useful for review tables and model judging.
- `context`: full `query-agent-tabs/v1` for each task.
- `full`: raw restored detail for each task.

`POST /api/internal/v1/history/questions/batch-context` is retained for existing clients. It uses the same implementation and response item shape, but keeps `schema_version=batch-context/v1` and defaults to `profile=context`.

## Report APIs

Report workflows are limited to 聚合统计、AI 分析、完整日报 plus the supporting 关注任务 conditional query. The gateway only optimizes pure data/stat/template collection. AI 分析 remains the local CLI workflow (`scripts/paiobs_ai_analysis.py analyze-batch --complete-search --progress`; 日报链路额外传 `--ended-only`) so operators can see per-task progress and keep the existing provider/fallback behavior.

`POST /api/internal/v1/reports/aggregate` returns `aggregate-report/v1` in one request. It runs SQL aggregate buckets and server-side detail `task-stats` concurrently, then returns:

- `aggregate`: `overall` plus requested breakdowns.
- `detail_stats`: detail dimensions such as `skill`, `tool_name`, `model`, `data_source_type`, `file_type`.
- `coverage`: whether detail scanning completed within `max_tasks`.
- `timings`: per-part runtime.

Typical request:

```json
{
  "env": "product",
  "filters": {
    "start_time": "2026-05-19 15:00:00",
    "end_time": "2026-05-19 15:10:00"
  },
  "breakdowns": ["status", "entry_scene", "scheduled", "is_web_search", "web_query_language", "user_role", "institution"],
  "detail_dimensions": ["skill", "tool_name", "model", "data_source_type", "file_type"],
  "include_token_stats": true,
  "include_credit_stats": false,
  "max_tasks": 1000000,
  "top_n": 10
}
```

`POST /api/internal/v1/reports/focus-tasks` returns `focus-tasks-report/v1`. It is the fast Gateway-side conditional query used by the daily report's 关注任务表格. It collects the window's ended tasks, loads task summaries in the gateway, fills the standard review fields, and returns four ready-to-write sheets:

- `失败任务`：ended tasks where `success=false` or status is failed/error/timeout/cancelled.
- `用户抱怨任务`：ended tasks whose query content matches complaint/dissatisfaction keywords.
- `高耗时任务`：ended non-failed tasks with `duration_seconds >= high_duration_seconds`，default `900`.
- `高token消耗任务`：ended non-failed tasks with `total_tokens >= high_token_threshold`，default `100000`.

Typical request:

```json
{
  "env": "product",
  "filters": {
    "start_time": "2026-05-18 00:00:00",
    "end_time": "2026-05-19 00:00:00"
  },
  "max_items": 1000000,
  "high_duration_seconds": 900,
  "high_token_threshold": 100000,
  "query_view_base_url": "http://192.168.15.57:30100",
  "search_workers": 8,
  "detail_workers": 8
}
```

`POST /api/internal/v1/reports/daily` returns `daily-report-data/v1`. It is a data pack for 完整日报: `aggregate_report` plus optional `analysis_refs` only when the client explicitly asks for refs. Pass `include_focus_tasks=true` to include the same `focus_tasks` payload from `/reports/focus-tasks`. The gateway does not call an LLM and does not expose a report-level AI batch endpoint.

Typical request:

```json
{
  "env": "product",
  "target_date": "2026-05-18",
  "include_refs": false,
  "include_focus_tasks": true,
  "max_items": 1000000,
  "search_workers": 8
}
```

The response includes `local_ai_analysis.gateway_runs_ai=false` and a `recommended_entry` pointing to the local AI CLI. Existing low-level `/analysis/query` and `/analysis/batch` endpoints remain available for explicit diagnostics and compatibility, but they are not the default report workflow.

## Analysis Request Notes

`POST /api/internal/v1/analysis/query` and `/analysis/batch` accept the legacy `focus` values (`general`, `failure`, `search_no_result`, `sql_dsl`, `cost_latency`, `files`) and return Markdown analysis by default.

For explicit server-side diagnostics, the gateway also accepts `custom_instruction` (aliases: `instruction`, `prompt`). When present, the LLM uses that instruction instead of the built-in Markdown report format while still receiving the compact `QueryAgentTabs` context server-side. This path is only used when the AI CLI is run with `--ai-provider gateway`; default report workflows keep AI analysis local:

```json
{
  "env": "product",
  "session_id": "...",
  "task_index": 1,
  "focus": "quality_json",
  "custom_instruction": "Return one JSON object with result_score/overall_score/...",
  "output_format": "json"
}
```

Without `custom_instruction`, old clients keep the original behavior.

## File Proxy Endpoints

The CLI also uses existing WebUI file proxy endpoints on the same base URL; these are not under `/api/internal/v1`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/files/preview` | Return text preview JSON for local or remote file paths. |
| GET | `/api/files/content` | Stream/download the full remote file. |

Typical query fields:

- `env`: environment, usually `product`.
- `file_path`: remote workspace path from `file_path`, `content_path`, or `raw_file_path`.
- `fallback_path`: repeatable fallback paths, useful when compact paths do not resolve.
- `target`: AlphaPai target URL; usually resolved from environment config.
- `owner_user_id`: historical file owner, useful for custom history file proxy.
- `file_id`: workspace file id if present in the data pack.
- `name`: display/download filename.

Use `paiobs files` to extract these fields from `summary/context/bundle`, then `paiobs file-preview` or `paiobs file-download` to read content.

## Skill Content Endpoint

`GET /api/internal/v1/skills/content` returns `skill-content/v1`.

Query fields:

- `skill_name` or `name`: required skill directory/name, for example `exa-search`.
- `user_id` / `owner_user_id`: optional PaiWork user id. When present, the gateway first reads that user's `custom-directory/skills-infos` view and then tries user/runtime paths such as `skills/user/<name>/SKILL.md`, `skills/<name>/SKILL.md`, `.system` paths, and candidates discovered from `skills-infos` through the same custom-file proxy used for historical files.
- `env`: environment, usually `product`.
- `max_chars`: optional response cap, default `40000`.
- `include_files`: optional boolean. When true, response includes `files[]`: the primary `SKILL.md` plus discovered sidecar files such as `scripts/*.py`, `README.md`, `requirements.txt`, and file paths referenced inside `SKILL.md`.
- `file_path`: optional repeatable extra file path to fetch under the same skill/workspace, useful when a process log exposed a concrete script path.

If the user workspace lookup misses, the gateway falls back to server-side configured roots from `PAI_OBS_SKILL_ROOTS`, `pai_automation/skills`, and `~/.codex/skills`. The historical failure mode where `skills-infos` matched a user skill but had an empty `source_path` is handled by the standard `skills/user/<name>/...` fallback candidates.

For full skill capture:

```bash
python3.11 scripts/paiobs.py skill-content exa-search \
  --user-id "8808..." \
  --include-files \
  --max-chars 0 \
  --format json \
  --output exa-search.skill.json
```

## Session / Query Relationship

`session_id` identifies a multi-turn session. Each user turn is a query/task under that session, identified by `task_index` and often `question_id`.

For non-first-turn investigation:

```bash
python3.11 scripts/paiobs.py session <session_id> --format table
python3.11 scripts/paiobs.py trace <session_id> <task_index> --format table
```

`trace` lists all queries in the same session up to the target `task_index`. Add `--with-context --context-profile summary` when the current turn depends on earlier turns.

## Search Filters

Supported filters:

- IDs: `session_id`, `question_id`, `user_id`.
- User/org: `username`, `institution`, `institution_nature`, `inst_type`, `product_type`, `user_type`, `user_role`.
- Task: `keyword`, `answer_keyword`, `entry_scene`, `status`, `scheduled`, `end_type`, `is_web_search`.
- Time: `start_time`, `end_time`.
- Theme search: `theme_rules` on `/history/questions/theme-search` or inside search filters. Rules support `include_any`, `include_all`, `include_all_any`, `include_regex_any`, `include_regex_all`, `exclude`, `exclude_regex`, `scope`, and `groups`.

`status` usually maps to values such as `success`, `failed`, `running`, or server-compatible historical values. Empty filters return recent questions, currently from the last 14 days.

Search request:

```json
{
  "env": "product",
  "filters": {
    "keyword": "没有按照",
    "answer_keyword": "无法访问",
    "status": "failed",
    "start_time": "2026-05-01 00:00:00"
  },
  "page": {"limit": 30},
  "profile": "lite"
}
```

Gateway-side theme search avoids collecting every query before local scanning. It applies SQL `LIKE`/`REGEXP` conditions in the gateway and returns only candidate question rows for later agent analysis:

```json
{
  "env": "product",
  "filters": {
    "start_time": "2026-05-19 00:00:00",
    "end_time": "2026-05-20 00:00:00"
  },
  "theme_rules": {
    "scope": "question",
    "groups": [
      {
        "include_regex_any": [
          "(数据源|来源|接入|抓取|采集|访问|查询).{0,24}[一-龥a-z0-9·&（）()_.-]{2,30}(资讯|信息网|数据平台|数据库|终端|统计局|交易所|化工|魔方)",
          "[一-龥a-z0-9·&（）()_.-]{2,30}(资讯|信息网|数据平台|数据库|终端|统计局|交易所|化工|魔方).{0,24}(数据源|来源|数据|网站|平台|接入|抓取|查询|上|里|中)"
        ]
      }
    ],
    "exclude": ["数据不对", "格式不对", "大模型", "标注"]
  },
  "max_items": 500,
  "profile": "lite"
}
```

`scope=question` only searches user question fields. Other scopes are `answer`, `feedback`, `question_feedback`, and `all`. `include_all_any` is an AND of OR groups, useful for expressions like `(request verb) AND (data object)`. Regex rules use the database `REGEXP` operator and are best kept to narrow debug windows; production theme scans should prefer keyword include/exclude prefilters when possible.

## Analytics

Aggregate `group_by` values:

`day`, `hour`, `entry_scene`, `status`, `scheduled`, `is_web_search`, `query_language`, `web_query_language`, `end_type`, `user`, `user_id`, `institution`, `institution_nature`, `inst_type`, `product_type`, `user_type`, `user_role`.

`query_language` uses gateway-side regex buckets: `english` means the query contains at least one English letter and only ASCII letters/digits/whitespace/punctuation; everything else is `other`. `web_query_language` applies the same regex only to web-search tasks and returns `english`, `other`, or `non_web_search`.

Aggregate metrics:

`question_count`, `session_count`, `user_count`, `active_user_count`, `failed_count`, `running_count`, `success_count`, `success_rate`, `avg_duration`, `p50_duration`, `p90_duration`, `p95_duration`, `avg_answer_length`.

报告层展示的成功率使用已结束任务口径：`success_count / (question_count - running_count)`。网关原始 `success_rate` 保留原样作为 API 字段，不直接作为 quickstat/daily Markdown 的展示值。

Facet dimensions use the same group values. The gateway delegates detail-derived dimensions to the server-side task stats path: `model`, `tool_type`, `tool_name`, `skill`, `data_source_type`, `source_provider`, `source_domain`, `source_title`, `source_id`, `file_type`, `file_role`.

For full task-window statistics from the CLI, use `paiobs_task_stats.py overview` for standard fields and `paiobs_task_stats.py task-stats` only for detail-derived dimensions. Standard fields such as day/hour/status/entry_scene/user/institution/user_role run through SQL aggregate paths and should not scan task JSON. Detail-derived dimensions such as tool/model/skill/source/file read `history_main/history_task` and parse only the JSON columns required by the requested dimensions inside the gateway process. This is the required path for high-volume tool/model/skill/source/file counts; the skill no longer performs local recursive search/context scans for statistics. The response emits `task-stats/v1` with `task_count` and `occurrence_count`.

Source/file dimensions are intentionally split:

- `data_source_type`: source content category, e.g. `web`, `report`, `comment`, `edb`, `ann`, `roadshow`, `social_media`.
- `source_provider`: source platform/channel/publisher, e.g. 今日头条、新浪财经、东方财富网.
- `source_domain`: parsed URL host.
- `file_type`: file/material type from mentioned/current/generated/changed/subtask files, e.g. `md`, `xlsx`, `docx`, `png`, `generated_report`, or material labels such as `report` and `web`.
- `file_role`: where the file appeared in the task, e.g. `current_file`, `mentioned_file`, `file_change`, `subtask_file`.

For token statistics, send `include_token_stats=true` to `/analytics/task-stats`. The response adds top-level `token_metrics` and token fields on each bucket when usage is available: `token_task_count`, `total_tokens`, `prompt_tokens`, `completion_tokens`, `cached_tokens`, `llm_call_count`, `avg_total_tokens`, `p50_total_tokens`, `p90_total_tokens`, `p95_total_tokens`, and `avg_llm_calls`. CLI users should normally call `paiobs_task_stats.py token-stats`, which renders these metrics in a separate Token section.

For credit / 研究值 statistics, send `include_credit_stats=true` to `/analytics/task-stats`. The gateway joins `saas.saas_point_freeze_order` by `business_no = question_id OR business_no = feedback_question_id`; `consumed_points` is exposed as `total_credits` / `consumed_points`, and `frozen_points`, `order_count`, `confirmed_order_count`, `avg_credits_per_task`, `p50_credits`, `p90_credits`, `p95_credits` are returned overall and on each bucket when available. CLI users should normally call `paiobs_task_stats.py credit-stats`.

`task-stats` default scan limit is 100000 tasks and the gateway hard limit is 1000000 tasks. These can be overridden with `PAI_OBS_TASK_STATS_DEFAULT_MAX_TASKS` and `PAI_OBS_TASK_STATS_HARD_MAX_TASKS`. For month-level detail statistics, first try a high `--max-tasks`; if the window still exceeds the hard limit, split by day/week and merge the returned buckets.

For user-facing summaries, prefer CLI-side Markdown rendering instead of exporting JSON and hand-writing `jq` reductions. Use `overview --display-top-n` for standard aggregate fields; add `--json-output` when the same run should also preserve the complete long-tail JSON:

```bash
python3.11 scripts/paiobs_task_stats.py overview \
  --start-time "2026-05-14 15:38:58" \
  --end-time "2026-05-14 16:38:58" \
  --breakdowns status,entry_scene,scheduled,is_web_search,user_role,institution \
  --display-top-n 10 \
  --json-output /tmp/paiobs_stats_overview.json \
  --format markdown \
  --output /tmp/paiobs_stats_overview.md
```

For detail-derived dimensions, use `task-stats`:

```bash
python3.11 scripts/paiobs_task_stats.py task-stats \
  --start-time "2026-05-14 15:38:58" \
  --end-time "2026-05-14 16:38:58" \
  --dimensions skill,tool_name,model,data_source_type,source_provider,file_type \
  --top-n 20 \
  --max-tasks 100000 \
  --format markdown \
  --output /tmp/paiobs_task_stats_report.md
```

For token statistics:

```bash
python3.11 scripts/paiobs_task_stats.py token-stats \
  --start-time "2026-05-14 15:38:58" \
  --end-time "2026-05-14 16:38:58" \
  --group-by status,entry_scene,user_role \
  --token-dimensions model \
  --max-tasks 100000 \
  --format markdown
```

The Markdown renderer computes per-dimension Top buckets, task share, occurrence share, failed buckets, coverage, and a snapshot of the current request. `aggregate`, `facets`, `duration-stats`, `token-stats`, `overview`, and `compare` also live in `scripts/paiobs_task_stats.py`; `compare` is opt-in and should only be used when the user asks for a previous-period comparison.

## Case Mining

Kinds:

- `badcase`: failures, user corrections, low-quality signals.
- `goodcase`: praise or useful-result signals.
- `complaint`: strong dissatisfaction.
- `new_requirement`: feature/data/source/automation requests.
- `inability`: AI says it cannot do something.
- `cost_outlier`: slow or expensive tasks.
- `tool_failure`: SQL/ES/search/file/tool failures.

Mining request:

```json
{
  "env": "product",
  "kind": "badcase",
  "filters": {"start_time": "2026-05-01 00:00:00"},
  "rules": {
    "include_keywords": ["不对", "没有按照", "格式不对"],
    "exclude_keywords": ["我搞错了"]
  },
  "sample": {"limit": 50},
  "with_context": false
}
```

Treat mining output as candidates. Verify with task summary/context before making product or engineering claims.
