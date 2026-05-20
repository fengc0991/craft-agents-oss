# PaiWork {{time_window_label}}任务统计报告

- 统计窗口：{{start_time}} ~ {{end_time}} {{timezone}}
- 数据环境：{{env}}
- 统计口径：Observability Gateway 聚合/统计接口；总量以 `overview` 为准，skill/tool/model/数据源/文件类型等明细 TopN 基于 `task-stats` 扫描结果；联网 query 语言由网关聚合层正则分桶
- 生成时间：{{generated_at}} {{timezone}}

## 1. 总览

| 指标 | 数值 |
| --- | ---: |
| query 数 | {{query_count}} |
| session 数 | {{session_count}} |
| 用户数 | {{user_count}} |
| 成功 | {{success_count}} |
| 失败 | {{failed_count}} |
| 运行中 | {{running_count}} |
| 已结束任务数 | {{ended_count}} |
| 已结束任务成功率 | {{ended_success_rate}} |

说明：{{overview_note}}

## 2. 耗时总计

| 指标 | 数值 |
| --- | ---: |
| 平均耗时 | {{avg_duration_seconds}} 秒 |
| P50 耗时 | {{p50_duration_seconds}} 秒 |
| P90 耗时 | {{p90_duration_seconds}} 秒 |
| P95 耗时 | {{p95_duration_seconds}} 秒 |

## 3. Token 总计

| 指标 | 数值 |
| --- | ---: |
| 有 Token 任务数 | {{token_task_count}} |
| Token 总量 | {{total_tokens}} |
| 输入 Token | {{input_tokens}} |
| 输出 Token | {{output_tokens}} |
| 缓存 Token | {{cached_tokens}} |
| LLM 调用数 | {{llm_call_count}} |
| 平均 Token/任务 | {{avg_total_tokens}} |
| P50 Token/任务 | {{p50_total_tokens}} |
| P90 Token/任务 | {{p90_total_tokens}} |
| P95 Token/任务 | {{p95_total_tokens}} |
| 平均 LLM 调用/任务 | {{avg_llm_calls}} |
| 平均 Token/调用 | {{avg_tokens_per_call}} |

## 4. 状态分布

| 状态 | query 数 | 成功 | 失败 | 运行中 | 已结束成功率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| {{status}} | {{query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |

## 5. 入口场景

| 入口 | query 数 | 成功 | 失败 | 运行中 | 已结束成功率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| {{entry_scene}} | {{query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |

## 6. 调度与联网

| 维度 | query 数 | 成功 | 失败 | 运行中 | 已结束成功率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| {{scheduled_or_search_value}} | {{query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |

### 联网 query 语言

| 维度 | query 数 | 成功 | 失败 | 运行中 | 已结束成功率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 联网英文 query | {{english_query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |
| 联网其他 query | {{other_query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |

说明：联网 query 语言由网关聚合层正则判断；`english` 要求 query 至少包含英文字母，且全文只包含 ASCII 字母、数字、空白和符号，其余联网 query 归为 `other`。

## 7. 用户画像

| 用户角色 | query 数 | 成功 | 失败 | 运行中 | 已结束成功率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| {{user_role}} | {{query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |

| 用户类型 | query 数 | 成功 | 失败 | 运行中 | 已结束成功率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| {{user_type}} | {{query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |

## 8. 产品类型

| 产品类型 | query 数 | 成功 | 失败 | 运行中 | 已结束成功率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| {{product_type}} | {{query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |

## 9. 机构 Top10

| 机构 | query 数 | 成功 | 失败 | 运行中 | 已结束成功率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| {{institution}} | {{query_count}} | {{success_count}} | {{failed_count}} | {{running_count}} | {{ended_success_rate}} |

## 10. 明细 TopN

task-stats 本次扫描任务数 {{scanned_task_count}}；明细维度 TopN 用于观察 skill/tool/model/数据源/文件类型，不作为总 query 数口径。

### Skill Top10

| skill | 任务数 | 引用次数 | 成功 | 失败 |
| --- | ---: | ---: | ---: | ---: |
| {{skill}} | {{task_count}} | {{ref_count}} | {{success_count}} | {{failed_count}} |

### 工具 Top10

| 工具 | 任务数 | 引用次数 | 成功 | 失败 |
| --- | ---: | ---: | ---: | ---: |
| {{tool_name}} | {{task_count}} | {{ref_count}} | {{success_count}} | {{failed_count}} |

### 模型 Top10

| 模型 | 任务数 | 引用次数 | 成功 | 失败 |
| --- | ---: | ---: | ---: | ---: |
| {{model}} | {{task_count}} | {{ref_count}} | {{success_count}} | {{failed_count}} |

### 数据源类型 Top10

| 数据源类型 | 任务数 | 引用次数 |
| --- | ---: | ---: |
| {{data_source_type}} | {{task_count}} | {{ref_count}} |

### 文件类型 Top10

| 文件类型 | 任务数 | 引用次数 | 成功 | 失败 |
| --- | ---: | ---: | ---: | ---: |
| {{file_type}} | {{task_count}} | {{ref_count}} | {{success_count}} | {{failed_count}} |

## 11. 简要结论

1. {{conclusion_1}}
2. {{conclusion_2}}
3. {{conclusion_3}}
4. {{conclusion_4}}
5. {{conclusion_5}}
