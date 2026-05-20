#!/usr/bin/env python3.11
"""Gateway-side aggregate statistics for PaiWork historical tasks.

All commands in this file call the Observability Gateway analytics APIs.  The
script intentionally does not crawl history rows locally or fetch QueryAgentTabs
context one by one; heavy parsing belongs in the gateway process.
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import paiobs


BASE_DIMENSIONS = [
    "day",
    "hour",
    "status",
    "entry_scene",
    "scheduled",
    "is_web_search",
    "query_language",
    "web_query_language",
    "end_type",
    "user",
    "user_id",
    "institution",
    "institution_nature",
    "inst_type",
    "product_type",
    "user_type",
    "user_role",
]
DETAIL_DIMENSIONS = [
    "skill",
    "tool_name",
    "tool_type",
    "model",
    "data_source_type",
    "source_provider",
    "source_domain",
    "source_title",
    "source_id",
    "file_type",
    "file_role",
]
DEFAULT_TASK_DIMENSIONS = [
    "skill",
    "tool_name",
    "tool_type",
    "model",
    "data_source_type",
    "source_provider",
    "source_domain",
    "file_type",
    "file_role",
]
DEFAULT_AGGREGATE_METRICS = [
    "question_count",
    "session_count",
    "user_count",
    "success_count",
    "failed_count",
    "running_count",
    "success_rate",
    "avg_duration",
    "p50_duration",
    "p90_duration",
    "p95_duration",
    "avg_answer_length",
]
DEFAULT_DURATION_METRICS = [
    "question_count",
    "success_count",
    "failed_count",
    "running_count",
    "success_rate",
    "avg_duration",
    "p50_duration",
    "p90_duration",
    "p95_duration",
]
DEFAULT_TOKEN_GROUP_BY = ["status", "entry_scene", "user_role"]
DEFAULT_TOKEN_DIMENSIONS = ["model"]
DEFAULT_CREDIT_GROUP_BY = ["status", "entry_scene", "user_role"]
DEFAULT_CREDIT_DIMENSIONS: list[str] = []
DEFAULT_OVERVIEW_BREAKDOWNS = ["day", "hour", "status", "entry_scene", "scheduled", "is_web_search", "web_query_language", "user_type", "inst_type", "product_type", "institution"]
DEFAULT_OVERVIEW_TASK_DIMENSIONS: list[str] = []
DEFAULT_MAX_TASKS = int(os.environ.get("PAI_OBS_TASK_STATS_DEFAULT_MAX_TASKS", "100000"))
DEFAULT_AGGREGATE_WORKERS = max(1, int(os.environ.get("PAI_OBS_STATS_AGGREGATE_WORKERS", "6")))
DEFAULT_OVERVIEW_DISPLAY_TOP_N = int(os.environ.get("PAI_OBS_OVERVIEW_DISPLAY_TOP_N", "20"))
DISABLED_CSV_VALUES = {"", "none", "null", "off", "false", "0", "-"}


def parse_csv_arg(value: str | None, default: list[str] | None = None, *, empty_means_default: bool = False) -> list[str]:
    if value is None:
        return list(default or [])
    text = str(value).strip()
    if text.lower() in DISABLED_CSV_VALUES:
        return list(default or []) if empty_means_default else []
    parsed = paiobs.parse_csv_values(text)
    if parsed:
        return parsed
    return list(default or []) if empty_means_default else []


def add_task_stats_specific_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dimensions",
        default=",".join(DEFAULT_TASK_DIMENSIONS),
        help=(
            "Comma-separated dimensions. Base dimensions: "
            + ",".join(BASE_DIMENSIONS)
            + ". Detail dimensions: "
            + ",".join(DETAIL_DIMENSIONS)
        ),
    )
    parser.add_argument("--top-n", type=int, default=20, help="Top values kept per dimension.")
    parser.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS, help="Maximum matching tasks scanned inside the gateway.")
    parser.add_argument(
        "--include-sample-refs",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Ask the gateway to include sample session/task refs per bucket.",
    )
    parser.add_argument("--sample-ref-limit", type=int, default=0, help="Sample refs kept per bucket.")


def request_task_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    body = {
        "env": client.env,
        "filters": paiobs.build_filters(args),
        "dimensions": paiobs.parse_csv_values(getattr(args, "dimensions", "")),
        "top_n": getattr(args, "top_n", 20),
        "max_tasks": getattr(args, "max_tasks", DEFAULT_MAX_TASKS),
        "include_sample_refs": bool(getattr(args, "include_sample_refs", False)),
        "sample_ref_limit": getattr(args, "sample_ref_limit", 0),
    }
    if bool(getattr(args, "include_token_stats", False)):
        body["include_token_stats"] = True
    if bool(getattr(args, "include_credit_stats", False)):
        body["include_credit_stats"] = True
    payload = client.request("POST", "/analytics/task-stats", body=body)
    if isinstance(payload, dict):
        payload.setdefault("schema_version", "task-stats/v1")
    return payload


def run_task_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = request_task_stats(client, args)
    paiobs.output_payload(payload, args, default_format=getattr(args, "format", None) or "table")


def request_aggregate(
    client: paiobs.PaiObsClient,
    filters: dict[str, Any],
    group_by: list[str],
    metrics: list[str],
    limit: int,
) -> dict[str, Any]:
    return client.request(
        "POST",
        "/analytics/aggregate",
        body={
            "env": client.env,
            "filters": filters,
            "group_by": group_by,
            "metrics": metrics,
            "limit": limit,
        },
    )


def request_aggregate_set(
    client: paiobs.PaiObsClient,
    filters: dict[str, Any],
    breakdowns: list[str],
    metrics: list[str],
    limit: int,
) -> dict[str, Any]:
    specs = [("overall", [])] + [(dimension, [dimension]) for dimension in breakdowns]
    if len(specs) <= 1:
        return {"overall": request_aggregate(client, filters, [], metrics, limit)}
    workers = min(len(specs), DEFAULT_AGGREGATE_WORKERS)
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_key = {
            executor.submit(request_aggregate, client, filters, group_by, metrics, limit): key
            for key, group_by in specs
        }
        for future in as_completed(future_to_key):
            results[future_to_key[future]] = future.result()
    return {key: results[key] for key, _ in specs if key in results}


def request_facets(
    client: paiobs.PaiObsClient,
    filters: dict[str, Any],
    dimensions: list[str],
    limit: int,
    sample_limit: int,
) -> dict[str, Any]:
    return client.request(
        "POST",
        "/analytics/facets",
        body={
            "env": client.env,
            "filters": filters,
            "dimensions": dimensions,
            "limit": limit,
            "sample_limit": sample_limit,
        },
    )


def build_duration_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    filters = paiobs.build_filters(args)
    metrics = parse_csv_arg(getattr(args, "metrics", ""), DEFAULT_DURATION_METRICS, empty_means_default=True)
    group_by = parse_csv_arg(getattr(args, "group_by", ""))
    slow_dimensions = parse_csv_arg(getattr(args, "slow_dimensions", ""))
    limit = getattr(args, "limit", 500)
    aggregate_payloads = request_aggregate_set(client, filters, group_by, metrics, limit)
    payload: dict[str, Any] = {
        "schema_version": "duration-stats/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": client.env,
        "filters": filters,
        "metrics": metrics,
        "group_by": group_by,
        "slow_dimensions": slow_dimensions,
        "slow_min_occurrences": getattr(args, "slow_min_occurrences", 3),
        "slow_top_n": getattr(args, "slow_top_n", 10),
        "warnings": [],
        "overall": aggregate_payloads.get("overall"),
        "breakdowns": {dimension: aggregate_payloads[dimension] for dimension in group_by if dimension in aggregate_payloads},
    }
    if slow_dimensions:
        slow_args = argparse.Namespace(
            **{
                **vars(args),
                "dimensions": ",".join(slow_dimensions),
                "top_n": getattr(args, "top_n", 100),
                "include_sample_refs": False,
                "sample_ref_limit": 0,
            }
        )
        payload["slow_buckets"] = request_task_stats(client, slow_args)
    return payload


def build_token_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    filters = paiobs.build_filters(args)
    group_by = parse_csv_arg(getattr(args, "group_by", ""), DEFAULT_TOKEN_GROUP_BY)
    token_dimensions = parse_csv_arg(getattr(args, "token_dimensions", ""), DEFAULT_TOKEN_DIMENSIONS)
    dimensions = list(dict.fromkeys([*group_by, *token_dimensions]))
    task_args = argparse.Namespace(
        **{
            **vars(args),
            "dimensions": ",".join(dimensions),
            "top_n": getattr(args, "top_n", 100),
            "max_tasks": getattr(args, "max_tasks", DEFAULT_MAX_TASKS),
            "include_sample_refs": False,
            "sample_ref_limit": 0,
            "include_token_stats": True,
        }
    )
    task_stats = request_task_stats(client, task_args)
    warnings: list[str] = []
    if not isinstance(task_stats.get("token_metrics"), dict):
        warnings.append("gateway did not return token_metrics; deploy gateway support for include_token_stats")
    return {
        "schema_version": "token-stats/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": client.env,
        "filters": filters,
        "group_by": group_by,
        "token_dimensions": token_dimensions,
        "top_n": getattr(args, "top_n", 100),
        "max_tasks": getattr(args, "max_tasks", DEFAULT_MAX_TASKS),
        "warnings": warnings,
        "task_stats": task_stats,
    }


def build_credit_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    filters = paiobs.build_filters(args)
    group_by = parse_csv_arg(getattr(args, "group_by", ""), DEFAULT_CREDIT_GROUP_BY)
    credit_dimensions = parse_csv_arg(getattr(args, "credit_dimensions", ""), DEFAULT_CREDIT_DIMENSIONS)
    dimensions = list(dict.fromkeys([*group_by, *credit_dimensions]))
    task_args = argparse.Namespace(
        **{
            **vars(args),
            "dimensions": ",".join(dimensions),
            "top_n": getattr(args, "top_n", 100),
            "max_tasks": getattr(args, "max_tasks", DEFAULT_MAX_TASKS),
            "include_sample_refs": False,
            "sample_ref_limit": 0,
            "include_credit_stats": True,
        }
    )
    task_stats = request_task_stats(client, task_args)
    warnings: list[str] = []
    if not isinstance(task_stats.get("credit_metrics"), dict):
        warnings.append("gateway did not return credit_metrics; deploy gateway support for include_credit_stats")
    return {
        "schema_version": "credit-stats/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": client.env,
        "filters": filters,
        "group_by": group_by,
        "credit_dimensions": credit_dimensions,
        "top_n": getattr(args, "top_n", 100),
        "max_tasks": getattr(args, "max_tasks", DEFAULT_MAX_TASKS),
        "warnings": warnings,
        "task_stats": task_stats,
    }


def build_overview(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    filters = paiobs.build_filters(args)
    breakdowns = parse_csv_arg(getattr(args, "breakdowns", ""), DEFAULT_OVERVIEW_BREAKDOWNS, empty_means_default=True)
    task_dimensions = parse_csv_arg(getattr(args, "task_dimensions", ""), DEFAULT_OVERVIEW_TASK_DIMENSIONS)
    metrics = parse_csv_arg(getattr(args, "metrics", ""), DEFAULT_AGGREGATE_METRICS, empty_means_default=True)
    aggregate_payloads = request_aggregate_set(client, filters, breakdowns, metrics, getattr(args, "limit", 500))
    payload: dict[str, Any] = {
        "schema_version": "stats-overview/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": client.env,
        "filters": filters,
        "breakdowns": breakdowns,
        "task_dimensions": task_dimensions,
        "aggregate": aggregate_payloads,
    }
    if task_dimensions:
        task_args = argparse.Namespace(
            **{
                **vars(args),
                "dimensions": ",".join(task_dimensions),
                "top_n": getattr(args, "top_n", 20),
                "max_tasks": getattr(args, "max_tasks", DEFAULT_MAX_TASKS),
                "include_sample_refs": bool(getattr(args, "include_sample_refs", False)),
                "sample_ref_limit": getattr(args, "sample_ref_limit", 0),
            }
        )
        payload["task_stats"] = request_task_stats(client, task_args)
    if bool(getattr(args, "include_token_stats", False)):
        payload["token_stats"] = build_token_stats(
            client,
            argparse.Namespace(
                **{
                    **vars(args),
                    "group_by": getattr(args, "token_group_by", "status,entry_scene,user_role"),
                    "token_dimensions": getattr(args, "token_dimensions", "model"),
                    "top_n": getattr(args, "token_top_n", getattr(args, "top_n", 20)),
                    "max_tasks": getattr(args, "max_tasks", DEFAULT_MAX_TASKS),
                }
            ),
        )
    if bool(getattr(args, "include_credit_stats", False)):
        payload["credit_stats"] = build_credit_stats(
            client,
            argparse.Namespace(
                **{
                    **vars(args),
                    "group_by": getattr(args, "credit_group_by", "status,entry_scene,user_role"),
                    "credit_dimensions": getattr(args, "credit_dimensions", "none"),
                    "top_n": getattr(args, "credit_top_n", getattr(args, "top_n", 20)),
                    "max_tasks": getattr(args, "max_tasks", DEFAULT_MAX_TASKS),
                }
            ),
        )
    return payload


def parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise SystemExit(f"invalid datetime: {value}")


def previous_period_filters(filters: dict[str, Any]) -> dict[str, Any]:
    start = parse_dt(str(filters.get("start_time") or ""))
    end = parse_dt(str(filters.get("end_time") or ""))
    if not start or not end or end <= start:
        raise SystemExit("compare requires --start-time and --end-time")
    delta = end - start
    previous = dict(filters)
    previous["start_time"] = (start - delta).strftime("%Y-%m-%d %H:%M:%S")
    previous["end_time"] = start.strftime("%Y-%m-%d %H:%M:%S")
    return previous


def first_metric(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items") if isinstance(payload, dict) else []
    if isinstance(items, list) and items:
        item = items[0] if isinstance(items[0], dict) else {}
        return item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    return {}


def build_compare(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    current_filters = paiobs.build_filters(args)
    previous_filters = paiobs.read_json_value(getattr(args, "previous_filters_json", ""), default=None)
    if not isinstance(previous_filters, dict):
        previous_filters = previous_period_filters(current_filters)
    metrics = paiobs.parse_csv_values(getattr(args, "metrics", "")) or DEFAULT_AGGREGATE_METRICS
    current = request_aggregate(client, current_filters, [], metrics, getattr(args, "limit", 500))
    previous = request_aggregate(client, previous_filters, [], metrics, getattr(args, "limit", 500))
    current_metrics = first_metric(current)
    previous_metrics = first_metric(previous)
    deltas = {}
    for key in metrics:
        current_value = paiobs.safe_number(current_metrics.get(key))
        previous_value = paiobs.safe_number(previous_metrics.get(key))
        if current_value is None or previous_value is None:
            continue
        deltas[key] = {
            "current": current_value,
            "previous": previous_value,
            "delta": round(current_value - previous_value, 6),
            "delta_ratio": round((current_value - previous_value) / previous_value, 6) if previous_value else None,
        }
    return {
        "schema_version": "stats-compare/v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "env": client.env,
        "current_filters": current_filters,
        "previous_filters": previous_filters,
        "metrics": metrics,
        "current": current,
        "previous": previous,
        "deltas": deltas,
    }


def overview_metric_headers(first_column: str = "取值") -> list[str]:
    return [
        first_column,
        "query 数",
        "session 数",
        "用户数",
        "成功",
        "失败",
        "运行中",
        "已结束成功率",
    ]


def overview_metric_row(label: str, metrics: dict[str, Any]) -> list[Any]:
    return [
        label,
        metrics.get("question_count", ""),
        metrics.get("session_count", ""),
        metrics.get("user_count", ""),
        metrics.get("success_count", ""),
        metrics.get("failed_count", ""),
        metrics.get("running_count", ""),
        paiobs.format_ended_success_rate(metrics),
    ]


def overview_duration_headers(first_column: str = "取值") -> list[str]:
    return [first_column, "query 数", "已结束成功率", "平均耗时", "P50", "P90", "P95"]


def overview_duration_row(label: str, metrics: dict[str, Any]) -> list[Any]:
    return [
        label,
        metrics.get("question_count", ""),
        paiobs.format_ended_success_rate(metrics),
        paiobs.format_number(metrics.get("avg_duration")),
        paiobs.format_number(metrics.get("p50_duration")),
        paiobs.format_number(metrics.get("p90_duration")),
        paiobs.format_number(metrics.get("p95_duration")),
    ]


def rendered_top_items(items: list[Any], display_top_n: int) -> list[Any]:
    if display_top_n <= 0:
        return items
    return items[:display_top_n]


def render_overview_markdown(payload: dict[str, Any], args: argparse.Namespace | None = None) -> str:
    display_top_n = int(
        getattr(args, "display_top_n", DEFAULT_OVERVIEW_DISPLAY_TOP_N)
        if args is not None
        else DEFAULT_OVERVIEW_DISPLAY_TOP_N
    )
    json_output = getattr(args, "json_output", "") if args is not None else ""
    lines = [
        "# PaiWork Gateway Stats Overview",
        "",
        f"- 生成时间：{payload.get('generated_at', '')}",
        f"- 过滤条件：{paiobs.filter_summary(payload.get('filters') or {})}",
        f"- 展示策略：每个拆分维度展示 {'全部' if display_top_n <= 0 else 'Top ' + str(display_top_n)}；如需长尾，使用 `--json-output` 留存完整 JSON 或 `--display-top-n 0` 展示全部",
    ]
    if json_output:
        lines.append(f"- 完整 JSON：{json_output}")
    lines.extend(["", "## Overall", ""])
    overall = ((payload.get("aggregate") or {}).get("overall") or {}).get("items") or []
    overall_metrics = (overall[0].get("metrics") if overall and isinstance(overall[0], dict) else {}) or {}
    lines.append(
        paiobs.markdown_table(
            [overview_metric_row("整体", overall_metrics)],
            overview_metric_headers("分组"),
        )
    )
    for dimension in payload.get("breakdowns") or []:
        aggregate_payload = (payload.get("aggregate") or {}).get(dimension) or {}
        rows = []
        items = aggregate_payload.get("items") or []
        for item in rendered_top_items(items, display_top_n):
            rows.append(overview_metric_row(paiobs.aggregate_group_value(item), item.get("metrics") or {}))
        lines.extend(["", f"## {paiobs.dimension_label(dimension)}"])
        if display_top_n > 0 and len(items) > display_top_n:
            lines.append(f"仅展示 Top {display_top_n} / 共 {len(items)} 个取值。")
        lines.extend(["", paiobs.markdown_table(rows, overview_metric_headers()) if rows else "无数据"])
    lines.extend(["", "## 耗时统计", "", "- 耗时单位：秒；分位数基于任务级 `duration_seconds`。", ""])
    lines.append(
        paiobs.markdown_table(
            [overview_duration_row("整体", overall_metrics)],
            overview_duration_headers("分组"),
        )
        if overall_metrics
        else "无数据"
    )
    for dimension in payload.get("breakdowns") or []:
        aggregate_payload = (payload.get("aggregate") or {}).get(dimension) or {}
        rows = []
        items = aggregate_payload.get("items") or []
        for item in rendered_top_items(items, display_top_n):
            rows.append(overview_duration_row(paiobs.aggregate_group_value(item), item.get("metrics") or {}))
        lines.extend(["", f"### 按{paiobs.dimension_label(dimension)}", ""])
        lines.append(paiobs.markdown_table(rows, overview_duration_headers()) if rows else "无数据")
    token_stats = payload.get("token_stats") if isinstance(payload.get("token_stats"), dict) else {}
    if token_stats:
        lines.extend(["", "## Token 统计", ""])
        token_text = paiobs.render_token_stats_markdown(token_stats, argparse.Namespace()).strip()
        token_lines = token_text.splitlines()
        if token_lines and token_lines[0].startswith("# "):
            token_text = "\n".join(token_lines[2:]).strip()
        lines.append(token_text)
    credit_stats = payload.get("credit_stats") if isinstance(payload.get("credit_stats"), dict) else {}
    if credit_stats:
        lines.extend(["", "## Credit / 研究值统计", ""])
        credit_text = paiobs.render_credit_stats_markdown(credit_stats, argparse.Namespace()).strip()
        credit_lines = credit_text.splitlines()
        if credit_lines and credit_lines[0].startswith("# "):
            credit_text = "\n".join(credit_lines[2:]).strip()
        lines.append(credit_text)
    task_stats = payload.get("task_stats") if isinstance(payload.get("task_stats"), dict) else {}
    if task_stats:
        lines.extend(["", "## 明细派生维度", ""])
        lines.append(paiobs.render_task_stats_markdown(task_stats, argparse.Namespace(top_n=None)))
    return "\n".join(lines).rstrip() + "\n"


def render_compare_markdown(payload: dict[str, Any]) -> str:
    rows = []
    for metric, item in (payload.get("deltas") or {}).items():
        rows.append(
            [
                metric,
                item.get("current"),
                item.get("previous"),
                item.get("delta"),
                paiobs.format_percent((item.get("delta_ratio") or 0) * 100) if item.get("delta_ratio") is not None else "",
            ]
        )
    return "\n".join(
        [
            "# PaiWork Stats Compare",
            "",
            f"- 当前周期：{paiobs.filter_summary(payload.get('current_filters') or {})}",
            f"- 对比周期：{paiobs.filter_summary(payload.get('previous_filters') or {})}",
            "",
            paiobs.markdown_table(rows, ["metric", "current", "previous", "delta", "delta_ratio"]) if rows else "无可比指标",
        ]
    ) + "\n"


def output_stats_payload(payload: Any, args: argparse.Namespace, *, default_format: str = "json") -> None:
    payload = paiobs.normalize_analytics_payload(payload)
    json_output = getattr(args, "json_output", None)
    if json_output:
        paiobs.Path(json_output).write_text(paiobs.json_dumps(payload) + "\n", encoding="utf-8")
    fmt = getattr(args, "format", None) or default_format
    if fmt == "markdown" and isinstance(payload, dict) and payload.get("schema_version") == "stats-overview/v1":
        text = render_overview_markdown(payload, args)
    elif fmt == "markdown" and isinstance(payload, dict) and payload.get("schema_version") == "stats-compare/v1":
        text = render_compare_markdown(payload)
    else:
        paiobs.output_payload(payload, args, default_format=default_format)
        return
    out = getattr(args, "output", None)
    if out:
        paiobs.Path(out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def cmd_task_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    run_task_stats(client, args)


def cmd_aggregate(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    filters = paiobs.build_filters(args)
    payload = request_aggregate(
        client,
        filters,
        paiobs.parse_csv_values(args.group_by),
        paiobs.parse_csv_values(args.metrics) or DEFAULT_AGGREGATE_METRICS,
        args.limit,
    )
    paiobs.output_payload(payload, args, default_format=args.format)


def cmd_facets(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = request_facets(
        client,
        paiobs.build_filters(args),
        paiobs.parse_csv_values(args.dimensions) or BASE_DIMENSIONS,
        args.limit,
        args.sample_limit,
    )
    paiobs.output_payload(payload, args, default_format=args.format)


def cmd_duration_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = build_duration_stats(client, args)
    paiobs.output_payload(payload, args, default_format=args.format)


def cmd_token_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = build_token_stats(client, args)
    paiobs.output_payload(payload, args, default_format=args.format)


def cmd_credit_stats(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = build_credit_stats(client, args)
    paiobs.output_payload(payload, args, default_format=args.format)


def cmd_overview(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = build_overview(client, args)
    output_stats_payload(payload, args, default_format=args.format)


def cmd_compare(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = build_compare(client, args)
    output_stats_payload(payload, args, default_format=args.format)


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--gateway-profile", default=None, help="Gateway profile: release/prod -> 30100, local/test/debug -> localhost:6193")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env", default=None)
    parser.add_argument("--timeout", default=None)
    parser.add_argument("--file-auth-token", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PaiWork gateway-side task statistics CLI")
    add_runtime_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    task_stats = sub.add_parser("task-stats", help="Gateway-side task bucket statistics")
    paiobs.add_filter_args(task_stats)
    add_task_stats_specific_args(task_stats)
    paiobs.add_output_args(task_stats, default_format="table")
    task_stats.set_defaults(func=cmd_task_stats)

    aggregate = sub.add_parser("aggregate", help="Gateway aggregate metrics")
    paiobs.add_filter_args(aggregate)
    aggregate.add_argument("--group-by", default="")
    aggregate.add_argument("--metrics", default=",".join(DEFAULT_AGGREGATE_METRICS))
    aggregate.add_argument("--limit", type=int, default=500)
    paiobs.add_output_args(aggregate, default_format="table")
    aggregate.set_defaults(func=cmd_aggregate)

    facets = sub.add_parser("facets", help="Gateway facets")
    paiobs.add_filter_args(facets)
    facets.add_argument("--dimensions", default="")
    facets.add_argument("--limit", type=int, default=20)
    facets.add_argument("--sample-limit", type=int, default=20)
    paiobs.add_output_args(facets, default_format="table")
    facets.set_defaults(func=cmd_facets)

    duration = sub.add_parser("duration-stats", help="Duration and slow-bucket statistics")
    paiobs.add_filter_args(duration)
    duration.add_argument("--group-by", default="status,entry_scene,user_role")
    duration.add_argument("--metrics", default=",".join(DEFAULT_DURATION_METRICS))
    duration.add_argument("--limit", type=int, default=500)
    duration.add_argument("--slow-dimensions", default="")
    duration.add_argument("--top-n", type=int, default=100)
    duration.add_argument("--slow-top-n", type=int, default=10)
    duration.add_argument("--slow-min-occurrences", type=int, default=3)
    duration.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    paiobs.add_output_args(duration, default_format="markdown")
    duration.set_defaults(func=cmd_duration_stats)

    token = sub.add_parser("token-stats", help="Token usage statistics and token-heavy buckets")
    paiobs.add_filter_args(token)
    token.add_argument("--group-by", default=",".join(DEFAULT_TOKEN_GROUP_BY))
    token.add_argument("--token-dimensions", default=",".join(DEFAULT_TOKEN_DIMENSIONS))
    token.add_argument("--top-n", type=int, default=100)
    token.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    paiobs.add_output_args(token, default_format="markdown")
    token.set_defaults(func=cmd_token_stats)

    credit = sub.add_parser("credit-stats", help="Research-value/Credit usage statistics from saas_point_freeze_order")
    paiobs.add_filter_args(credit)
    credit.add_argument("--group-by", default=",".join(DEFAULT_CREDIT_GROUP_BY))
    credit.add_argument("--credit-dimensions", default=",".join(DEFAULT_CREDIT_DIMENSIONS))
    credit.add_argument("--top-n", type=int, default=100)
    credit.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    paiobs.add_output_args(credit, default_format="markdown")
    credit.set_defaults(func=cmd_credit_stats)

    overview = sub.add_parser("overview", help="One-shot overview with aggregate breakdowns and detail buckets")
    paiobs.add_filter_args(overview)
    overview.add_argument("--breakdowns", default=",".join(DEFAULT_OVERVIEW_BREAKDOWNS))
    overview.add_argument("--task-dimensions", default="")
    overview.add_argument("--metrics", default=",".join(DEFAULT_AGGREGATE_METRICS))
    overview.add_argument("--top-n", type=int, default=20)
    overview.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    overview.add_argument("--include-sample-refs", action=argparse.BooleanOptionalAction, default=False)
    overview.add_argument("--sample-ref-limit", type=int, default=0)
    overview.add_argument("--include-token-stats", action=argparse.BooleanOptionalAction, default=False)
    overview.add_argument("--token-group-by", default="status,entry_scene,user_role")
    overview.add_argument("--token-dimensions", default="model")
    overview.add_argument("--token-top-n", type=int, default=20)
    overview.add_argument("--include-credit-stats", action=argparse.BooleanOptionalAction, default=False)
    overview.add_argument("--credit-group-by", default="status,entry_scene,user_role")
    overview.add_argument("--credit-dimensions", default="none")
    overview.add_argument("--credit-top-n", type=int, default=20)
    overview.add_argument("--limit", type=int, default=500)
    overview.add_argument(
        "--display-top-n",
        type=int,
        default=DEFAULT_OVERVIEW_DISPLAY_TOP_N,
        help="Rows shown per aggregate breakdown in markdown output; 0 shows all returned rows.",
    )
    overview.add_argument(
        "--json-output",
        help="Also write the complete normalized overview JSON in the same CLI run.",
    )
    paiobs.add_output_args(overview, default_format="markdown")
    overview.set_defaults(func=cmd_overview)

    compare = sub.add_parser("compare", help="Compare current period with previous equal-length period")
    paiobs.add_filter_args(compare)
    compare.add_argument("--previous-filters-json", default="", help="Optional JSON/path overriding automatic previous period")
    compare.add_argument("--metrics", default=",".join(DEFAULT_AGGREGATE_METRICS))
    compare.add_argument("--limit", type=int, default=500)
    paiobs.add_output_args(compare, default_format="markdown")
    compare.set_defaults(func=cmd_compare)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = paiobs.build_client(args)
    try:
        args.func(client, args)
        return 0
    except paiobs.ApiError as exc:
        sys.stderr.write(f"ERROR: {exc.message}\n")
        if exc.payload is not None:
            sys.stderr.write(paiobs.json_dumps(exc.payload) + "\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
