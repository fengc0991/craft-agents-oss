#!/usr/bin/env python3.11
"""Fast final-format PaiWork task statistics report.

This script is the short path for "过去 1h/今天/最近 N 天任务统计":

1. Run SQL aggregate breakdowns and detail task-stats in parallel.
2. Ask detail task-stats to include overall token_metrics so token totals do
   not need a separate gateway request in the common case.
3. Render the fixed 11-section report used by this skill.
4. Optionally create a Feishu document from the generated Markdown.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import paiobs
import paiobs_lark
import paiobs_task_stats


DEFAULT_BREAKDOWNS = [
    "status",
    "entry_scene",
    "scheduled",
    "is_web_search",
    "web_query_language",
    "end_type",
    "user_role",
    "user_type",
    "institution",
    "product_type",
]
DEFAULT_DETAIL_DIMENSIONS = ["skill", "tool_name", "model", "data_source_type", "file_type"]
DEFAULT_METRICS = paiobs_task_stats.DEFAULT_AGGREGATE_METRICS
DEFAULT_OUTPUT_DIR = "/tmp/paiobs_stats_reports"
TIMEZONE_LABEL = "Asia/Shanghai"
WEB_QUERY_LANGUAGE_LABELS = {
    "english": "联网英文 query",
    "other": "联网其他 query",
}


def parse_csv(value: str, default: list[str]) -> list[str]:
    parsed = paiobs_task_stats.parse_csv_arg(value, default, empty_means_default=True)
    return parsed or list(default)


def dt_slug(value: str) -> str:
    return str(value).replace("-", "").replace(":", "").replace(" ", "_")


def ensure_time_window(args: argparse.Namespace) -> None:
    if args.start_time and args.end_time:
        return
    if args.start_time or args.end_time:
        raise SystemExit("--start-time and --end-time must be provided together")
    if args.last_minutes and args.last_minutes > 0:
        delta = timedelta(minutes=args.last_minutes)
    else:
        delta = timedelta(hours=args.last_hours)
    end = datetime.now()
    start = end - delta
    args.start_time = start.strftime("%Y-%m-%d %H:%M:%S")
    args.end_time = end.strftime("%Y-%m-%d %H:%M:%S")


def default_stem(args: argparse.Namespace) -> str:
    return f"paiwork_stats_{dt_slug(args.start_time)}_to_{dt_slug(args.end_time)}"


def resolve_outputs(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    output_dir = Path(args.output_dir or DEFAULT_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = default_stem(args)
    report_md = Path(args.report_md) if args.report_md else output_dir / f"{stem}.md"
    raw_json = Path(args.json_output) if args.json_output else output_dir / f"{stem}.json"
    publish_json = Path(args.publish_output) if args.publish_output else output_dir / f"{stem}_lark_publish.json"
    report_md.parent.mkdir(parents=True, exist_ok=True)
    raw_json.parent.mkdir(parents=True, exist_ok=True)
    publish_json.parent.mkdir(parents=True, exist_ok=True)
    return report_md, raw_json, publish_json


def timed_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, float]:
    started = time.monotonic()
    return fn(*args, **kwargs), round(time.monotonic() - started, 3)


def task_stats_args(args: argparse.Namespace, dimensions: list[str]) -> argparse.Namespace:
    return argparse.Namespace(
        **{
            **vars(args),
            "dimensions": ",".join(dimensions),
            "top_n": args.detail_top_n,
            "max_tasks": args.max_tasks,
            "include_sample_refs": False,
            "sample_ref_limit": 0,
            "include_token_stats": True,
        }
    )


def token_fallback_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        **{
            **vars(args),
            "group_by": "none",
            "token_dimensions": "none",
            "top_n": args.detail_top_n,
            "max_tasks": args.max_tasks,
        }
    )


def request_gateway_aggregate_report(
    client: paiobs.PaiObsClient,
    *,
    filters: dict[str, Any],
    breakdowns: list[str],
    detail_dimensions: list[str],
    metrics: list[str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    return client.request(
        "POST",
        "/reports/aggregate",
        body={
            "env": client.env,
            "filters": filters,
            "breakdowns": breakdowns,
            "detail_dimensions": detail_dimensions,
            "metrics": metrics,
            "limit": args.limit,
            "top_n": args.detail_top_n,
            "max_tasks": args.max_tasks,
            "include_token_stats": True,
            "include_credit_stats": False,
        },
    )


def build_report_payload(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    filters = paiobs.build_filters(args)
    breakdowns = parse_csv(args.breakdowns, DEFAULT_BREAKDOWNS)
    detail_dimensions = parse_csv(args.detail_dimensions, DEFAULT_DETAIL_DIMENSIONS)
    metrics = parse_csv(args.metrics, DEFAULT_METRICS)

    started = time.monotonic()
    try:
        gateway_report, gateway_seconds = timed_call(
            request_gateway_aggregate_report,
            client,
            filters=filters,
            breakdowns=breakdowns,
            detail_dimensions=detail_dimensions,
            metrics=metrics,
            args=args,
        )
        if isinstance(gateway_report, dict) and gateway_report.get("schema_version") == "aggregate-report/v1":
            timings = dict(gateway_report.get("timings") or {})
            timings["gateway_report_seconds"] = gateway_seconds
            timings["total_seconds"] = round(time.monotonic() - started, 3)
            return {
                "schema_version": "quick-stats-report/v1",
                "generated_at": gateway_report.get("generated_at") or datetime.now().isoformat(timespec="seconds"),
                "env": gateway_report.get("env") or client.env,
                "timezone": TIMEZONE_LABEL,
                "filters": gateway_report.get("filters") or filters,
                "breakdowns": gateway_report.get("breakdowns") or breakdowns,
                "detail_dimensions": gateway_report.get("detail_dimensions") or detail_dimensions,
                "aggregate": {
                    key: paiobs.normalize_analytics_payload(value)
                    for key, value in (gateway_report.get("aggregate") or {}).items()
                },
                "detail_stats": paiobs.normalize_analytics_payload(gateway_report.get("detail_stats") or {}),
                "token_stats": None,
                "coverage": gateway_report.get("coverage") or {},
                "timings": timings,
                "display_top_n": args.display_top_n,
                "detail_top_n": args.detail_top_n,
                "gateway_report_schema": gateway_report.get("schema_version"),
            }
    except paiobs.ApiError as exc:
        if exc.status not in {404, 405}:
            raise

    timings: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        aggregate_future = executor.submit(
            timed_call,
            paiobs_task_stats.request_aggregate_set,
            client,
            filters,
            breakdowns,
            metrics,
            args.limit,
        )
        detail_future = executor.submit(
            timed_call,
            paiobs_task_stats.request_task_stats,
            client,
            task_stats_args(args, detail_dimensions),
        )
        aggregate_payloads, timings["aggregate_seconds"] = aggregate_future.result()
        detail_stats, timings["detail_seconds"] = detail_future.result()

    normalized_aggregate_payloads = {
        key: paiobs.normalize_analytics_payload(value)
        for key, value in (aggregate_payloads or {}).items()
    }

    token_stats = None
    if args.token_fallback and not isinstance((detail_stats or {}).get("token_metrics"), dict):
        token_stats, timings["token_fallback_seconds"] = timed_call(
            paiobs_task_stats.build_token_stats,
            client,
            token_fallback_args(args),
        )

    timings["total_seconds"] = round(time.monotonic() - started, 3)
    return {
        "schema_version": "quick-stats-report/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": client.env,
        "timezone": TIMEZONE_LABEL,
        "filters": filters,
        "breakdowns": breakdowns,
        "detail_dimensions": detail_dimensions,
        "aggregate": normalized_aggregate_payloads,
        "detail_stats": paiobs.normalize_analytics_payload(detail_stats),
        "token_stats": paiobs.normalize_analytics_payload(token_stats) if token_stats else None,
        "timings": timings,
        "display_top_n": args.display_top_n,
        "detail_top_n": args.detail_top_n,
    }


def metric_items(payload: dict[str, Any], dimension: str) -> list[dict[str, Any]]:
    items = ((payload.get("aggregate") or {}).get(dimension) or {}).get("items") or []
    return [item for item in items if isinstance(item, dict)]


def first_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    overall = metric_items(payload, "overall")
    if not overall:
        return {}
    metrics = overall[0].get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def group_value(item: dict[str, Any], dimension: str) -> str:
    group = item.get("group") if isinstance(item.get("group"), dict) else {}
    value = group.get(dimension)
    if value not in (None, ""):
        return str(paiobs.normalize_dimension_value(dimension, value))
    return str(paiobs.normalize_dimension_value(dimension, paiobs.aggregate_group_value(item)))


def safe_int(value: Any) -> int:
    number = paiobs.safe_number(value)
    return int(number) if number is not None else 0


def fmt_int(value: Any) -> str:
    number = paiobs.safe_number(value)
    return f"{int(number):,}" if number is not None else ""


def fmt_num(value: Any, digits: int = 1) -> str:
    number = paiobs.safe_number(value)
    if number is None:
        return ""
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.{digits}f}"


def fmt_rate(value: Any) -> str:
    number = paiobs.safe_number(value)
    return f"{number * 100:.2f}%" if number is not None else ""


def ended_count(metrics: dict[str, Any]) -> int:
    total = paiobs.safe_number(metrics.get("question_count"))
    running = paiobs.safe_number(metrics.get("running_count"))
    if total is not None and running is not None:
        return max(0, int(total) - int(running))
    return safe_int(metrics.get("success_count")) + safe_int(metrics.get("failed_count"))


def ended_success_rate(metrics: dict[str, Any]) -> float | None:
    ended = ended_count(metrics)
    if ended <= 0:
        return None
    return safe_int(metrics.get("success_count")) / ended


def fmt_ended_success_rate(metrics: dict[str, Any]) -> str:
    return fmt_rate(ended_success_rate(metrics))


def metric_row(label: str, metrics: dict[str, Any]) -> list[str]:
    return [
        label,
        fmt_int(metrics.get("question_count")),
        fmt_int(metrics.get("success_count")),
        fmt_int(metrics.get("failed_count")),
        fmt_int(metrics.get("running_count")),
        fmt_ended_success_rate(metrics),
    ]


def breakdown_rows(payload: dict[str, Any], dimension: str, *, prefix: str = "", limit: int | None = None) -> list[list[str]]:
    rows = []
    for item in metric_items(payload, dimension)[: limit or 10_000]:
        label = group_value(item, dimension)
        if prefix:
            label = f"{prefix}: {label}"
        rows.append(metric_row(label, item.get("metrics") or {}))
    return rows or [["无", "0", "0", "0", "0", ""]]


def token_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    detail = payload.get("detail_stats") if isinstance(payload.get("detail_stats"), dict) else {}
    metrics = detail.get("token_metrics") if isinstance(detail.get("token_metrics"), dict) else {}
    if metrics:
        return metrics
    fallback = payload.get("token_stats") if isinstance(payload.get("token_stats"), dict) else {}
    task_stats = fallback.get("task_stats") if isinstance(fallback.get("task_stats"), dict) else {}
    return task_stats.get("token_metrics") if isinstance(task_stats.get("token_metrics"), dict) else {}


def avg_tokens_per_call(metrics: dict[str, Any]) -> float | None:
    total = paiobs.safe_number(metrics.get("total_tokens"))
    calls = paiobs.safe_number(metrics.get("llm_call_count"))
    if total is None or not calls:
        return None
    return total / calls


def web_query_language_rows(payload: dict[str, Any]) -> list[list[str]]:
    aggregate = (payload.get("aggregate") or {}).get("web_query_language") or {}
    warnings = aggregate.get("warnings") if isinstance(aggregate.get("warnings"), dict) else {}
    unsupported = warnings.get("unsupported_group_by") or []
    if "web_query_language" in unsupported:
        return [[WEB_QUERY_LANGUAGE_LABELS[value], "未返回", "", "", "", ""] for value in ("english", "other")]
    by_value = {
        group_value(item, "web_query_language").lower(): item.get("metrics") or {}
        for item in metric_items(payload, "web_query_language")
    }
    empty_metrics = {"question_count": 0, "success_count": 0, "failed_count": 0, "running_count": 0}
    return [
        metric_row(WEB_QUERY_LANGUAGE_LABELS[value], by_value.get(value) or empty_metrics)
        for value in ("english", "other")
    ]


def web_query_language_note(payload: dict[str, Any]) -> str:
    aggregate = (payload.get("aggregate") or {}).get("web_query_language") or {}
    warnings = aggregate.get("warnings") if isinstance(aggregate.get("warnings"), dict) else {}
    unsupported = warnings.get("unsupported_group_by") or []
    if "web_query_language" in unsupported:
        return "说明：当前网关尚未支持 `web_query_language` 聚合维度，升级网关后会显示联网英文/其他 query 细分。"
    return "说明：联网 query 语言由网关聚合层正则判断；`english` 要求 query 至少包含英文字母，且全文只包含 ASCII 字母、数字、空白和符号，其余联网 query 归为 `other`。"


def detail_rows(payload: dict[str, Any], dimension: str, *, include_success: bool = True) -> list[list[str]]:
    detail = payload.get("detail_stats") if isinstance(payload.get("detail_stats"), dict) else {}
    facets = detail.get("facets") if isinstance(detail.get("facets"), dict) else {}
    buckets = facets.get(dimension) if isinstance(facets.get(dimension), list) else []
    rows = []
    for bucket in buckets[: int(payload.get("detail_top_n") or 10)]:
        if not isinstance(bucket, dict):
            continue
        row = [
            str(bucket.get("value") or ""),
            fmt_int(bucket.get("task_count")),
            fmt_int(bucket.get("occurrence_count")),
        ]
        if include_success:
            row.extend([fmt_int(bucket.get("success_count")), fmt_int(bucket.get("failed_count"))])
        rows.append(row)
    if rows:
        return rows
    return [["无", "0", "0", "0", "0"]] if include_success else [["无", "0", "0"]]


def low_success_label(payload: dict[str, Any], dimension: str, *, limit: int | None = None) -> str:
    candidates = []
    for item in metric_items(payload, dimension)[: limit or 10_000]:
        metrics = item.get("metrics") or {}
        ended = ended_count(metrics)
        if ended <= 0:
            continue
        rate = ended_success_rate(metrics)
        if rate is None:
            continue
        candidates.append((rate, -ended, group_value(item, dimension)))
    if not candidates:
        return "无"
    rate, negative_count, label = sorted(candidates)[0]
    return f"{label}（已结束 {-negative_count} queries，成功率 {rate * 100:.2f}%）"


def render_report(payload: dict[str, Any]) -> str:
    filters = payload.get("filters") or {}
    overall = first_metrics(payload)
    token = token_metrics(payload)
    token_avg = avg_tokens_per_call(token)
    top_n = int(payload.get("display_top_n") or 10)
    detail = payload.get("detail_stats") if isinstance(payload.get("detail_stats"), dict) else {}

    manual = next((item.get("metrics") or {} for item in metric_items(payload, "scheduled") if group_value(item, "scheduled") == "manual"), {})
    scheduled = next((item.get("metrics") or {} for item in metric_items(payload, "scheduled") if group_value(item, "scheduled") == "scheduled"), {})
    web_true = next((item.get("metrics") or {} for item in metric_items(payload, "is_web_search") if group_value(item, "is_web_search").lower() == "true"), {})

    lines = [
        "# PaiWork 任务统计报告",
        "",
        f"- 统计窗口：{filters.get('start_time', '')} ~ {filters.get('end_time', '')} {payload.get('timezone') or TIMEZONE_LABEL}",
        f"- 数据环境：{payload.get('env', '')}",
        "- 统计口径：Observability Gateway 聚合/统计接口；总量以 `overview` 为准，skill/tool/model/数据源/文件类型等明细 TopN 基于 `task-stats` 扫描结果；联网 query 语言由网关聚合层正则分桶",
        f"- 生成时间：{payload.get('generated_at', '')} {payload.get('timezone') or TIMEZONE_LABEL}",
        f"- 快捷脚本耗时：{fmt_num((payload.get('timings') or {}).get('total_seconds'))} 秒",
        "",
        "## 1. 总览",
        "",
        paiobs.markdown_table(
            [
                ["query 数", fmt_int(overall.get("question_count"))],
                ["session 数", fmt_int(overall.get("session_count"))],
                ["用户数", fmt_int(overall.get("user_count"))],
                ["成功", fmt_int(overall.get("success_count"))],
                ["失败", fmt_int(overall.get("failed_count"))],
                ["运行中", fmt_int(overall.get("running_count"))],
                ["已结束任务数", fmt_int(ended_count(overall))],
                ["已结束任务成功率", fmt_ended_success_rate(overall)],
            ],
            ["指标", "数值"],
        ),
        "",
        f"说明：本窗口共 {fmt_int(overall.get('question_count'))} 个 query，失败 {fmt_int(overall.get('failed_count'))}，running {fmt_int(overall.get('running_count'))}，已结束 {fmt_int(ended_count(overall))}。",
        "",
        "## 2. 耗时总计",
        "",
        paiobs.markdown_table(
            [
                ["平均耗时", f"{fmt_num(overall.get('avg_duration'))} 秒"],
                ["P50 耗时", f"{fmt_num(overall.get('p50_duration'))} 秒"],
                ["P90 耗时", f"{fmt_num(overall.get('p90_duration'))} 秒"],
                ["P95 耗时", f"{fmt_num(overall.get('p95_duration'))} 秒"],
            ],
            ["指标", "数值"],
        ),
        "",
        "## 3. Token 总计",
        "",
        paiobs.markdown_table(
            [
                ["有 Token 任务数", fmt_int(token.get("token_task_count"))],
                ["Token 总量", fmt_int(token.get("total_tokens"))],
                ["输入 Token", fmt_int(token.get("prompt_tokens"))],
                ["输出 Token", fmt_int(token.get("completion_tokens"))],
                ["缓存 Token", fmt_int(token.get("cached_tokens"))],
                ["LLM 调用数", fmt_int(token.get("llm_call_count"))],
                ["平均 Token/任务", fmt_num(token.get("avg_total_tokens"))],
                ["P50 Token/任务", fmt_num(token.get("p50_total_tokens"))],
                ["P90 Token/任务", fmt_num(token.get("p90_total_tokens"))],
                ["P95 Token/任务", fmt_num(token.get("p95_total_tokens"))],
                ["平均 LLM 调用/任务", fmt_num(token.get("avg_llm_calls"))],
                ["平均 Token/调用", fmt_num(token_avg)],
            ],
            ["指标", "数值"],
        ),
        "",
        "## 4. 状态分布",
        "",
        paiobs.markdown_table(breakdown_rows(payload, "status", limit=top_n), ["状态", "query 数", "成功", "失败", "运行中", "已结束成功率"]),
        "",
        "## 5. 入口场景",
        "",
        paiobs.markdown_table(breakdown_rows(payload, "entry_scene", limit=top_n), ["入口", "query 数", "成功", "失败", "运行中", "已结束成功率"]),
        "",
        "## 6. 调度与联网",
        "",
        paiobs.markdown_table(
            [
                *breakdown_rows(payload, "scheduled", prefix="调度", limit=top_n),
                *breakdown_rows(payload, "is_web_search", prefix="联网", limit=top_n),
            ],
            ["维度", "query 数", "成功", "失败", "运行中", "已结束成功率"],
        ),
        "",
        "### 联网 query 语言",
        "",
        paiobs.markdown_table(web_query_language_rows(payload), ["维度", "query 数", "成功", "失败", "运行中", "已结束成功率"]),
        "",
        web_query_language_note(payload),
        "",
        "## 7. 用户画像",
        "",
        paiobs.markdown_table(breakdown_rows(payload, "user_role", limit=top_n), ["用户角色", "query 数", "成功", "失败", "运行中", "已结束成功率"]),
        "",
        paiobs.markdown_table(breakdown_rows(payload, "user_type", limit=top_n), ["用户类型", "query 数", "成功", "失败", "运行中", "已结束成功率"]),
        "",
        "## 8. 产品类型",
        "",
        paiobs.markdown_table(breakdown_rows(payload, "product_type", limit=top_n), ["产品类型", "query 数", "成功", "失败", "运行中", "已结束成功率"]),
        "",
        "## 9. 机构 Top10",
        "",
        paiobs.markdown_table(breakdown_rows(payload, "institution", limit=10), ["机构", "query 数", "成功", "失败", "运行中", "已结束成功率"]),
        "",
        "## 10. 明细 TopN",
        "",
        f"task-stats 本次扫描任务数 {fmt_int(detail.get('scanned_task_count'))}；明细维度 TopN 用于观察 skill/tool/model/数据源/文件类型，不作为总 query 数口径。",
        "",
        "### Skill Top10",
        "",
        paiobs.markdown_table(detail_rows(payload, "skill"), ["skill", "任务数", "引用次数", "成功", "失败"]),
        "",
        "### 工具 Top10",
        "",
        paiobs.markdown_table(detail_rows(payload, "tool_name"), ["工具", "任务数", "引用次数", "成功", "失败"]),
        "",
        "### 模型 Top10",
        "",
        paiobs.markdown_table(detail_rows(payload, "model"), ["模型", "任务数", "引用次数", "成功", "失败"]),
        "",
        "### 数据源类型 Top10",
        "",
        paiobs.markdown_table(detail_rows(payload, "data_source_type", include_success=False), ["数据源类型", "任务数", "引用次数"]),
        "",
        "### 文件类型 Top10",
        "",
        paiobs.markdown_table(detail_rows(payload, "file_type"), ["文件类型", "任务数", "引用次数", "成功", "失败"]),
        "",
        "## 11. 简要结论",
        "",
        f"1. 本窗口共有 {fmt_int(overall.get('question_count'))} 个 query、{fmt_int(overall.get('session_count'))} 个 session、{fmt_int(overall.get('user_count'))} 个用户，已结束任务成功率 {fmt_ended_success_rate(overall)}。",
        f"2. 失败数为 {fmt_int(overall.get('failed_count'))}；当前 running 为 {fmt_int(overall.get('running_count'))}，已结束任务数为 {fmt_int(ended_count(overall))}，running 任务不纳入成功率分母。",
        f"3. 手动任务 {fmt_int(manual.get('question_count'))} 个，已结束成功率 {fmt_ended_success_rate(manual)}；定时任务 {fmt_int(scheduled.get('question_count'))} 个，已结束成功率 {fmt_ended_success_rate(scheduled)}。入口中低成功率项为 {low_success_label(payload, 'entry_scene', limit=top_n)}。",
        f"4. 联网任务 {fmt_int(web_true.get('question_count'))} 个，已结束成功率 {fmt_ended_success_rate(web_true)}；联网英文/其他 query 细分见第 6 章；用户角色低成功率项为 {low_success_label(payload, 'user_role', limit=top_n)}，机构 Top10 中低成功率项为 {low_success_label(payload, 'institution', limit=10)}。",
        f"5. 整体耗时平均 {fmt_num(overall.get('avg_duration'))} 秒，P50/P90/P95 分别为 {fmt_num(overall.get('p50_duration'))}/{fmt_num(overall.get('p90_duration'))}/{fmt_num(overall.get('p95_duration'))} 秒；Token 总量 {fmt_int(token.get('total_tokens'))}，任务级 Token P50/P90/P95 分别为 {fmt_num(token.get('p50_total_tokens'))}/{fmt_num(token.get('p90_total_tokens'))}/{fmt_num(token.get('p95_total_tokens'))}，本报告未做上一周期对比，不能单独判定异常。",
    ]
    return "\n".join(lines).rstrip() + "\n"


def publish_to_lark(args: argparse.Namespace, report_md: Path, publish_json: Path) -> dict[str, Any]:
    if args.lark_profile:
        os.environ["PAI_OBS_LARK_PROFILE"] = args.lark_profile
    doc_path = args.doc_path or f"paiwork_reports/{report_md.stem}.md"
    doc = paiobs_lark.create_doc(str(report_md), doc_path, dry_run=args.dry_run)
    doc_url = str(doc.get("url") or doc.get("remote_url") or "")
    message = {}
    recipient_user_id = args.recipient_user_id
    if args.send:
        if not recipient_user_id:
            recipient_user_id = f"query:{args.recipient}" if args.dry_run else paiobs_lark.resolve_user_id(args.recipient)
        text = args.message or "\n".join(part for part in ["PaiWork 快捷任务统计报告已生成。", f"报告：{doc_url}" if doc_url else ""] if part)
        message = paiobs_lark.send_message(recipient_user_id, text, dry_run=args.dry_run, send_as=args.send_as)
    payload = {
        "schema_version": "paiobs-quick-stats-lark-publish/v1",
        "report_md": str(report_md),
        "doc_path": doc_path,
        "doc": doc,
        "send": bool(args.send),
        "recipient": args.recipient,
        "recipient_user_id": recipient_user_id,
        "message": message,
        "send_as": args.send_as,
    }
    publish_json.write_text(paiobs.json_dumps(payload) + "\n", encoding="utf-8")
    return payload


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--gateway-profile", default=None, help="Gateway profile: release/prod -> 30100, local/test/debug -> localhost:6193")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env", default=None)
    parser.add_argument("--timeout", default=None)
    parser.add_argument("--file-auth-token", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fast final-format PaiWork task statistics report")
    add_runtime_args(parser)
    paiobs.add_filter_args(parser)
    parser.add_argument("--last-hours", type=float, default=1.0, help="Default time window when start/end are omitted.")
    parser.add_argument("--last-minutes", type=float, default=0.0, help="Alternative relative window; overrides --last-hours when positive.")
    parser.add_argument("--breakdowns", default=",".join(DEFAULT_BREAKDOWNS))
    parser.add_argument("--detail-dimensions", default=",".join(DEFAULT_DETAIL_DIMENSIONS))
    parser.add_argument("--metrics", default=",".join(DEFAULT_METRICS))
    parser.add_argument("--display-top-n", type=int, default=10)
    parser.add_argument("--detail-top-n", type=int, default=10)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--max-tasks", type=int, default=paiobs_task_stats.DEFAULT_MAX_TASKS)
    parser.add_argument("--token-fallback", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report-md", default="", help="Markdown report output path. Defaults to output-dir/time-window name.")
    parser.add_argument("--json-output", default="", help="Raw quick report JSON output path. Defaults to output-dir/time-window name.")
    parser.add_argument("--publish-lark", action="store_true", help="Create a Feishu doc from the generated report.")
    parser.add_argument("--publish-output", default="", help="Feishu publish JSON output path.")
    parser.add_argument("--doc-path", default="")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--recipient", default="fengchao")
    parser.add_argument("--recipient-user-id", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--lark-profile", default=os.environ.get("PAI_OBS_LARK_PROFILE", paiobs_lark.DEFAULT_LARK_PROFILE))
    parser.add_argument("--send-as", choices=["auto", "user", "bot"], default=os.environ.get("PAI_OBS_LARK_SEND_AS", "bot"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "markdown"], default="json", help="Terminal output format.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ensure_time_window(args)
    report_md, raw_json, publish_json = resolve_outputs(args)
    client = paiobs.build_client(args)
    try:
        payload = build_report_payload(client, args)
        payload["outputs"] = {"report_md": str(report_md), "raw_json": str(raw_json), "publish_json": str(publish_json) if args.publish_lark else ""}
        report = render_report(payload)
        report_md.write_text(report, encoding="utf-8")
        raw_json.write_text(paiobs.json_dumps(payload) + "\n", encoding="utf-8")
        publish_payload = publish_to_lark(args, report_md, publish_json) if args.publish_lark else {}
        manifest = {
            "schema_version": "paiobs-quick-stats-report-manifest/v1",
            "report_md": str(report_md),
            "raw_json": str(raw_json),
            "publish_json": str(publish_json) if args.publish_lark else "",
            "doc_url": str(((publish_payload.get("doc") or {}).get("url") or (publish_payload.get("doc") or {}).get("remote_url") or "")) if publish_payload else "",
            "filters": payload.get("filters"),
            "env": payload.get("env"),
            "timings": payload.get("timings"),
        }
        if args.format == "markdown":
            sys.stdout.write(report)
        else:
            sys.stdout.write(paiobs.json_dumps(manifest) + "\n")
        return 0
    except paiobs.ApiError as exc:
        sys.stderr.write(f"ERROR: {exc.message}\n")
        if exc.payload is not None:
            sys.stderr.write(paiobs.json_dumps(exc.payload) + "\n")
        return 1
    except paiobs_lark.LarkError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
