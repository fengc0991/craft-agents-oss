#!/usr/bin/env python3.11
"""Query-oriented CLI for PaiWork Observability Gateway tasks.

This script owns historical question/task lookup.  It deliberately stays on the
gateway route: every search, mining request, context fetch, and export goes
through /api/internal/v1 or the gateway file proxy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import sys
from typing import Any

import paiobs


KEYWORD_BUCKETS: dict[str, dict[str, Any]] = {
    "satisfaction": {
        "kind": "goodcase",
        "description": "用户明确满意、认可结果或表达强正反馈",
        "keywords": [
            "不错",
            "很好",
            "太棒了",
            "漂亮",
            "优秀",
            "好用",
            "做的好",
            "做得好",
            "非常好",
            "666",
            "满意",
            "就是这个",
            "厉害",
            "很赞",
            "越来越完美",
            "准确",
            "牛",
        ],
        "exclude_keywords": ["不准确", "准确吗", "准确性", "核对准确", "准确来说"],
    },
    "complaint": {
        "kind": "complaint",
        "description": "用户抱怨、追责、表达不满",
        "keywords": [
            "不满意",
            "很不满意",
            "不太行",
            "还是不对",
            "还是不行",
            "还是错",
            "你错了",
            "错了",
            "没有按照",
            "没按照",
            "不是我要的",
            "不是要求",
            "不是让你",
            "怎么没",
            "为什么没",
            "没有完成",
            "没完成",
            "没做",
            "没有做",
            "不符合",
            "不是说",
            "太差",
            "什么鬼",
            "离谱",
            "乱写",
            "胡说",
            "胡说八道",
            "瞎写",
            "瞎编",
            "编造",
            "差评",
            "投诉",
            "浪费",
            "浪费时间",
            "浪费算力",
            "绝望",
            "崩溃",
            "垃圾",
            "废物",
            "太烂",
            "很烂",
            "烂透",
            "烂死",
            "恶心",
            "坑爹",
            "太坑",
            "很坑",
            "真坑",
            "一坨",
            "shit",
            "bullshit",
            "crap",
            "garbage",
            "trash",
            "terrible",
            "awful",
            "useless",
            "stupid",
            "idiot",
            "sucks",
            "wtf",
            "WTF",
            "fuck",
            "fucking",
            "damn",
            "wrong again",
            "still wrong",
            "this is wrong",
            "not what I asked",
            "didn't follow",
            "doesn't follow",
            "waste of time",
        ],
        "exclude_keywords": [
            "我搞错了",
            "是不是说",
            "不是说明",
            "把不符合",
            "垃圾资产",
            "垃圾焚烧",
            "垃圾分类",
            "垃圾处理",
            "垃圾发电",
            "客户投诉",
            "投诉纠纷",
            "好评 差评",
            "好评/差评",
            "差评比例",
            "差评方向",
        ],
    },
    "pointed_error": {
        "kind": "badcase",
        "description": "用户指出具体错误、数据错误、计算错误或格式错误",
        "keywords": [
            "格式不对",
            "格式有问题",
            "数据不对",
            "数据有误",
            "数字不对",
            "不对啊",
            "写错了",
            "搞错了",
            "算错了",
            "不准确",
            "有误",
            "引用错",
            "来源错",
            "算的不对",
            "公式错",
            "表格错",
            "没按格式",
            "漏了",
        ],
        "exclude_keywords": ["我搞错了", "是否有误", "有没有误差"],
    },
    "inability": {
        "kind": "inability",
        "description": "AI 表示无能力、无权限、暂不支持或建议用户手动处理",
        "keywords": [
            "无法",
            "没有权限",
            "暂不支持",
            "不支持",
            "做不到",
            "无权",
            "无法访问",
            "无法操作",
            "功能暂未",
            "尚不支持",
            "未开放",
            "超出我的能力",
            "没有该功能",
            "建议您手动",
            "需要您自行",
            "未开通",
            "无可用",
            "不覆盖",
            "无法下载",
            "无法生成",
            "没有数据",
            "查不到",
        ],
    },
    "continue": {
        "kind": "goodcase",
        "description": "用户要求继续，通常代表深度投入或对上轮结果可继续加工",
        "keywords": ["继续", "接着", "继续做", "继续执行", "往下做"],
    },
    "new_requirement": {
        "kind": "new_requirement",
        "description": "用户提出新功能、新数据源、新自动化或交付方式诉求",
        "keywords": [
            "能不能",
            "可不可以",
            "希望支持",
            "增加",
            "新增",
            "接入",
            "打通",
            "自动",
            "定时",
            "推送",
            "导出",
            "生成",
            "支持",
        ],
    },
}


def task_key(item: dict[str, Any]) -> str:
    ref = item.get("query_ref") if isinstance(item.get("query_ref"), dict) else item
    session_id = str(ref.get("session_id") or item.get("session_id") or "")
    task_index = str(ref.get("task_index") or item.get("task_index") or "")
    question_id = str(ref.get("question_id") or item.get("question_id") or "")
    raw = f"{session_id}:{task_index}:{question_id}"
    return raw if raw.strip(":") else hashlib.sha1(paiobs.json_dumps(item, compact=True).encode("utf-8")).hexdigest()


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


def compact_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def filter_window(filters: dict[str, Any]) -> tuple[str, str]:
    return str(filters.get("start_time") or ""), str(filters.get("end_time") or "")


def item_ref(item: dict[str, Any]) -> dict[str, Any]:
    ref = item.get("query_ref") if isinstance(item.get("query_ref"), dict) else item
    return ref if isinstance(ref, dict) else {}


def joined_values(value: Any) -> str:
    if isinstance(value, list):
        return "|".join(str(item) for item in value if item not in (None, ""))
    if isinstance(value, tuple | set):
        return "|".join(str(item) for item in value if item not in (None, ""))
    return str(value or "")


def query_text_from_item(item: dict[str, Any]) -> str:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    for key in (
        "user_text",
        "question",
        "latest_question",
        "first_question",
        "question_text",
        "question_preview",
        "match_preview",
        "answer_excerpt",
    ):
        source = evidence if key in evidence else item
        value = source.get(key) if isinstance(source, dict) else None
        if isinstance(value, str) and value.strip():
            return compact_text(value)
    return ""


def query_detail_row(item: dict[str, Any], *, filters: dict[str, Any], category: str = "") -> dict[str, Any]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    ref = item_ref(item)
    session_id = str(ref.get("session_id") or item.get("session_id") or "")
    task_index = str(ref.get("task_index") or item.get("task_index") or "")
    question_id = str(ref.get("question_id") or item.get("question_id") or "")
    start_time, end_time = filter_window(filters)
    matched_keywords = (
        evidence.get("matched_keywords")
        or item.get("matched_keywords")
        or item.get("matched_bucket_keyword")
        or ""
    )
    categories = category or item.get("matched_bucket") or item.get("kind") or ""
    return {
        "window_start": start_time,
        "window_end": end_time,
        "request_time": item.get("request_time") or evidence.get("request_time") or evidence.get("time") or "",
        "categories": categories,
        "user_name": item.get("user_name") or evidence.get("user_name") or item.get("user") or "",
        "institution": item.get("institution") or evidence.get("institution") or item.get("user_institution") or "",
        "status": item.get("status") or evidence.get("status") or "",
        "severity": item.get("severity") or "",
        "confidence": item.get("confidence") or "",
        "matched_keywords": joined_values(matched_keywords),
        "session_id": session_id,
        "task_index": task_index,
        "question_id": question_id,
        "ref": f"{session_id}:{task_index}" if session_id or task_index else "",
        "feedback_text": query_text_from_item(item),
    }


def detail_csv_text(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=DETAIL_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def payload_detail_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    if not filters and isinstance(payload.get("query"), dict):
        query_filters = payload["query"].get("filters")
        if isinstance(query_filters, dict):
            filters = query_filters
    category = payload.get("bucket") or payload.get("theme") or ""
    if not category and payload.get("schema_version") == "keyword-search/v1":
        category = "keyword_search"
    rows = []
    for item in payload.get("items") or []:
        if isinstance(item, dict):
            rows.append(query_detail_row(item, filters=filters, category=str(category or "")))
    rows.sort(key=lambda row: str(row.get("request_time") or ""), reverse=True)
    return rows


def write_text_output(text: str, args: argparse.Namespace, *, encoding: str = "utf-8") -> None:
    out = getattr(args, "output", None)
    if out:
        paiobs.Path(out).write_text(text, encoding=encoding)
    else:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")


def search_questions(
    client: paiobs.PaiObsClient,
    *,
    filters: dict[str, Any],
    profile: str = "lite",
    limit: int = 20,
    cursor: str = "",
) -> dict[str, Any]:
    body = {
        "env": client.env,
        "filters": filters,
        "page": {"limit": limit, "cursor": cursor or ""},
        "profile": profile,
    }
    return client.request("POST", "/history/questions/search", body=body)


def collect_search_items(
    client: paiobs.PaiObsClient,
    *,
    filters: dict[str, Any],
    profile: str = "lite",
    limit: int = 100,
    page_limit: int = 100,
) -> list[dict[str, Any]]:
    """Collect items with cursor pagination when the gateway exposes it."""

    remaining = max(1, int(limit or 1))
    cursor = ""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    while remaining > 0:
        payload = search_questions(
            client,
            filters=filters,
            profile=profile,
            limit=min(page_limit, remaining),
            cursor=cursor,
        )
        batch = payload.get("items") if isinstance(payload, dict) else []
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            key = task_key(item)
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            remaining -= 1
            if remaining <= 0:
                break
        next_cursor = str(payload.get("next_cursor") or payload.get("cursor") or "").strip() if isinstance(payload, dict) else ""
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return items


def merge_keyword_search(
    client: paiobs.PaiObsClient,
    *,
    base_filters: dict[str, Any],
    keywords: list[str],
    profile: str,
    limit_per_keyword: int,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    keyword_counts: dict[str, int] = {}
    for keyword in keywords:
        filters = dict(base_filters)
        filters["keyword"] = keyword
        payload = search_questions(client, filters=filters, profile=profile, limit=limit_per_keyword)
        count = 0
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = task_key(item)
            if key in seen:
                continue
            seen.add(key)
            enriched = dict(item)
            enriched["matched_bucket_keyword"] = keyword
            items.append(enriched)
            count += 1
        keyword_counts[keyword] = count
    return {
        "schema_version": "keyword-search/v1",
        "env": client.env,
        "filters": base_filters,
        "keywords": keywords,
        "keyword_counts": keyword_counts,
        "count": len(items),
        "items": items,
    }


def mine_keyword_bucket(
    client: paiobs.PaiObsClient,
    *,
    bucket_name: str,
    bucket: dict[str, Any],
    filters: dict[str, Any],
    limit: int,
    with_context: bool,
) -> dict[str, Any]:
    body = {
        "env": client.env,
        "kind": bucket.get("kind") or bucket_name,
        "filters": filters,
        "rules": {
            "include_keywords": bucket.get("keywords") or [],
            "exclude_keywords": bucket.get("exclude_keywords") or [],
        },
        "sample": {"limit": limit},
        "with_context": with_context,
    }
    payload = client.request("POST", "/mining/cases", body=body)
    if isinstance(payload, dict):
        payload.setdefault("bucket", bucket_name)
        payload.setdefault("bucket_description", bucket.get("description"))
    return payload


def render_keyword_table(payload: dict[str, Any], *, style: str, max_cell_chars: int | None) -> str:
    rows = []
    for index, item in enumerate(payload.get("items") or [], 1):
        ref = item.get("query_ref") if isinstance(item.get("query_ref"), dict) else item
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        rows.append(
            [
                index,
                payload.get("bucket") or item.get("matched_bucket") or "",
                item.get("matched_bucket_keyword") or evidence.get("matched_keywords") or "",
                item.get("kind") or "",
                item.get("severity") or "",
                item.get("status") or "",
                item.get("request_time") or evidence.get("time") or "",
                item.get("user_name") or item.get("user") or "",
                item.get("institution") or item.get("user_institution") or "",
                f"{ref.get('session_id')}:{ref.get('task_index')}",
                evidence.get("user_text") or item.get("question") or item.get("question_preview") or "",
            ]
        )
    return paiobs.table(
        rows,
        ["#", "bucket", "keyword", "kind", "severity", "status", "request_time", "user", "institution", "ref", "evidence"],
        style=style,
        max_cell_chars=120 if max_cell_chars is None and style == "pretty" else (max_cell_chars or 0),
    )


def output_query_payload(payload: Any, args: argparse.Namespace, *, default_format: str = "json") -> None:
    fmt = getattr(args, "format", None) or default_format
    if getattr(args, "detail_csv", False) and isinstance(payload, dict):
        write_text_output(detail_csv_text(payload_detail_rows(payload)), args, encoding="utf-8-sig")
        return
    if isinstance(payload, dict) and payload.get("schema_version") == "keyword-buckets/v1" and fmt in {"table", "pretty"}:
        rows = [
            [
                item.get("bucket"),
                item.get("kind"),
                item.get("description"),
                ",".join(item.get("keywords") or []),
                ",".join(item.get("exclude_keywords") or []),
            ]
            for item in payload.get("buckets") or []
            if isinstance(item, dict)
        ]
        text = paiobs.table(
            rows,
            ["bucket", "kind", "description", "keywords", "exclude_keywords"],
            style="pretty" if fmt == "pretty" else "csv",
            max_cell_chars=getattr(args, "max_cell_chars", None) or 0,
        )
        out = getattr(args, "output", None)
        if out:
            paiobs.Path(out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
            if text and not text.endswith("\n"):
                sys.stdout.write("\n")
        return
    if isinstance(payload, dict) and payload.get("schema_version") == "keyword-search/v1" and fmt in {"table", "pretty"}:
        text = render_keyword_table(
            payload,
            style="pretty" if fmt == "pretty" else "csv",
            max_cell_chars=getattr(args, "max_cell_chars", None),
        )
        out = getattr(args, "output", None)
        if out:
            paiobs.Path(out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
            if text and not text.endswith("\n"):
                sys.stdout.write("\n")
        return
    paiobs.output_payload(payload, args, default_format=default_format)


def cmd_search(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    filters = paiobs.build_filters(args)
    keywords = paiobs.parse_csv_values(getattr(args, "keywords", ""))
    if keywords:
        payload = merge_keyword_search(
            client,
            base_filters=filters,
            keywords=keywords,
            profile=args.profile,
            limit_per_keyword=args.limit_per_keyword,
        )
        output_query_payload(payload, args, default_format=args.format)
        return
    payload = search_questions(client, filters=filters, profile=args.profile, limit=args.limit, cursor=args.cursor)
    output_query_payload(payload, args, default_format=args.format)


def cmd_keyword_buckets(_: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    payload = {
        "schema_version": "keyword-buckets/v1",
        "buckets": [
            {
                "bucket": name,
                "kind": spec.get("kind"),
                "description": spec.get("description"),
                "keywords": spec.get("keywords") or [],
                "exclude_keywords": spec.get("exclude_keywords") or [],
            }
            for name, spec in KEYWORD_BUCKETS.items()
        ],
    }
    output_query_payload(payload, args, default_format=args.format)


def cmd_keyword_search(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    bucket = KEYWORD_BUCKETS.get(args.bucket)
    if not bucket:
        raise SystemExit(f"unknown bucket: {args.bucket}; choices={', '.join(KEYWORD_BUCKETS)}")
    filters = paiobs.build_filters(args)
    if args.via == "mine":
        payload = mine_keyword_bucket(
            client,
            bucket_name=args.bucket,
            bucket=bucket,
            filters=filters,
            limit=args.limit,
            with_context=args.with_context,
        )
        output_query_payload(payload, args, default_format=args.format)
        return
    payload = merge_keyword_search(
        client,
        base_filters=filters,
        keywords=list(bucket.get("keywords") or []),
        profile=args.profile,
        limit_per_keyword=args.limit_per_keyword,
    )
    payload["bucket"] = args.bucket
    payload["bucket_description"] = bucket.get("description")
    output_query_payload(payload, args, default_format=args.format)


def cmd_session(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_session(client, args)


def cmd_trace(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_trace(client, args)


def cmd_task(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_task(client, args)


def cmd_context(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_context(client, args)


def cmd_bundle(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_bundle(client, args)


def cmd_inspect(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_inspect(client, args)


def cmd_files(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_files(client, args)


def cmd_skills(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_skills(client, args)


def cmd_file_preview(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_file_preview(client, args)


def cmd_file_download(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_file_download(client, args)


def cmd_skill_content(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_skill_content(client, args)


def cmd_batch_context(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_batch_context(client, args)


def cmd_batch(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_batch(client, args)


def cmd_mine(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    if not getattr(args, "detail_csv", False):
        paiobs.cmd_mine(client, args)
        return
    rules: dict[str, Any] = {}
    include = paiobs.parse_csv_values(args.include_keywords)
    exclude = paiobs.parse_csv_values(args.exclude_keywords)
    if include:
        rules["include_keywords"] = include
    if exclude:
        rules["exclude_keywords"] = exclude
    if args.min_duration_seconds:
        rules["min_duration_seconds"] = args.min_duration_seconds
    filters = paiobs.build_filters(args)
    body = {
        "env": client.env,
        "kind": args.kind,
        "filters": filters,
        "rules": rules,
        "sample": {"limit": args.limit},
        "with_context": args.with_context,
    }
    payload = client.request("POST", "/mining/cases", body=body)
    if isinstance(payload, dict):
        payload.setdefault("filters", filters)
        payload.setdefault("bucket", args.kind)
    output_query_payload(payload, args, default_format=args.format)


def cmd_export(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_export(client, args)


def cmd_download(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    paiobs.cmd_download(client, args)


def add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--gateway-profile", default=None, help="Gateway profile: release/prod -> 30100, local/test/debug -> localhost:6193")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env", default=None)
    parser.add_argument("--timeout", default=None)
    parser.add_argument("--file-auth-token", default=None, help="Optional PaiWork auth token for /api/files preview/content")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PaiWork task query CLI")
    add_common_runtime_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="Search historical questions with WebUI-compatible filters")
    paiobs.add_filter_args(search)
    search.add_argument("--profile", default="lite", choices=["lite", "summary", "raw"])
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--cursor", default="")
    search.add_argument("--keywords", default="", help="Comma-separated OR keywords; runs one gateway search per keyword and de-dupes refs")
    search.add_argument("--limit-per-keyword", type=int, default=50)
    search.add_argument("--detail-csv", action="store_true", help="Write unified query-detail CSV columns instead of the normal payload/table")
    paiobs.add_output_args(search, default_format="table")
    search.set_defaults(func=cmd_search)

    buckets = sub.add_parser("keyword-buckets", help="List built-in keyword buckets")
    paiobs.add_output_args(buckets, default_format="json")
    buckets.set_defaults(func=cmd_keyword_buckets)

    bucket_search = sub.add_parser("keyword-search", help="Search a built-in keyword bucket")
    bucket_search.add_argument("bucket", choices=sorted(KEYWORD_BUCKETS))
    paiobs.add_filter_args(bucket_search)
    bucket_search.add_argument("--via", choices=["search", "mine"], default="search", help="search=question keyword search; mine=case mining endpoint")
    bucket_search.add_argument("--profile", default="lite", choices=["lite", "summary", "raw"])
    bucket_search.add_argument("--limit", type=int, default=100)
    bucket_search.add_argument("--limit-per-keyword", type=int, default=50)
    bucket_search.add_argument("--with-context", action="store_true")
    bucket_search.add_argument("--detail-csv", action="store_true", help="Write unified query-detail CSV columns for matched queries/cases")
    paiobs.add_output_args(bucket_search, default_format="table")
    bucket_search.set_defaults(func=cmd_keyword_search)

    session = sub.add_parser("session", help="List queries in one session, ordered by task_index")
    session.add_argument("session_id")
    session.add_argument("--profile", default="summary", choices=["lite", "summary", "raw"])
    session.add_argument("--limit", type=int, default=100)
    paiobs.add_output_args(session, default_format="table")
    session.set_defaults(func=cmd_session)

    trace = sub.add_parser("trace", help="Trace current and previous queries in the same session")
    trace.add_argument("session_id")
    trace.add_argument("task_index", type=int)
    trace.add_argument("--profile", default="summary", choices=["lite", "summary", "raw"])
    trace.add_argument("--limit", type=int, default=100)
    trace.add_argument("--with-context", action="store_true")
    trace.add_argument("--context-profile", default="summary", choices=["summary", "context", "qa", "full"])
    trace.add_argument("--max-items", type=int, default=20)
    paiobs.add_output_args(trace, default_format="table")
    trace.set_defaults(func=cmd_trace)

    task = sub.add_parser("task", help="Load one task payload")
    task.add_argument("session_id")
    task.add_argument("task_index", type=int)
    task.add_argument("--profile", default="summary", choices=["summary", "context", "qa", "full"])
    paiobs.add_output_args(task)
    task.set_defaults(func=cmd_task)

    context = sub.add_parser("context", help="Load QueryAgentTabs context")
    context.add_argument("session_id")
    context.add_argument("task_index", type=int)
    paiobs.add_output_args(context)
    context.set_defaults(func=cmd_context)

    bundle = sub.add_parser("bundle", help="Download one task context bundle zip")
    bundle.add_argument("session_id")
    bundle.add_argument("task_index", type=int)
    bundle.add_argument("-o", "--output")
    bundle.set_defaults(func=cmd_bundle)

    inspect = sub.add_parser("inspect", help="Inspect one task from context or a downloaded data pack")
    paiobs.add_context_source_args(inspect)
    inspect.add_argument("--process-limit", type=int, default=120)
    paiobs.add_output_args(inspect, default_format="table")
    inspect.set_defaults(func=cmd_inspect)

    files = sub.add_parser("files", help="List input/process/result files from context or a data pack")
    paiobs.add_context_source_args(files)
    paiobs.add_output_args(files, default_format="table")
    files.set_defaults(func=cmd_files)

    skills = sub.add_parser("skills", help="List mentioned/read skills from context or a data pack")
    paiobs.add_context_source_args(skills)
    paiobs.add_output_args(skills, default_format="table")
    skills.set_defaults(func=cmd_skills)

    file_preview = sub.add_parser("file-preview", help="Preview a remote text file referenced by a task data pack")
    paiobs.add_file_proxy_args(file_preview)
    file_preview.add_argument("--local-path", default="")
    paiobs.add_output_args(file_preview)
    file_preview.set_defaults(func=cmd_file_preview)

    file_download = sub.add_parser("file-download", help="Download a remote file referenced by a task data pack")
    paiobs.add_file_proxy_args(file_download)
    file_download.add_argument("-o", "--output")
    file_download.set_defaults(func=cmd_file_download)

    skill_content = sub.add_parser("skill-content", help="Read full skill content from local roots or the gateway")
    skill_content.add_argument("skill_name")
    skill_content.add_argument("--skill-root", action="append", default=[])
    skill_content.add_argument("--user-id", "--owner-user-id", dest="user_id", default="")
    skill_content.add_argument("--remote", action="store_true")
    skill_content.add_argument("--max-chars", type=int, default=40000)
    skill_content.add_argument("--include-files", action=argparse.BooleanOptionalAction, default=True)
    skill_content.add_argument("--file-path", action="append", default=[])
    paiobs.add_output_args(skill_content)
    skill_content.set_defaults(func=cmd_skill_content)

    batch = sub.add_parser("batch", help="Load batch task payloads by refs or search filters")
    paiobs.add_items_args(batch)
    paiobs.add_filter_args(batch)
    batch.add_argument("--profile", default="summary", choices=["summary", "context", "qa", "full"])
    batch.add_argument("--max-items", type=int, default=20)
    batch.add_argument("--max-concurrency", type=int, default=8)
    batch.add_argument("--include-search-item", action="store_true")
    paiobs.add_output_args(batch)
    batch.set_defaults(func=cmd_batch)

    batch_context = sub.add_parser("batch-context", help="Compatibility alias for loading batch task payloads")
    paiobs.add_items_args(batch_context)
    batch_context.add_argument("--profile", default="context", choices=["summary", "context", "qa", "full"])
    batch_context.add_argument("--max-items", type=int, default=20)
    batch_context.add_argument("--max-concurrency", type=int, default=8)
    paiobs.add_output_args(batch_context)
    batch_context.set_defaults(func=cmd_batch_context)

    mine = sub.add_parser("mine", help="Mine case candidates")
    paiobs.add_filter_args(mine)
    mine.add_argument("kind", choices=["badcase", "goodcase", "complaint", "new_requirement", "inability", "cost_outlier", "tool_failure"])
    mine.add_argument("--include-keywords", default="")
    mine.add_argument("--exclude-keywords", default="")
    mine.add_argument("--min-duration-seconds", type=float, default=0)
    mine.add_argument("--with-context", action="store_true")
    mine.add_argument("--limit", type=int, default=50)
    mine.add_argument("--detail-csv", action="store_true", help="Write unified query-detail CSV columns for mined cases")
    paiobs.add_output_args(mine, default_format="table")
    mine.set_defaults(func=cmd_mine)

    export = sub.add_parser("export", help="Create an export")
    paiobs.add_filter_args(export)
    export.add_argument("type", choices=["search_json", "search_jsonl", "search_csv", "context_zip"])
    paiobs.add_items_args(export)
    export.add_argument("--limit", type=int, default=100)
    export.add_argument("--max-items", type=int, default=20)
    export.add_argument("--download-to")
    paiobs.add_output_args(export)
    export.set_defaults(func=cmd_export)

    download = sub.add_parser("download", help="Download an export by id")
    download.add_argument("export_id")
    download.add_argument("-o", "--output")
    download.set_defaults(func=cmd_download)

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
