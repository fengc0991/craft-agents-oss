#!/usr/bin/env python3.11
"""Local theme scans over PaiWork historical question text.

This script fills the gap between gateway search/analytics and LLM batch
analysis: collect a complete question-text corpus for a time window, then run
auditable local extractors over the user query text.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import paiobs


DEFAULT_TEXT_FIELDS = ["latest_question", "first_question", "question", "question_text", "question_preview", "match_preview"]
DEFAULT_X_CONTEXT_PATTERN = (
    r"(x\.com|twitter\.com|\btwitter\b|x\s*/\s*twitter|twitter\s*/\s*x|"
    r"推特|推文|\btweet\b|\btweets\b|X\.com|X账号|X\s*账号|X（Twitter）|X\(Twitter\))"
)
X_RESERVED_HANDLES = {
    "about",
    "compose",
    "download",
    "explore",
    "handle",
    "hashtag",
    "home",
    "i",
    "intent",
    "jobs",
    "login",
    "messages",
    "notifications",
    "privacy",
    "search",
    "settings",
    "share",
    "signup",
    "status",
    "statuses",
    "tos",
    "user",
    "username",
}

DATA_INTENT_REQUEST_PATTERN = (
    r"查询|查找|查一下|查一查|找一下|获取|拿到|拉取|收集|搜集|整理|统计|汇总|提取|列出|"
    r"抓取|爬取|采集|下载|导出|更新|补充|接入|支持|覆盖|监控|跟踪|提供|给我|帮我找|"
    r"需要|想要|希望|有没有|能否|是否可以|get|fetch|collect|extract|download|export|dataset|database"
)
DATA_INTENT_OBJECT_PATTERN = (
    r"数据|数据源|数据库|数据集|网站|平台|接口|API|指标|明细|清单|列表|表格|价格|报价|行情|指数|"
    r"库存|产量|产能|销量|销售|出货|装机|开工率|负荷|进出口|进口|出口|需求|供给|消费|"
    r"财报|公告|研报|新闻|舆情|招投标|中标|药品|临床|管线|审批|注册|煤炭|煤价|化工|大宗|"
    r"data|source|metric|price|inventory|production|shipment|financials"
)
DATA_INTENT_NEED_PATTERN = r"(需要|想要|希望|缺少|没有|能否|是否可以|支持|接入|覆盖|补充).{0,30}(数据|数据源|数据库|数据集|网站|平台|接口|API)"
DATA_SOURCE_CONTEXT_PATTERN = r"数据源|数据来源|来源|来自|数据库|数据平台|行业网站|垂类网站|网站|平台|接口|API|接入|抓取|采集|爬取|访问|使用|参考|查询|查看"
URL_SOURCE_CONTEXT_PATTERN = r"数据|来源|来自|接入|抓取|采集|爬取|访问|查询|查看|跟踪|监控|分析|价格|行情|资讯|网站|平台|接口|上|里|中"
NAMED_SOURCE_SUFFIX_PATTERN = (
    r"(?:数据平台|数据库|数据终端|数据中心|数据服务|数据资讯|数据网|信息网|行业网|资讯网|资源网|煤炭网|货币网|证券网|财经网|"
    r"医药网|化工网|能源网|有色网|钢铁网|交易中心|交易所|统计局|研究院|协会|海关|资讯|终端|智库|化工|钢联|魔方)"
)
NAMED_SOURCE_TOKEN_PATTERN = (
    rf"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·&（）()_.\-]{{1,28}}{NAMED_SOURCE_SUFFIX_PATTERN}"
)
GENERIC_SOURCE_PATTERNS = [
    rf"(?:数据源|数据来源|来源|来自|接入|抓取|采集|爬取|访问|使用|参考|从|在|查询|查看|打开|登录|看).{{0,18}}?(?P<value>{NAMED_SOURCE_TOKEN_PATTERN}(?:[、,，/]\s*{NAMED_SOURCE_TOKEN_PATTERN})*)",
    rf"(?P<value>{NAMED_SOURCE_TOKEN_PATTERN}(?:[、,，/]\s*{NAMED_SOURCE_TOKEN_PATTERN})*).{{0,18}}?(?:数据源|数据来源|来源|数据|网站|平台|接口|API|上|里|中|查询|查看|抓取|采集|爬取|接入|访问|使用|参考)",
    rf"(?:例如|比如|包括|包含|提到|提及|列举)(?:[:：为是]|\s)*?(?P<value>{NAMED_SOURCE_TOKEN_PATTERN}(?:[、,，/]\s*{NAMED_SOURCE_TOKEN_PATTERN})*)",
]
GENERIC_SOURCE_STOPWORDS = {
    "api",
    "API",
    "互联网",
    "公开",
    "公开数据",
    "全网",
    "官方",
    "官网",
    "垂类",
    "垂类行业",
    "行业",
    "行业数据",
    "网页",
    "网站",
    "平台",
    "数据库",
    "数据",
    "数据源",
    "数据平台",
    "新闻",
    "研报",
    "公告",
    "这种",
    "这类",
    "类似",
    "如果",
    "交易所",
    "行业协会",
    "主要指数",
    "行情数据库",
    "数据中心",
    "业务",
    "模型",
    "基础大模型",
    "大模型",
    "向量",
    "标注",
    "不标注",
    "弱权威",
    "卖方评论",
    "可能是一些",
}
SOURCE_DOMAIN_TLDS = {
    "ai",
    "biz",
    "cc",
    "cn",
    "co",
    "com",
    "edu",
    "gov",
    "hk",
    "io",
    "jp",
    "kr",
    "net",
    "org",
    "sg",
    "tw",
    "uk",
    "us",
    "xyz",
}
DATA_TERM_DEFINITIONS = [
    ("价格/报价/行情", r"价格|报价|行情|现货|期货|均价|涨跌|价差|价格指数|报价单"),
    ("库存", r"库存|库容|库销比"),
    ("产量/产能/开工", r"产量|产能|开工率|负荷|排产|利用率"),
    ("销量/出货/需求", r"销量|销售|出货|装机|需求|消费|订单"),
    ("进出口/海关", r"进出口|进口|出口|海关"),
    ("财务/公告", r"财报|营收|收入|利润|毛利|净利|资产负债|现金流|公告|年报|季报"),
    ("医药研发/临床", r"药品|医药|临床|适应症|管线|靶点|注册|审批|CDE|NMPA|获批"),
    ("煤炭数据", r"煤炭|煤价|动力煤|焦煤|焦炭"),
    ("化工/大宗商品", r"化工|大宗|原油|塑料|橡胶|PVC|PTA|乙二醇|甲醇|纯碱|尿素|聚烯烃"),
    ("招投标/工商", r"招投标|中标|投标|工商|注册资本|股东|企业名单|客户名单"),
    ("新闻/研报/舆情", r"新闻|舆情|研报|报告|公告"),
    ("宏观/官方统计", r"宏观|GDP|CPI|PPI|PMI|统计局|社融|利率|汇率"),
]
DATA_INTENT_CATEGORY_DEFINITIONS = [
    ("价格行情数据", r"价格|报价|行情|现货|期货|均价|涨跌|价差|价格指数"),
    ("产业供需/产销存数据", r"库存|产量|产能|销量|销售|出货|装机|开工率|负荷|进出口|进口|出口|需求|供给|消费"),
    ("公司财务/公告数据", r"财报|营收|收入|利润|毛利|净利|资产负债|现金流|公告|年报|季报|上市公司"),
    ("医药研发/审批数据", r"医药|药品|临床|适应症|管线|靶点|注册|审批|CDE|NMPA|获批"),
    ("煤炭行业数据", r"煤炭|煤价|动力煤|焦煤|焦炭"),
    ("化工/大宗商品数据", r"化工|大宗|原油|塑料|橡胶|PVC|PTA|乙二醇|甲醇|纯碱|尿素|聚烯烃"),
    ("宏观/官方统计数据", r"宏观|GDP|CPI|PPI|PMI|统计局|海关|社融|利率|汇率"),
    ("招投标/企业名单数据", r"招投标|中标|投标|工商|注册资本|股东|企业名单|客户名单"),
    ("新闻/研报/舆情数据", r"新闻|舆情|研报|报告"),
]
DATA_INTENT_THEME_RULES = {
    "scope": "question",
    "groups": [
        {
            "include_any": [
                "数据平台",
                "数据库",
                "数据终端",
                "数据中心",
                "数据服务",
                "数据资讯",
                "数据网",
                "信息网",
                "行业网",
                "资讯网",
                "资源网",
                "交易中心",
                "交易所",
                "统计局",
                "研究院",
                "协会",
                "海关",
                "资讯",
                "终端",
                "智库",
                "钢联",
                "魔方",
                "煤炭网",
                "货币网",
                "证券网",
                "财经网",
                "医药网",
                "化工网",
                "能源网",
                "有色网",
                "钢铁网",
                "http://",
                "https://",
                "www.",
                ".com",
                ".cn",
                ".net",
                ".org",
                ".gov",
                ".edu",
                ".io",
                ".ai",
            ]
        },
        {
            "include_all_any": [
                [
                    "数据源",
                    "数据来源",
                    "来源",
                    "来自",
                    "接入",
                    "抓取",
                    "采集",
                    "爬取",
                    "访问",
                    "使用",
                    "参考",
                    "查询",
                    "查看",
                    "获取",
                    "网站",
                    "平台",
                    "接口",
                    "API",
                    "数据",
                ],
                ["化工", "煤炭", "能源", "有色", "医药"],
            ]
        },
    ],
    "exclude": ["数据不对", "数据有误", "格式不对", "不准确", "RLHF", "SFT", "微调", "蒸馏", "模型压缩", "大模型", "向量", "标注"],
}

DETAIL_CSV_FIELDS = [
    "window_start",
    "window_end",
    "request_time",
    "categories",
    "user_name",
    "institution",
    "status",
    "severity",
    "confidence",
    "matched_keywords",
    "session_id",
    "task_index",
    "question_id",
    "ref",
    "feedback_text",
]


def add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--gateway-profile", default=None, help="Gateway profile: release/prod -> 30100, local/test/debug -> localhost:6193")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env", default=None)
    parser.add_argument("--timeout", default=None)
    parser.add_argument("--file-auth-token", default=None)


def add_corpus_args(parser: argparse.ArgumentParser) -> None:
    paiobs.add_filter_args(parser)
    parser.add_argument("--input", help="Existing corpus JSON/JSONL from the collect command. If omitted, fetches from gateway.")
    parser.add_argument("--profile", default="summary", choices=["lite", "summary", "raw"])
    parser.add_argument("--hours", type=float, default=24, help="Default lookback when start/end filters are not supplied.")
    parser.add_argument("--initial-window-minutes", type=float, default=5, help="Initial time-slice size for adaptive collection.")
    parser.add_argument("--page-limit", type=int, default=100, help="Gateway page limit per slice.")
    parser.add_argument("--max-workers", type=int, default=8)
    parser.add_argument("--min-slice-seconds", type=float, default=1)
    parser.add_argument("--text-fields", default=",".join(DEFAULT_TEXT_FIELDS), help="Comma-separated fields used as query text.")
    parser.add_argument("--verbose", action="store_true", help="Write collection progress to stderr.")


def add_scan_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=["json", "jsonl", "table", "pretty", "markdown"],
        default="table",
        help="Output format. table is CSV-compatible.",
    )
    parser.add_argument("--max-cell-chars", type=int, default=None)
    parser.add_argument("--detail-csv", action="store_true", help="Write unified query-detail CSV rows for matched queries instead of aggregate values.")
    parser.add_argument("-o", "--output")


def parse_time(value: str) -> datetime:
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                return parsed.replace(hour=0, minute=0, second=0)
            return parsed
        except ValueError:
            continue
    raise SystemExit(f"invalid time '{value}', expected YYYY-MM-DD HH:MM:SS")


def format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def build_scan_filters(args: argparse.Namespace) -> dict[str, Any]:
    filters = paiobs.build_filters(args)
    end_time = str(filters.get("end_time") or "").strip()
    start_time = str(filters.get("start_time") or "").strip()
    if not end_time:
        end_dt = datetime.now().replace(microsecond=0)
        filters["end_time"] = format_time(end_dt)
    else:
        end_dt = parse_time(end_time)
        filters["end_time"] = format_time(end_dt)

    if not start_time:
        filters["start_time"] = format_time(end_dt - timedelta(hours=float(getattr(args, "hours", 24) or 24)))
    else:
        filters["start_time"] = format_time(parse_time(start_time))
    return filters


def item_key(item: dict[str, Any]) -> str:
    session_id = str(item.get("session_id") or "")
    task_index = str(item.get("task_index") or "")
    question_id = str(item.get("question_id") or "")
    raw = f"{session_id}:{task_index}:{question_id}"
    return raw if raw.strip(":") else paiobs.json_dumps(item, compact=True)


def query_ref(item: dict[str, Any]) -> str:
    return f"{item.get('session_id', '')}:{item.get('task_index', '')}"


def compact_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def joined_values(values: Iterable[Any]) -> str:
    return "|".join(str(value) for value in values if value not in (None, ""))


def filter_window(filters: dict[str, Any]) -> tuple[str, str]:
    return str(filters.get("start_time") or ""), str(filters.get("end_time") or "")


def detail_csv_row(
    item: dict[str, Any],
    *,
    filters: dict[str, Any],
    category: str,
    matched_values: Iterable[Any],
    text: str,
) -> dict[str, Any]:
    session_id = str(item.get("session_id") or "")
    task_index = str(item.get("task_index") or "")
    question_id = str(item.get("question_id") or "")
    start_time, end_time = filter_window(filters)
    return {
        "window_start": start_time,
        "window_end": end_time,
        "request_time": item.get("request_time") or "",
        "categories": category,
        "user_name": item.get("user_name") or item.get("user") or "",
        "institution": item.get("institution") or item.get("user_institution") or "",
        "status": item.get("status") or "",
        "severity": "",
        "confidence": "",
        "matched_keywords": joined_values(matched_values),
        "session_id": session_id,
        "task_index": task_index,
        "question_id": question_id,
        "ref": f"{session_id}:{task_index}" if session_id or task_index else "",
        "feedback_text": compact_text(text),
    }


def detail_csv_text(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=DETAIL_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def question_text(item: dict[str, Any], text_fields: list[str]) -> str:
    values: list[str] = []
    for field in text_fields:
        value = item.get(field)
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return "\n".join(values)


def question_text_for_item(item: dict[str, Any], args: argparse.Namespace) -> str:
    fields = paiobs.parse_csv_values(getattr(args, "text_fields", "")) or DEFAULT_TEXT_FIELDS
    return question_text(item, fields)


def search_questions(client: paiobs.PaiObsClient, filters: dict[str, Any], profile: str, limit: int) -> dict[str, Any]:
    body = {
        "env": client.env,
        "filters": filters,
        "page": {"limit": limit, "cursor": ""},
        "profile": profile,
    }
    return client.request("POST", "/history/questions/search", body=body)


def time_windows(start_dt: datetime, end_dt: datetime, step: timedelta) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    current = start_dt
    while current < end_dt:
        nxt = min(current + step, end_dt)
        windows.append((current, nxt))
        current = nxt
    return windows


def collect_corpus(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    filters = build_scan_filters(args)
    start_dt = parse_time(str(filters["start_time"]))
    end_dt = parse_time(str(filters["end_time"]))
    if end_dt <= start_dt:
        raise SystemExit("end_time must be greater than start_time")

    page_limit = max(1, int(getattr(args, "page_limit", 100) or 100))
    initial_minutes = float(getattr(args, "initial_window_minutes", 5) or 5)
    min_slice_seconds = float(getattr(args, "min_slice_seconds", 1) or 1)
    max_workers = max(1, int(getattr(args, "max_workers", 8) or 8))
    profile = getattr(args, "profile", "summary")
    verbose = bool(getattr(args, "verbose", False))

    seen: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    query_count = 0
    capped_windows: list[dict[str, Any]] = []

    def fetch_window(start: datetime, end: datetime) -> tuple[datetime, datetime, list[dict[str, Any]]]:
        nonlocal query_count
        window_filters = dict(filters)
        window_filters["start_time"] = format_time(start)
        window_filters["end_time"] = format_time(end)
        payload = search_questions(client, window_filters, profile, page_limit)
        with lock:
            query_count += 1
        batch = payload.get("items") if isinstance(payload, dict) else []
        return start, end, [item for item in batch or [] if isinstance(item, dict)]

    pending = time_windows(start_dt, end_dt, timedelta(minutes=initial_minutes))
    round_no = 0
    while pending:
        round_no += 1
        next_pending: list[tuple[datetime, datetime]] = []
        completed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(fetch_window, start, end) for start, end in pending]
            for future in as_completed(futures):
                start, end, items = future.result()
                completed += 1
                span_seconds = (end - start).total_seconds()
                if len(items) >= page_limit and span_seconds > min_slice_seconds:
                    midpoint = start + (end - start) / 2
                    next_pending.append((start, midpoint))
                    next_pending.append((midpoint, end))
                else:
                    if len(items) >= page_limit:
                        capped_windows.append({"start_time": format_time(start), "end_time": format_time(end), "count": len(items)})
                    for item in items:
                        seen[item_key(item)] = item
                if verbose and completed % 50 == 0:
                    print(
                        f"round {round_no}: {completed}/{len(pending)} windows, "
                        f"unique={len(seen)}, split_next={len(next_pending)}, queries={query_count}",
                        file=sys.stderr,
                        flush=True,
                    )
        if verbose:
            print(
                f"round {round_no} complete: windows={len(pending)}, unique={len(seen)}, "
                f"split_next={len(next_pending)}, queries={query_count}",
                file=sys.stderr,
                flush=True,
            )
        pending = next_pending

    items = sorted(seen.values(), key=lambda item: (str(item.get("request_time") or ""), str(item.get("session_id") or ""), int(item.get("task_index") or 0)))
    fields = paiobs.parse_csv_values(getattr(args, "text_fields", "")) or DEFAULT_TEXT_FIELDS
    for item in items:
        item["query_text"] = question_text(item, fields)

    warnings: list[str] = []
    if capped_windows:
        warnings.append("some windows still reached page_limit at min slice; results may be incomplete")
    return {
        "schema_version": "query-theme-corpus/v1",
        "env": client.env,
        "filters": filters,
        "profile": profile,
        "items": items,
        "count": len(items),
        "coverage": {
            "query_count": query_count,
            "page_limit": page_limit,
            "initial_window_minutes": initial_minutes,
            "min_slice_seconds": min_slice_seconds,
            "capped_windows_count": len(capped_windows),
            "capped_windows_sample": capped_windows[:10],
            "warnings": warnings,
        },
    }


def load_corpus(path: str) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise SystemExit(f"input not found: {path}")
    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".jsonl":
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
        return {"schema_version": "query-theme-corpus/v1", "items": items, "count": len(items), "source": str(source)}
    payload = json.loads(text)
    if isinstance(payload, list):
        return {"schema_version": "query-theme-corpus/v1", "items": payload, "count": len(payload), "source": str(source)}
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return payload
    raise SystemExit("--input must be a JSONL file, a JSON list, or a JSON object with items")


def get_or_collect_corpus(client: paiobs.PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "input", None):
        return load_corpus(args.input)
    return collect_corpus(client, args)


def gateway_theme_search_corpus(client: paiobs.PaiObsClient, args: argparse.Namespace, *, theme_rules: dict[str, Any]) -> dict[str, Any]:
    filters = build_scan_filters(args)
    max_items = max(1, int(getattr(args, "max_items", 500) or 500))
    profile = str(getattr(args, "theme_search_profile", "") or "lite").strip() or "lite"
    started = datetime.now()
    payload = client.request(
        "POST",
        "/history/questions/theme-search",
        body={
            "env": client.env,
            "filters": filters,
            "theme_rules": theme_rules,
            "max_items": max_items,
            "profile": profile,
        },
    )
    items = [item for item in payload.get("items") or [] if isinstance(item, dict)] if isinstance(payload, dict) else []
    fields = paiobs.parse_csv_values(getattr(args, "text_fields", "")) or DEFAULT_TEXT_FIELDS
    for item in items:
        item["query_text"] = question_text(item, fields)
    elapsed = (datetime.now() - started).total_seconds()
    timings = payload.get("timings") if isinstance(payload, dict) and isinstance(payload.get("timings"), dict) else {}
    return {
        "schema_version": "query-theme-corpus/v1",
        "env": client.env,
        "filters": filters,
        "profile": profile,
        "items": items,
        "count": len(items),
        "coverage": {
            "mode": "gateway_theme_search",
            "gateway_schema_version": payload.get("schema_version") if isinstance(payload, dict) else "",
            "gateway_count": payload.get("count") if isinstance(payload, dict) else len(items),
            "max_items": max_items,
            "query_count": 1,
            "elapsed_seconds": round(elapsed, 3),
            "gateway_elapsed_seconds": timings.get("elapsed_seconds", ""),
            "warnings": [],
        },
    }


def normalize_value(value: str, mode: str) -> str:
    text = value.strip()
    if mode == "lower":
        return text.lower()
    return text


def add_match(bucket: dict[str, Any], value: str, source: str, item: dict[str, Any], position: int, text: str, include_snippet: bool) -> None:
    forms: Counter[str] = bucket.setdefault("forms", Counter())
    forms[value] += 1
    bucket["mention_count"] = int(bucket.get("mention_count") or 0) + 1
    source_types: Counter[str] = bucket.setdefault("source_types", Counter())
    source_types[source] += 1
    refs: set[str] = bucket.setdefault("refs", set())
    refs.add(item_key(item))
    request_time = str(item.get("request_time") or "")
    if request_time:
        if not bucket.get("first_seen") or request_time < bucket["first_seen"]:
            bucket["first_seen"] = request_time
        if not bucket.get("last_seen") or request_time > bucket["last_seen"]:
            bucket["last_seen"] = request_time
    if not bucket.get("sample"):
        sample = {"ref": query_ref(item), "request_time": request_time}
        if include_snippet:
            sample["snippet"] = text[max(0, position - 90) : position + 160].replace("\n", " ")
        bucket["sample"] = sample


def finalize_buckets(
    buckets: dict[str, dict[str, Any]],
    *,
    min_task_count: int,
    top_n: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, bucket in buckets.items():
        forms: Counter[str] = bucket.get("forms", Counter())
        source_types: Counter[str] = bucket.get("source_types", Counter())
        sample = bucket.get("sample") or {}
        task_count = len(bucket.get("refs") or set())
        if task_count < min_task_count:
            continue
        row = {
            "value": forms.most_common(1)[0][0] if forms else "",
            "task_count": task_count,
            "mention_count": int(bucket.get("mention_count") or 0),
            "first_seen": bucket.get("first_seen", ""),
            "last_seen": bucket.get("last_seen", ""),
            "source_types": ";".join(f"{key}:{count}" for key, count in source_types.most_common()),
            "sample_ref": sample.get("ref", ""),
        }
        if sample.get("snippet"):
            row["sample_snippet"] = sample.get("snippet", "")
        rows.append(row)
    rows.sort(key=lambda row: (-int(row.get("task_count") or 0), str(row.get("value") or "").lower()))
    if top_n > 0:
        rows = rows[:top_n]
    return rows


def compile_regex(pattern: str, ignore_case: bool = True) -> re.Pattern[str]:
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(pattern, flags)


def top_counter_text(counter: Counter[str] | dict[str, int] | None, limit: int = 5) -> str:
    if not counter:
        return ""
    if not isinstance(counter, Counter):
        counter = Counter(counter)
    return ";".join(f"{key}:{count}" for key, count in counter.most_common(limit))


def source_definitions(args: argparse.Namespace) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for value in getattr(args, "source", None) or []:
        text = str(value or "").strip()
        if text:
            definitions.append({"canonical": text, "category": "自定义候选", "patterns": [re.escape(text)]})
    source_file = getattr(args, "source_file", "")
    if source_file:
        for raw_line in Path(source_file).read_text(encoding="utf-8").splitlines():
            text = raw_line.strip()
            if text and not text.startswith("#"):
                definitions.append({"canonical": text, "category": "自定义候选", "patterns": [re.escape(text)]})
    return definitions


def compiled_source_definitions(args: argparse.Namespace) -> list[dict[str, Any]]:
    compiled: list[dict[str, Any]] = []
    for definition in source_definitions(args):
        patterns = [compile_regex(pattern, True) for pattern in definition.get("patterns") or []]
        if not patterns:
            continue
        compiled.append(
            {
                "canonical": str(definition.get("canonical") or ""),
                "category": str(definition.get("category") or ""),
                "patterns": patterns,
            }
        )
    return compiled


def split_source_candidates(value: str) -> list[str]:
    parts = re.split(r"[、,，/]|和|及|与|或|(?:\s+和\s+)|(?:\s+及\s+)", value)
    return [part for part in parts if part.strip()]


def is_stock_like_domain(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4,8}\.(?:hk|sz|sh|bj|ss)", value.strip().lower()))


def looks_like_named_source_candidate(value: str) -> bool:
    text = value.strip()
    lower = text.lower()
    if not text or len(text) < 2:
        return False
    if lower in {item.lower() for item in GENERIC_SOURCE_STOPWORDS}:
        return False
    if is_stock_like_domain(lower):
        return False
    if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
        return True
    if re.match(r"^\d", text):
        return False
    if text in {"市场资讯", "宏观资讯", "医药资讯", "科技资讯", "国内科技资讯", "实时行情数据库"}:
        return False
    if re.search(r"[()（）]", text):
        return False
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    if len(cjk_chars) > 14 and not re.search(r"[A-Za-z0-9.]", text):
        return False
    if re.match(r"^(?:A股|港股|美股|沪深|中证|股票|基金|债券|公募基金|实时行情).{0,12}数据库$", text, re.IGNORECASE):
        return False
    if re.search(r"可能|如果|每条|以下|主要|最新|基础|模型|业务|卖方|弱权威|不标注|向量|标注|传闻|谣言|总结|摘要|标题|发现|信号|重要|异动|判断|检查|交易日|联网|逐一|表格|全文是否|不得|不要|禁止|不可|不能|摘自|是一家|一家公司|一家|下游|传统|数据中心|大型|推送|同时搜索|学习|学会|加载|行情数据库", text):
        return False
    if re.search(r"帮我|希望|需要|想要|查询|查找|获取|整理|抓取|采集|下载|导出|接入|支持|建立", text):
        return False
    if len(text) > 32 and not re.search(r"[A-Za-z0-9.]", text):
        return False
    return bool(re.search(NAMED_SOURCE_SUFFIX_PATTERN + r"$", text, re.IGNORECASE))


def clean_source_candidate(value: str) -> str:
    text = compact_text(value)
    text = re.sub(r"^[：:，,、/和及与\s]+", "", text)
    text = re.sub(
        r"^(?:请|帮我|给我|我|我们|希望|需要|想要|能否|是否可以)?(?:想看|看一下|查一下|查一查|优先使用|根据|判断|检查|接入|支持|使用|参考|来自|来源于|从|在|抓取|采集|爬取|访问|打开|登录|查询|查看|看|提到|提及|列举|命中|过滤|筛选)",
        "",
        text,
    )
    text = re.sub(r"^.*(?:从|来自|来源于)", "", text)
    text = re.sub(r"[：:，,、/和及与\s]+$", "", text)
    text = re.sub(r"(?:这种|这类|类似).*$", "", text)
    text = re.sub(r"的(?:数据|网站|平台|接口|API)?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:这种|这类|类似|等|的|行业|垂类)$", "", text)
    text = text.strip("《》（）()[]【】\"'“”‘’")
    if not text or len(text) < 2:
        return ""
    if text in GENERIC_SOURCE_STOPWORDS or text.lower() in {item.lower() for item in GENERIC_SOURCE_STOPWORDS}:
        return ""
    if len(text) > 32 and not re.search(r"[A-Za-z0-9.]", text):
        return ""
    if not looks_like_named_source_candidate(text):
        return ""
    return text


def add_source_hit(
    hits: list[dict[str, Any]],
    seen: set[str],
    *,
    canonical: str,
    category: str,
    source_type: str,
    position: int,
    display: str = "",
) -> None:
    value = canonical.strip()
    if not value:
        return
    keys = {value.lower()}
    display_text = display.strip()
    if display_text:
        keys.add(display_text.lower())
    if any(key in seen for key in keys):
        return
    seen.update(keys)
    hits.append(
        {
            "canonical": value,
            "display": display_text or value,
            "category": category,
            "source_type": source_type,
            "position": max(0, position),
        }
    )


def detect_data_source_hits(
    text: str,
    source_defs: list[dict[str, Any]],
    *,
    include_generic_sources: bool = True,
) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for definition in source_defs:
        canonical = str(definition.get("canonical") or "")
        category = str(definition.get("category") or "")
        for regex in definition.get("patterns") or []:
            for match in regex.finditer(text):
                add_source_hit(
                    hits,
                    seen,
                    canonical=canonical,
                    category=category,
                    source_type="custom",
                    position=match.start(),
                    display=match.group(0),
                )

    url_re = re.compile(r"(?P<prefix>https?://|www\.)?(?P<domain>[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?=[/?#\s]|$)")
    for match in url_re.finditer(text):
        domain = match.group("domain").lower()
        tld = domain.rsplit(".", 1)[-1]
        if tld not in SOURCE_DOMAIN_TLDS:
            continue
        if is_stock_like_domain(domain):
            continue
        context = text[max(0, match.start() - 24) : match.end() + 24]
        if not re.search(URL_SOURCE_CONTEXT_PATTERN, context, re.IGNORECASE):
            continue
        add_source_hit(
            hits,
            seen,
            canonical=domain,
            category="URL/网站",
            source_type="url",
            position=match.start(),
            display=domain,
        )

    if include_generic_sources:
        for pattern in GENERIC_SOURCE_PATTERNS:
            regex = compile_regex(pattern, True)
            for match in regex.finditer(text):
                raw_value = match.group("value")
                for candidate in split_source_candidates(raw_value):
                    value = clean_source_candidate(candidate)
                    if not value:
                        continue
                    add_source_hit(
                        hits,
                        seen,
                        canonical=value,
                        category="表达抽取候选",
                        source_type="phrase",
                        position=match.start("value"),
                        display=value,
                    )
    return hits


def detect_data_terms(text: str) -> list[str]:
    terms: list[str] = []
    for value, pattern in DATA_TERM_DEFINITIONS:
        if re.search(pattern, text, re.IGNORECASE) and value not in terms:
            terms.append(value)
    return terms


def classify_data_intent(text: str, source_hits: list[dict[str, Any]], terms: list[str]) -> list[str]:
    categories: list[str] = []
    if source_hits:
        categories.append("指定数据源/垂类网站")
    for value, pattern in DATA_INTENT_CATEGORY_DEFINITIONS:
        if re.search(pattern, text, re.IGNORECASE) and value not in categories:
            categories.append(value)
    if not categories and terms:
        categories.append("通用数据获取/整理")
    return categories


def has_explicit_data_intent(text: str, source_hits: list[dict[str, Any]], terms: list[str]) -> bool:
    return bool(source_hits)


def add_bucket_tags(bucket: dict[str, Any], *, categories: Iterable[str], terms: Iterable[str]) -> None:
    category_counter: Counter[str] = bucket.setdefault("intent_categories", Counter())
    term_counter: Counter[str] = bucket.setdefault("data_terms", Counter())
    for category in categories:
        category_counter[category] += 1
    for term in terms:
        term_counter[term] += 1


def finalize_enriched_buckets(
    buckets: dict[str, dict[str, Any]],
    *,
    min_task_count: int,
    top_n: int,
    include_source_metadata: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, bucket in buckets.items():
        forms: Counter[str] = bucket.get("forms", Counter())
        source_types: Counter[str] = bucket.get("source_types", Counter())
        sample = bucket.get("sample") or {}
        task_count = len(bucket.get("refs") or set())
        if task_count < min_task_count:
            continue
        row = {
            "value": forms.most_common(1)[0][0] if forms else "",
            "task_count": task_count,
            "mention_count": int(bucket.get("mention_count") or 0),
            "first_seen": bucket.get("first_seen", ""),
            "last_seen": bucket.get("last_seen", ""),
            "source_types": ";".join(f"{key}:{count}" for key, count in source_types.most_common()),
            "sample_ref": sample.get("ref", ""),
        }
        if include_source_metadata:
            row["source_category"] = bucket.get("source_category", "")
            row["intent_categories"] = top_counter_text(bucket.get("intent_categories"), 5)
            row["data_terms"] = top_counter_text(bucket.get("data_terms"), 5)
        if sample.get("snippet"):
            row["sample_snippet"] = sample.get("snippet", "")
        rows.append(row)
    rows.sort(key=lambda row: (-int(row.get("task_count") or 0), str(row.get("value") or "").lower()))
    if top_n > 0:
        rows = rows[:top_n]
    return rows


def scan_x_accounts(corpus: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    text_fields = paiobs.parse_csv_values(getattr(args, "text_fields", "")) or DEFAULT_TEXT_FIELDS
    filters = corpus.get("filters", paiobs.build_filters(args))
    context_re = compile_regex(getattr(args, "context_pattern", "") or DEFAULT_X_CONTEXT_PATTERN, True)
    handle_re = re.compile(r"(?<![A-Za-z0-9_.+-])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])")
    url_re = re.compile(r"(?:https?://)?(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})(?=[/?#\s]|$)", re.IGNORECASE)
    placeholder_re = re.compile(r"^_?user_\d+$", re.IGNORECASE)
    require_context = not bool(getattr(args, "no_require_x_context", False))
    include_placeholders = bool(getattr(args, "include_placeholders", False))
    include_snippets = bool(getattr(args, "include_snippets", False))
    want_detail_csv = bool(getattr(args, "detail_csv", False))
    buckets: dict[str, dict[str, Any]] = {}
    detail_rows: list[dict[str, Any]] = []
    scanned_tasks = 0
    x_context_tasks = 0

    for item in corpus.get("items") or []:
        if not isinstance(item, dict):
            continue
        scanned_tasks += 1
        text = str(item.get("query_text") or question_text(item, text_fields))
        url_matches = list(url_re.finditer(text))
        has_context = bool(url_matches or context_re.search(text))
        if require_context and not has_context:
            continue
        x_context_tasks += 1
        task_values: list[str] = []
        for source, matches in (("@", handle_re.finditer(text)), ("url", iter(url_matches))):
            seen_in_task: set[str] = set()
            for match in matches:
                handle = match.group(1)
                lower = handle.lower()
                if lower in X_RESERVED_HANDLES:
                    continue
                if not include_placeholders and placeholder_re.match(handle):
                    continue
                if "[truncated" in text[match.end() : match.end() + 40]:
                    continue
                key = lower
                bucket = buckets.setdefault(key, {})
                display = "@" + handle
                add_match(bucket, display, source, item, match.start(), text, include_snippets)
                if display not in task_values:
                    task_values.append(display)
                seen_in_task.add(key)
        if want_detail_csv and task_values:
            detail_rows.append(
                detail_csv_row(
                    item,
                    filters=filters,
                    category="x_accounts",
                    matched_values=task_values,
                    text=text,
                )
            )

    rows = finalize_buckets(
        buckets,
        min_task_count=max(1, int(getattr(args, "min_task_count", 1) or 1)),
        top_n=int(getattr(args, "top_n", 0) or 0),
    )
    return {
        "schema_version": "query-theme-scan/v1",
        "theme": "x_accounts",
        "items": rows,
        "count": len(rows),
        "corpus_count": len(corpus.get("items") or []),
        "scanned_tasks": scanned_tasks,
        "matched_task_count": x_context_tasks,
        "filters": filters,
        "coverage": corpus.get("coverage", {}),
        "query_details": sorted(detail_rows, key=lambda row: str(row.get("request_time") or ""), reverse=True),
    }


def load_patterns(args: argparse.Namespace) -> list[str]:
    patterns = list(getattr(args, "pattern", None) or [])
    pattern_file = getattr(args, "pattern_file", "")
    if pattern_file:
        for raw_line in Path(pattern_file).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    if not patterns:
        raise SystemExit("scan-regex requires --pattern or --pattern-file")
    return patterns


def scan_regex(corpus: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    text_fields = paiobs.parse_csv_values(getattr(args, "text_fields", "")) or DEFAULT_TEXT_FIELDS
    filters = corpus.get("filters", paiobs.build_filters(args))
    ignore_case = bool(getattr(args, "ignore_case", True))
    regexes = [compile_regex(pattern, ignore_case) for pattern in load_patterns(args)]
    context_re = compile_regex(args.context_pattern, ignore_case) if getattr(args, "context_pattern", "") else None
    exclude_regexes = [compile_regex(pattern, ignore_case) for pattern in (getattr(args, "exclude_pattern", None) or [])]
    value_group = int(getattr(args, "value_group", 1) or 1)
    normalize = getattr(args, "normalize", "none")
    include_snippets = bool(getattr(args, "include_snippets", False))
    want_detail_csv = bool(getattr(args, "detail_csv", False))
    buckets: dict[str, dict[str, Any]] = {}
    detail_rows: list[dict[str, Any]] = []
    matched_tasks = 0
    scanned_tasks = 0

    for item in corpus.get("items") or []:
        if not isinstance(item, dict):
            continue
        scanned_tasks += 1
        text = str(item.get("query_text") or question_text(item, text_fields))
        if context_re and not context_re.search(text):
            continue
        if any(regex.search(text) for regex in exclude_regexes):
            continue
        task_matched = False
        task_values: list[str] = []
        for regex in regexes:
            for match in regex.finditer(text):
                try:
                    raw_value = match.group(value_group)
                except IndexError:
                    raw_value = match.group(0)
                if raw_value is None:
                    raw_value = match.group(0)
                value = normalize_value(str(raw_value), normalize)
                if not value:
                    continue
                bucket = buckets.setdefault(value if normalize == "lower" else value.lower(), {})
                add_match(bucket, value, "regex", item, match.start(), text, include_snippets)
                if value not in task_values:
                    task_values.append(value)
                task_matched = True
        if task_matched:
            matched_tasks += 1
            if want_detail_csv:
                detail_rows.append(
                    detail_csv_row(
                        item,
                        filters=filters,
                        category=getattr(args, "theme", "") or "regex",
                        matched_values=task_values,
                        text=text,
                    )
                )

    rows = finalize_buckets(
        buckets,
        min_task_count=max(1, int(getattr(args, "min_task_count", 1) or 1)),
        top_n=int(getattr(args, "top_n", 0) or 0),
    )
    return {
        "schema_version": "query-theme-scan/v1",
        "theme": getattr(args, "theme", "") or "regex",
        "patterns": load_patterns(args),
        "items": rows,
        "count": len(rows),
        "corpus_count": len(corpus.get("items") or []),
        "scanned_tasks": scanned_tasks,
        "matched_task_count": matched_tasks,
        "filters": filters,
        "coverage": corpus.get("coverage", {}),
        "query_details": sorted(detail_rows, key=lambda row: str(row.get("request_time") or ""), reverse=True),
    }


def scan_data_intent(corpus: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    text_fields = paiobs.parse_csv_values(getattr(args, "text_fields", "")) or DEFAULT_TEXT_FIELDS
    filters = corpus.get("filters", paiobs.build_filters(args))
    ignore_case = True
    exclude_regexes = [compile_regex(pattern, ignore_case) for pattern in (getattr(args, "exclude_pattern", None) or [])]
    include_snippets = bool(getattr(args, "include_snippets", False))
    include_generic_sources = not bool(getattr(args, "no_generic_sources", False))
    want_detail_csv = bool(getattr(args, "detail_csv", False))
    source_defs = compiled_source_definitions(args)

    source_buckets: dict[str, dict[str, Any]] = {}
    category_buckets: dict[str, dict[str, Any]] = {}
    term_buckets: dict[str, dict[str, Any]] = {}
    detail_rows: list[dict[str, Any]] = []
    scanned_tasks = 0
    matched_tasks = 0
    source_matched_tasks = 0

    for item in corpus.get("items") or []:
        if not isinstance(item, dict):
            continue
        scanned_tasks += 1
        text = str(item.get("query_text") or question_text(item, text_fields))
        if any(regex.search(text) for regex in exclude_regexes):
            continue
        source_hits = detect_data_source_hits(text, source_defs, include_generic_sources=include_generic_sources)
        terms = detect_data_terms(text)
        if not has_explicit_data_intent(text, source_hits, terms):
            continue
        categories = classify_data_intent(text, source_hits, terms)
        matched_tasks += 1
        if source_hits:
            source_matched_tasks += 1

        matched_values: list[str] = []
        for hit in source_hits:
            value = str(hit.get("canonical") or "")
            if not value:
                continue
            key = value.lower()
            bucket = source_buckets.setdefault(key, {"source_category": hit.get("category", "")})
            if not bucket.get("source_category"):
                bucket["source_category"] = hit.get("category", "")
            add_match(
                bucket,
                value,
                str(hit.get("source_type") or "source"),
                item,
                int(hit.get("position") or 0),
                text,
                include_snippets,
            )
            add_bucket_tags(bucket, categories=categories, terms=terms)
            matched_values.append(f"source:{value}")

        for category in categories:
            bucket = category_buckets.setdefault(category.lower(), {})
            add_match(bucket, category, "classifier", item, 0, text, include_snippets)
            matched_values.append(f"category:{category}")

        for term in terms:
            bucket = term_buckets.setdefault(term.lower(), {})
            add_match(bucket, term, "term", item, 0, text, include_snippets)
            matched_values.append(f"term:{term}")

        if want_detail_csv:
            detail_rows.append(
                detail_csv_row(
                    item,
                    filters=filters,
                    category="data_intent",
                    matched_values=matched_values,
                    text=text,
                )
            )

    min_task_count = max(1, int(getattr(args, "min_task_count", 1) or 1))
    top_n = int(getattr(args, "top_n", 0) or 0)
    source_rows = finalize_enriched_buckets(
        source_buckets,
        min_task_count=min_task_count,
        top_n=top_n,
        include_source_metadata=True,
    )
    category_rows = finalize_buckets(category_buckets, min_task_count=min_task_count, top_n=top_n)
    term_rows = finalize_buckets(term_buckets, min_task_count=min_task_count, top_n=top_n)
    payload = {
        "schema_version": "query-theme-data-intent/v1",
        "theme": "data_intent",
        "items": source_rows,
        "source_demands": source_rows,
        "intent_categories": category_rows,
        "data_terms": term_rows,
        "count": len(source_rows),
        "source_count": len(source_rows),
        "category_count": len(category_rows),
        "term_count": len(term_rows),
        "corpus_count": len(corpus.get("items") or []),
        "scanned_tasks": scanned_tasks,
        "matched_task_count": matched_tasks,
        "source_matched_task_count": source_matched_tasks,
        "filters": filters,
        "coverage": corpus.get("coverage", {}),
        "query_details": sorted(detail_rows, key=lambda row: str(row.get("request_time") or ""), reverse=True),
    }
    payload["summary"] = build_data_intent_summary(payload)
    return payload


def iter_output_rows(payload: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    items = payload.get("items") or []
    if payload.get("theme") == "data_intent":
        keys = [
            "value",
            "source_category",
            "task_count",
            "mention_count",
            "intent_categories",
            "data_terms",
            "first_seen",
            "last_seen",
            "source_types",
            "sample_ref",
        ]
    else:
        keys = ["value", "task_count", "mention_count", "first_seen", "last_seen", "source_types", "sample_ref"]
    if any(isinstance(item, dict) and "sample_snippet" in item for item in items):
        keys.append("sample_snippet")
    rows = [[item.get(key, "") for key in keys] for item in items if isinstance(item, dict)]
    return keys, rows


def format_top_values(rows: list[dict[str, Any]], limit: int = 5) -> str:
    values: list[str] = []
    for row in rows[:limit]:
        value = str(row.get("value") or "")
        if not value:
            continue
        values.append(f"{value}（{row.get('task_count', 0)}）")
    return "、".join(values)


def build_data_intent_summary(payload: dict[str, Any]) -> list[str]:
    matched_count = int(payload.get("matched_task_count") or 0)
    corpus_count = int(payload.get("corpus_count") or 0)
    source_count = int(payload.get("source_matched_task_count") or 0)
    source_rows = [row for row in payload.get("source_demands") or [] if isinstance(row, dict)]
    category_rows = [row for row in payload.get("intent_categories") or [] if isinstance(row, dict)]
    term_rows = [row for row in payload.get("data_terms") or [] if isinstance(row, dict)]
    if matched_count <= 0:
        return ["本窗口未命中明确点名外部数据源、数据库、行业网站、资讯平台或 URL 的 query。"]

    lines = [
        f"本窗口从 {corpus_count} 条网关候选 query 中检出 {matched_count} 条明确点名数据来源的 query，其中 {source_count} 条命中可抽取的数据源、数据库、行业网站、资讯平台或 URL 名称。",
    ]
    top_sources = format_top_values(source_rows)
    if top_sources:
        lines.append(f"被点名的数据源/网站主要集中在：{top_sources}。")
    else:
        lines.append("本窗口未集中出现可由规则抽取的命名数据源/网站。")

    top_categories = format_top_values(category_rows)
    if top_categories:
        lines.append(f"需求类型 Top 项为：{top_categories}。")

    top_terms = format_top_values(term_rows)
    if top_terms:
        lines.append(f"用户反复提到的数据主题/指标包括：{top_terms}。")

    lines.append("该统计只按 query 原文中的显式命名来源命中；候选数据源/网站仍需后续 agent 或人工复核，泛泛的数据需求、隐含研究意图、仅在 answer 或工具入参中出现的数据源不计入。")
    return lines


def render_data_intent_markdown(payload: dict[str, Any]) -> str:
    coverage = payload.get("coverage") or {}
    lines = [
        "# PaiWork Query Named Data Source Intent Scan",
        "",
        f"- Corpus tasks: {payload.get('corpus_count', 0)}",
        f"- Matched named-source tasks: {payload.get('matched_task_count', 0)}",
        f"- Tasks with extracted sources/sites: {payload.get('source_matched_task_count', 0)}",
        f"- Unique extracted sources/sites: {payload.get('source_count', 0)}",
    ]
    if coverage:
        lines.append(f"- Gateway requests: {coverage.get('query_count', '')}")
        warnings = coverage.get("warnings") or []
        if warnings:
            lines.append(f"- Warnings: {'; '.join(str(item) for item in warnings)}")

    lines.extend(["", "## 用户数据需求总结", ""])
    for index, item in enumerate(payload.get("summary") or build_data_intent_summary(payload), start=1):
        lines.append(f"{index}. {item}")

    source_headers = ["点名数据源/网站", "抽取方式", "query 数", "提及次数", "需求类型", "数据主题", "样例 ref"]
    source_rows = [
        [
            row.get("value", ""),
            row.get("source_category", ""),
            row.get("task_count", ""),
            row.get("mention_count", ""),
            row.get("intent_categories", ""),
            row.get("data_terms", ""),
            row.get("sample_ref", ""),
        ]
        for row in payload.get("source_demands") or []
        if isinstance(row, dict)
    ]
    lines.extend(["", "## 点名数据源/网站需求 TopN", "", paiobs.markdown_table(source_rows, source_headers), ""])

    category_headers = ["需求类型", "query 数", "提及次数", "样例 ref"]
    category_rows = [
        [row.get("value", ""), row.get("task_count", ""), row.get("mention_count", ""), row.get("sample_ref", "")]
        for row in payload.get("intent_categories") or []
        if isinstance(row, dict)
    ]
    lines.extend(["## 数据意向分类 TopN", "", paiobs.markdown_table(category_rows, category_headers), ""])

    term_headers = ["数据主题/指标", "query 数", "提及次数", "样例 ref"]
    term_rows = [
        [row.get("value", ""), row.get("task_count", ""), row.get("mention_count", ""), row.get("sample_ref", "")]
        for row in payload.get("data_terms") or []
        if isinstance(row, dict)
    ]
    lines.extend(["## 数据主题/指标 TopN", "", paiobs.markdown_table(term_rows, term_headers), ""])
    return "\n".join(lines)


def render_markdown(payload: dict[str, Any]) -> str:
    if payload.get("theme") == "data_intent":
        return render_data_intent_markdown(payload)
    headers, rows = iter_output_rows(payload)
    coverage = payload.get("coverage") or {}
    lines = [
        f"# Query Theme Scan: {payload.get('theme', '')}",
        "",
        f"- Corpus tasks: {payload.get('corpus_count', 0)}",
        f"- Matched tasks: {payload.get('matched_task_count', 0)}",
        f"- Unique values: {payload.get('count', 0)}",
    ]
    if coverage:
        lines.append(f"- Gateway requests: {coverage.get('query_count', '')}")
        warnings = coverage.get("warnings") or []
        if warnings:
            lines.append(f"- Warnings: {'; '.join(str(item) for item in warnings)}")
    lines.extend(["", paiobs.markdown_table(rows, headers), ""])
    return "\n".join(lines)


def output_payload(payload: dict[str, Any], args: argparse.Namespace, *, default_format: str = "json") -> None:
    fmt = getattr(args, "format", None) or default_format
    output = getattr(args, "output", None)
    if getattr(args, "detail_csv", False):
        text = detail_csv_text(list(payload.get("query_details") or []))
    elif fmt == "json":
        text = paiobs.json_dumps(payload) + "\n"
    elif fmt == "jsonl":
        text = "".join(paiobs.json_dumps(item, compact=True) + "\n" for item in payload.get("items") or [])
    elif fmt == "markdown":
        text = render_markdown(payload)
    elif fmt in {"table", "pretty"}:
        headers, rows = iter_output_rows(payload)
        max_cell_chars = getattr(args, "max_cell_chars", None)
        if max_cell_chars is None:
            max_cell_chars = 120 if fmt == "pretty" else 0
        text = paiobs.table(rows, headers, style="pretty" if fmt == "pretty" else "csv", max_cell_chars=max_cell_chars)
    else:
        text = paiobs.json_dumps(payload) + "\n"

    if output:
        Path(output).write_text(text, encoding="utf-8-sig" if getattr(args, "detail_csv", False) else "utf-8")
    else:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")


def output_corpus(payload: dict[str, Any], args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "jsonl")
    output = getattr(args, "output", None)
    if fmt == "jsonl":
        text = "".join(paiobs.json_dumps(item, compact=True) + "\n" for item in payload.get("items") or [])
    elif fmt == "json":
        text = paiobs.json_dumps(payload) + "\n"
    elif fmt in {"table", "pretty"}:
        text = paiobs.render_table_payload(
            {"schema_version": "question-search/v1", "items": payload.get("items") or []},
            style="pretty" if fmt == "pretty" else "csv",
            max_cell_chars=getattr(args, "max_cell_chars", None),
        )
    else:
        text = render_markdown(
            {
                "theme": "corpus",
                "corpus_count": payload.get("count", 0),
                "matched_task_count": payload.get("count", 0),
                "count": payload.get("count", 0),
                "items": [],
                "coverage": payload.get("coverage", {}),
            }
        )
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")


def cmd_collect(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = collect_corpus(client, args)
    output_corpus(payload, args)


def cmd_x_accounts(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    corpus = get_or_collect_corpus(client, args)
    payload = scan_x_accounts(corpus, args)
    output_payload(payload, args, default_format=args.format)


def cmd_scan_regex(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    corpus = get_or_collect_corpus(client, args)
    payload = scan_regex(corpus, args)
    output_payload(payload, args, default_format=args.format)


def cmd_data_intent(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    if getattr(args, "input", None) or getattr(args, "local_collect", False):
        corpus = get_or_collect_corpus(client, args)
    else:
        corpus = gateway_theme_search_corpus(client, args, theme_rules=DATA_INTENT_THEME_RULES)
    payload = scan_data_intent(corpus, args)
    output_payload(payload, args, default_format=args.format)


def add_scan_common_args(parser: argparse.ArgumentParser) -> None:
    add_corpus_args(parser)
    parser.add_argument("--top-n", type=int, default=0, help="Keep only the top N values; 0 keeps all.")
    parser.add_argument("--min-task-count", type=int, default=1)
    parser.add_argument("--include-snippets", action="store_true", help="Include one short query-text snippet per value.")
    add_scan_output_args(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect query-text corpora and run local theme scans")
    add_common_runtime_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Collect a complete question-text corpus with adaptive time slicing")
    add_corpus_args(collect)
    collect.add_argument("--format", choices=["json", "jsonl", "table", "pretty", "markdown"], default="jsonl")
    collect.add_argument("--max-cell-chars", type=int, default=None)
    collect.add_argument("-o", "--output")
    collect.set_defaults(func=cmd_collect)

    x_accounts = sub.add_parser("x-accounts", help="Extract X/Twitter handles from query text")
    add_scan_common_args(x_accounts)
    x_accounts.add_argument("--context-pattern", default=DEFAULT_X_CONTEXT_PATTERN)
    x_accounts.add_argument("--no-require-x-context", action="store_true", help="Count @handles even when X/Twitter is not mentioned.")
    x_accounts.add_argument("--include-placeholders", action="store_true", help="Keep platform placeholders such as @_user_1.")
    x_accounts.set_defaults(func=cmd_x_accounts)

    scan_regex_parser = sub.add_parser("scan-regex", help="Extract values from query text with local regex patterns")
    add_scan_common_args(scan_regex_parser)
    scan_regex_parser.add_argument("--theme", default="regex")
    scan_regex_parser.add_argument("--pattern", action="append", default=[], help="Regex pattern. Repeatable.")
    scan_regex_parser.add_argument("--pattern-file", default="", help="Text file with one regex per line.")
    scan_regex_parser.add_argument("--context-pattern", default="", help="Only scan tasks whose query text matches this regex.")
    scan_regex_parser.add_argument("--exclude-pattern", action="append", default=[], help="Skip tasks whose query text matches this regex.")
    scan_regex_parser.add_argument("--value-group", type=int, default=1, help="Capture group used as the counted value; falls back to full match.")
    scan_regex_parser.add_argument("--ignore-case", action=argparse.BooleanOptionalAction, default=True)
    scan_regex_parser.add_argument("--normalize", choices=["none", "lower"], default="none")
    scan_regex_parser.set_defaults(func=cmd_scan_regex)

    data_intent = sub.add_parser("data-intent", aliases=["data-intents"], help="Extract queries that explicitly name requested data sources/sites")
    add_scan_common_args(data_intent)
    data_intent.add_argument("--source", action="append", default=[], help="Optional custom source/site candidate to count. Repeatable.")
    data_intent.add_argument("--source-file", default="", help="Text file with one optional custom source/site candidate per line.")
    data_intent.add_argument("--no-generic-sources", action="store_true", help="Only count custom --source/--source-file values and URL sources; skip generic named-source extraction.")
    data_intent.add_argument("--exclude-pattern", action="append", default=[], help="Skip tasks whose query text matches this regex.")
    data_intent.add_argument("--max-items", type=int, default=500, help="Maximum gateway-side theme-search candidates to return before local summarization.")
    data_intent.add_argument("--theme-search-profile", choices=["lite", "summary", "raw"], default="lite", help="Gateway theme-search result profile.")
    data_intent.add_argument("--local-collect", action="store_true", help="Use the old full-corpus collect path instead of gateway-side theme search.")
    data_intent.set_defaults(func=cmd_data_intent)

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
