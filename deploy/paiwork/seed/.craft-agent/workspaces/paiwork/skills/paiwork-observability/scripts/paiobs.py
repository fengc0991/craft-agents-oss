#!/usr/bin/env python3.11
"""CLI for the PaiWork Observability Gateway internal API."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_LOCAL_BASE_URL = "http://localhost:6193"
DEFAULT_RELEASE_BASE_URL = "http://192.168.15.57:30100"
DEFAULT_GATEWAY_PROFILE = "release"
DEFAULT_BASE_URL = DEFAULT_RELEASE_BASE_URL
GATEWAY_PROFILE_ALIASES = {
    "local": "local",
    "localhost": "local",
    "test": "local",
    "testing": "local",
    "debug": "local",
    "dev": "local",
    "release": "release",
    "publish": "release",
    "published": "release",
    "prod": "release",
    "product": "release",
    "production": "release",
}
GATEWAY_PROFILE_BASE_URLS = {
    "local": DEFAULT_LOCAL_BASE_URL,
    "release": DEFAULT_RELEASE_BASE_URL,
}
INTERNAL_PREFIX = "/api/internal/v1"
SKILL_DIR = Path(__file__).resolve().parents[1]
LOCAL_CONFIG_PATHS = [
    SKILL_DIR / ".paiobs.env",
    SKILL_DIR / "paiobs.env",
]
FILTER_KEYS = [
    "session_id",
    "question_id",
    "keyword",
    "answer_keyword",
    "user_id",
    "username",
    "institution",
    "entry_scene",
    "status",
    "scheduled",
    "end_type",
    "is_web_search",
    "start_time",
    "end_time",
    "institution_nature",
    "inst_type",
    "product_type",
    "user_type",
    "user_role",
]
QUERY_INFO_KEYS = [
    "session_id",
    "task_index",
    "question_id",
    "feedback_question_id",
    "request_time",
    "response_time",
    "status",
    "success",
    "env",
    "target",
    "history_db",
    "entry_scene",
    "user_id",
    "user_name",
    "user_institution",
    "institution",
    "institution_nature",
    "inst_type",
    "product_type",
    "user_type",
    "user_role",
    "duration_seconds",
    "total_elapsed",
    "average_time",
    "question",
]
FILE_NAME_KEYS = ["file_name", "fileName", "name", "title"]
FILE_PATH_KEYS = [
    "content_path",
    "contentPath",
    "resolved_content_path",
    "resolvedContentPath",
    "file_path",
    "filePath",
    "remote_path",
    "remotePath",
    "path",
]
RAW_FILE_PATH_KEYS = [
    "raw_file_path",
    "rawFilePath",
    "full_file_path",
    "fullFilePath",
    "nas_file_path",
    "nasFilePath",
]
FILE_ID_KEYS = ["file_id", "fileId"]
FILE_TYPE_KEYS = ["file_type", "fileType", "mime_type", "mimeType", "content_type", "contentType"]
SKILL_TITLE_RE = re.compile(r"读取技能\s*[:：]\s*([A-Za-z0-9_.@/-]+)")
SKILL_CONTENT_MAX_FILES = 80
SKILL_CONTENT_TEXT_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
DURATION_STATS_DEFAULT_METRICS = [
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
DURATION_STATS_DEFAULT_GROUP_BY = ["status", "entry_scene"]
DURATION_STATS_DEFAULT_SLOW_DIMENSIONS: list[str] = []
TASK_STATS_DEFAULT_MAX_TASKS = int(os.environ.get("PAI_OBS_TASK_STATS_DEFAULT_MAX_TASKS", "100000"))


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload


class PaiObsClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        env: str = "",
        timeout: float = 60,
        file_auth_token: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.env = env.strip()
        self.timeout = timeout
        self.file_auth_token = file_auth_token.strip()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, Any] | None = None,
        binary: bool = False,
        internal: bool = True,
    ) -> Any:
        query = {k: v for k, v in (query or {}).items() if v not in (None, "", [], {})}
        url = self.base_url + (INTERNAL_PREFIX if internal else "") + path
        if query:
            url += "?" + urlencode(query, doseq=True)

        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if internal and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif not internal and self.file_auth_token:
            headers["Authorization"] = self.file_auth_token

        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content = response.read()
                if binary:
                    return content, dict(response.headers)
                if not content:
                    return None
                return json.loads(content.decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read()
            payload = _decode_json(raw)
            message = _error_message(exc.code, payload, raw)
            raise ApiError(exc.code, message, payload) from exc
        except URLError as exc:
            raise ApiError(0, f"request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(0, "request timed out") from exc


def _decode_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _error_message(status: int, payload: Any, raw: bytes) -> str:
    if isinstance(payload, dict):
        pieces = [str(payload.get("error") or payload.get("message") or f"HTTP {status}")]
        if payload.get("required_scope"):
            pieces.append(f"required_scope={payload.get('required_scope')}")
        if payload.get("hint"):
            pieces.append(str(payload.get("hint")))
        return " | ".join(piece for piece in pieces if piece)
    text = raw.decode("utf-8", errors="replace").strip()
    return text or f"HTTP {status}"


def json_dumps(value: Any, compact: bool = False) -> str:
    if compact:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, indent=2)


def read_json_value(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    text = value.strip()
    path_text = text[1:] if text.startswith("@") else text
    path = Path(path_text)
    if text.startswith("@") or path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(text)


def read_json_file(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_csv_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace("|", ",").split(",") if item.strip()]


def split_path_values(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in re.split(r"[,;\n]+", value) if item.strip()]


def _strip_env_value(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def load_skill_local_config() -> dict[str, str]:
    config: dict[str, str] = {}
    for path in LOCAL_CONFIG_PATHS:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            if name.startswith("PAI_OBS_"):
                config[name] = _strip_env_value(value)
    return config


def api_key_from_config(config: dict[str, str]) -> str:
    key = config.get("PAI_OBS_API_KEY", "").strip()
    if key:
        return key
    api_keys = config.get("PAI_OBS_API_KEYS", "").strip()
    if not api_keys:
        return ""
    first_entry = api_keys.split(";", 1)[0].strip()
    if ":" not in first_entry:
        return first_entry
    return first_entry.split(":", 2)[1].strip()


def normalize_gateway_profile(value: str | None) -> str:
    profile = (value or DEFAULT_GATEWAY_PROFILE).strip().lower()
    resolved = GATEWAY_PROFILE_ALIASES.get(profile)
    if not resolved:
        choices = ", ".join(sorted(GATEWAY_PROFILE_ALIASES))
        raise SystemExit(f"invalid gateway profile '{value}', expected one of: {choices}")
    return resolved


def gateway_profile_from_config(args: argparse.Namespace, config: dict[str, str]) -> str:
    raw_profile = (
        getattr(args, "gateway_profile", None)
        or os.environ.get("PAI_OBS_GATEWAY_PROFILE")
        or config.get("PAI_OBS_GATEWAY_PROFILE")
        or os.environ.get("PAI_OBS_PROFILE")
        or config.get("PAI_OBS_PROFILE")
        or DEFAULT_GATEWAY_PROFILE
    )
    return normalize_gateway_profile(raw_profile)


def base_url_from_config(args: argparse.Namespace, config: dict[str, str]) -> str:
    explicit_base_url = (
        getattr(args, "base_url", None)
        or os.environ.get("PAI_OBS_BASE_URL")
        or config.get("PAI_OBS_BASE_URL")
    )
    if explicit_base_url:
        return explicit_base_url
    profile = gateway_profile_from_config(args, config)
    return GATEWAY_PROFILE_BASE_URLS[profile]


def parse_items(args: argparse.Namespace) -> list[dict[str, Any]]:
    items = read_json_value(getattr(args, "items_json", ""), default=None)
    if items is None:
        items = []
    if isinstance(items, dict) and isinstance(items.get("items"), list):
        items = items["items"]
    if not isinstance(items, list):
        raise SystemExit("--items-json must be a JSON list or an object with an items list")

    refs = getattr(args, "refs", None) or []
    for ref in refs:
        if ":" not in ref:
            raise SystemExit(f"invalid ref '{ref}', expected session_id:task_index")
        session_id, task_index = ref.rsplit(":", 1)
        items.append({"session_id": session_id, "task_index": int(task_index)})
    return items


def build_filters(args: argparse.Namespace) -> dict[str, Any]:
    filters = {}
    extra = read_json_value(getattr(args, "filters_json", ""), default={})
    if extra:
        if not isinstance(extra, dict):
            raise SystemExit("--filters-json must be a JSON object")
        filters.update(extra)
    for key in FILTER_KEYS:
        value = getattr(args, key, None)
        if value not in (None, "", [], {}):
            filters[key] = value
    return filters


def build_client(args: argparse.Namespace) -> PaiObsClient:
    local_config = load_skill_local_config()
    timeout = float(getattr(args, "timeout", None) or os.environ.get("PAI_OBS_TIMEOUT") or local_config.get("PAI_OBS_TIMEOUT") or 60)
    return PaiObsClient(
        base_url=base_url_from_config(args, local_config),
        api_key=getattr(args, "api_key", None) or os.environ.get("PAI_OBS_API_KEY") or api_key_from_config(local_config),
        env=getattr(args, "env", None) or os.environ.get("PAI_OBS_ENV") or local_config.get("PAI_OBS_ENV") or "product",
        timeout=timeout,
        file_auth_token=(
            getattr(args, "file_auth_token", None)
            or os.environ.get("PAI_OBS_FILE_AUTH_TOKEN")
            or os.environ.get("PAI_FILE_AUTH_TOKEN")
            or local_config.get("PAI_OBS_FILE_AUTH_TOKEN")
            or ""
        ),
    )


def normalize_cell(value: Any) -> str:
    return "" if value is None else str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def clip(value: Any, limit: int = 80) -> str:
    text = normalize_cell(value)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def csv_table(rows: list[list[Any]], headers: list[str], *, max_cell_chars: int = 0) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([normalize_cell(header) for header in headers])
    for row in rows:
        writer.writerow([clip(cell, max_cell_chars) for cell in row])
    return buffer.getvalue().rstrip("\r\n")


def pretty_table(rows: list[list[Any]], headers: list[str], *, max_cell_chars: int = 120) -> str:
    text_rows = [[clip(cell, max_cell_chars) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in text_rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    sep = "  ".join("-" * width for width in widths)
    body = [
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        for row in text_rows
    ]
    return "\n".join([line, sep, *body])


def table(rows: list[list[Any]], headers: list[str], *, style: str = "csv", max_cell_chars: int = 0) -> str:
    if style == "pretty":
        limit = 120 if max_cell_chars is None else max_cell_chars
        return pretty_table(rows, headers, max_cell_chars=limit)
    limit = 0 if max_cell_chars is None else max_cell_chars
    return csv_table(rows, headers, max_cell_chars=limit)


def markdown_cell(value: Any) -> str:
    text = normalize_cell(value)
    return text.replace("|", "\\|")


def markdown_table(rows: list[list[Any]], headers: list[str]) -> str:
    output = [
        "| " + " | ".join(markdown_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        output.append("| " + " | ".join(markdown_cell(cell) for cell in row) + " |")
    return "\n".join(output)


def safe_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def format_number(value: Any, digits: int = 1) -> str:
    number = safe_number(value)
    if number is None:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.{digits}f}"


def format_percent(value: Any, digits: int = 2) -> str:
    number = safe_number(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}%"


def ratio_percent(numerator: Any, denominator: Any, digits: int = 2) -> str:
    num = safe_number(numerator)
    den = safe_number(denominator)
    if num is None or den in (None, 0):
        return ""
    return f"{(num / den * 100):.{digits}f}%"


def dimension_label(dimension: str) -> str:
    labels = {
        "status": "状态",
        "entry_scene": "入口场景",
        "scheduled": "调度类型",
        "is_web_search": "是否联网搜索",
        "query_language": "query 语言",
        "web_query_language": "联网 query 语言",
        "end_type": "结束类型",
        "user": "用户",
        "user_id": "用户 ID",
        "institution": "机构",
        "institution_nature": "机构性质",
        "inst_type": "机构类型",
        "product_type": "产品类型",
        "user_type": "用户类型",
        "user_role": "用户角色",
        "model": "模型",
        "tool_type": "工具类型",
        "tool_name": "工具",
        "skill": "Skill",
        "data_source_type": "数据源类型",
        "source_provider": "来源平台",
        "source_domain": "来源域名",
        "source_title": "来源标题",
        "source_id": "来源 ID",
        "file_type": "文件类型",
        "file_role": "文件角色",
        "day": "日期",
        "hour": "小时",
    }
    return labels.get(dimension, dimension)


def detail_dimension_description(dimension: str) -> str:
    descriptions = {
        "data_source_type": (
            "`data_source_type` 统计任务过程中引用的数据/内容类型，例如 web、report、comment、edb、ann、roadshow、social_media。"
            "它回答“用了什么类别的数据”。"
        ),
        "source_provider": (
            "`source_provider` 统计来源平台或发布方，例如 今日头条、新浪财经、东方财富网。"
            "它回答“来自哪个平台/渠道”。"
        ),
        "source_domain": "`source_domain` 统计可解析 URL 的域名，适合看外部网页来源分布。",
        "source_title": "`source_title` 统计来源标题或文档标题，适合抽样定位高频具体材料。",
        "source_id": "`source_id` 统计来源记录 ID 或 URL，适合追溯具体来源对象。",
        "file_type": (
            "`file_type` 统计任务上下文、子任务产物、用户提及文件和文件变更中的文件/材料类型，"
            "例如 report、web、file、md、xlsx、docx、png、generated_report。它回答“任务读写/携带了什么类型的文件或材料”，"
            "不是数据源平台统计。"
        ),
        "file_role": "`file_role` 统计文件在任务里的角色，例如 current_file、mentioned_file、file_change、subtask_file。",
    }
    return descriptions.get(dimension, "")


USER_ROLE_LABELS = {
    "2": "销售",
    "3": "首席分析师",
    "4": "分析师",
    "5": "基金经理",
    "6": "研究员",
    "unknown": "未知角色",
}
USER_ROLE_NAMES = {"销售", "首席分析师", "分析师", "基金经理", "研究员", "未知角色"}


def normalize_dimension_value(dimension: str, value: Any) -> Any:
    if dimension != "user_role":
        return value
    text = str(value or "").strip()
    if not text:
        return "未知角色"
    if text in USER_ROLE_NAMES:
        return text
    return USER_ROLE_LABELS.get(text, text)


def normalize_group_values(group: dict[str, Any]) -> None:
    for key, value in list(group.items()):
        group[key] = normalize_dimension_value(str(key), value)


def merge_numeric_bucket(target: dict[str, Any], source: dict[str, Any], *, count_key: str) -> None:
    target_weight = safe_number(target.get(count_key)) or 0
    source_weight = safe_number(source.get(count_key)) or 0
    total_weight = target_weight + source_weight

    for key, value in source.items():
        if not key.startswith("avg_"):
            continue
        source_number = safe_number(value)
        if source_number is None:
            continue
        target_number = safe_number(target.get(key))
        if target_number is None or target_weight <= 0:
            target[key] = source_number
        elif total_weight > 0:
            target[key] = round((target_number * target_weight + source_number * source_weight) / total_weight, 6)

    for key, value in source.items():
        if key in {"value", "group", "ratio", "success_rate"} or key.startswith("avg_"):
            continue
        source_number = safe_number(value)
        target_number = safe_number(target.get(key))
        if source_number is not None and target_number is not None and (
            key == "count" or key.endswith("_count") or key in {"task_count", "occurrence_count"}
        ):
            total = target_number + source_number
            target[key] = int(total) if float(total).is_integer() else round(total, 6)
        elif target.get(key) in (None, "") and value not in (None, ""):
            target[key] = value

    success = safe_number(target.get("success_count"))
    total = safe_number(target.get("question_count") if "question_count" in target else target.get("count"))
    if ("success_rate" in target or "success_rate" in source) and success is not None and total:
        target["success_rate"] = round(success / total, 6)


def merge_aggregate_items(payload: dict[str, Any]) -> None:
    merged: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        group = item.get("group")
        if isinstance(group, dict):
            normalize_group_values(group)
        key = json.dumps(group or {}, ensure_ascii=False, sort_keys=True)
        if key not in merged:
            merged[key] = item
            ordered.append(item)
            continue
        target_metrics = merged[key].setdefault("metrics", {})
        source_metrics = item.get("metrics") or {}
        if isinstance(target_metrics, dict) and isinstance(source_metrics, dict):
            merge_numeric_bucket(target_metrics, source_metrics, count_key="question_count")
    if ordered:
        payload["items"] = ordered


def normalize_facet_values(facets: dict[str, Any], *, total_count: Any = None) -> None:
    for dimension, items in facets.items():
        merged: dict[str, dict[str, Any]] = {}
        ordered: list[dict[str, Any]] = []
        for item in items or []:
            if isinstance(item, dict) and "value" in item:
                item["value"] = normalize_dimension_value(str(dimension), item.get("value"))
                key = str(item.get("value"))
                if key not in merged:
                    merged[key] = item
                    ordered.append(item)
                    continue
                count_key = "task_count" if "task_count" in item else "count"
                merge_numeric_bucket(merged[key], item, count_key=count_key)
        total = safe_number(total_count)
        if total:
            for item in ordered:
                count = safe_number(item.get("task_count") if "task_count" in item else item.get("count"))
                if count is not None:
                    item["ratio"] = round(count / total, 6)
        if ordered:
            facets[dimension] = ordered


def normalize_analytics_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    schema = payload.get("schema_version")
    if schema == "analytics-aggregate/v1":
        merge_aggregate_items(payload)
    elif schema == "analytics-facets/v1":
        facets = payload.get("facets")
        if isinstance(facets, dict):
            normalize_facet_values(facets, total_count=payload.get("total_count"))
    elif schema == "task-stats/v1":
        facets = payload.get("facets")
        if isinstance(facets, dict):
            normalize_facet_values(facets, total_count=payload.get("total_count"))
    elif schema == "duration-stats/v1":
        normalize_analytics_payload(payload.get("overall"))
        for item in (payload.get("breakdowns") or {}).values():
            normalize_analytics_payload(item)
        normalize_analytics_payload(payload.get("slow_buckets"))
    elif schema == "token-stats/v1":
        normalize_analytics_payload(payload.get("task_stats"))
    elif schema == "credit-stats/v1":
        normalize_analytics_payload(payload.get("task_stats"))
    elif schema == "stats-overview/v1":
        for item in (payload.get("aggregate") or {}).values():
            normalize_analytics_payload(item)
        normalize_analytics_payload(payload.get("task_stats"))
        normalize_analytics_payload(payload.get("token_stats"))
        normalize_analytics_payload(payload.get("credit_stats"))
    elif schema == "stats-compare/v1":
        normalize_analytics_payload(payload.get("current"))
        normalize_analytics_payload(payload.get("previous"))
    return payload


def filter_summary(filters: dict[str, Any]) -> str:
    if not filters:
        return "未设置过滤条件"
    preferred = [
        "start_time",
        "end_time",
        "status",
        "entry_scene",
        "scheduled",
        "username",
        "user_id",
        "institution",
        "keyword",
        "answer_keyword",
    ]
    keys = [key for key in preferred if key in filters]
    keys.extend(key for key in filters if key not in keys)
    return ", ".join(f"{key}={filters[key]}" for key in keys)


def render_markdown_payload(payload: Any, args: argparse.Namespace) -> str:
    if not isinstance(payload, dict):
        return "```json\n" + json_dumps(payload) + "\n```\n"
    schema = payload.get("schema_version", "")
    if schema == "task-stats/v1":
        return render_task_stats_markdown(payload, args)
    if schema == "duration-stats/v1":
        return render_duration_stats_markdown(payload, args)
    if schema == "token-stats/v1":
        return render_token_stats_markdown(payload, args)
    if schema == "credit-stats/v1":
        return render_credit_stats_markdown(payload, args)
    if schema == "analytics-aggregate/v1":
        return render_aggregate_markdown(payload, args)
    if schema == "analytics-facets/v1":
        return render_facets_markdown(payload, args)
    return "```json\n" + json_dumps(payload) + "\n```\n"


def render_aggregate_markdown(payload: dict[str, Any], args: argparse.Namespace) -> str:
    rows = []
    for item in payload.get("items") or []:
        group = item.get("group") or {}
        metrics = item.get("metrics") or {}
        rows.append(
            [
                ", ".join(f"{key}={value}" for key, value in group.items()) or "ALL",
                ", ".join(f"{key}={value}" for key, value in metrics.items()),
            ]
        )
    lines = [
        "# PaiWork Aggregate Report",
        "",
        f"- 过滤条件：{filter_summary(payload.get('filters') or build_filters(args))}",
        f"- 返回分组数：{len(rows)}",
        "",
        markdown_table(rows, ["分组", "指标"]) if rows else "无数据",
    ]
    return "\n".join(lines) + "\n"


def aggregate_group_label(group: dict[str, Any]) -> str:
    if not group:
        return "ALL"
    return ", ".join(f"{dimension_label(str(key))}={value}" for key, value in group.items())


def aggregate_group_value(item: dict[str, Any]) -> str:
    group = item.get("group") or {}
    if len(group) == 1:
        return str(next(iter(group.values())))
    return aggregate_group_label(group)


def format_success_rate(value: Any) -> str:
    number = safe_number(value)
    if number is None:
        return ""
    return format_percent(number * 100)


def ended_count(metrics: dict[str, Any]) -> int:
    total = safe_number(metrics.get("question_count"))
    running = safe_number(metrics.get("running_count"))
    if total is not None and running is not None:
        return max(0, int(total) - int(running))
    success = safe_number(metrics.get("success_count")) or 0
    failed = safe_number(metrics.get("failed_count")) or 0
    return max(0, int(success) + int(failed))


def ended_success_rate(metrics: dict[str, Any]) -> float | None:
    ended = ended_count(metrics)
    if ended <= 0:
        return None
    success = safe_number(metrics.get("success_count")) or 0
    return success / ended


def format_ended_success_rate(metrics: dict[str, Any]) -> str:
    rate = ended_success_rate(metrics)
    return format_success_rate(rate)


def duration_metric_row(label: str, metrics: dict[str, Any]) -> list[Any]:
    return [
        label,
        metrics.get("question_count", ""),
        metrics.get("success_count", ""),
        metrics.get("failed_count", ""),
        metrics.get("running_count", ""),
        format_ended_success_rate(metrics),
        format_number(metrics.get("avg_duration")),
        format_number(metrics.get("p50_duration")),
        format_number(metrics.get("p90_duration")),
        format_number(metrics.get("p95_duration")),
    ]


def duration_metric_headers(first_column: str = "分组") -> list[str]:
    return [first_column, "query 数", "成功", "失败", "运行中", "已结束成功率", "平均耗时", "P50", "P90", "P95"]


def render_duration_stats_markdown(payload: dict[str, Any], args: argparse.Namespace) -> str:
    slow_dimensions = payload.get("slow_dimensions") or []
    slow_min_occurrences = int(payload.get("slow_min_occurrences") or 3)
    slow_top_n = int(payload.get("slow_top_n") or 10)
    warnings = payload.get("warnings") or []
    lines = [
        "# PaiWork Duration Stats Report",
        "",
        f"- 过滤条件：{filter_summary(payload.get('filters') or {})}",
        f"- 聚合拆分：{', '.join(dimension_label(str(item)) for item in (payload.get('group_by') or [])) or '无'}",
        f"- 慢项维度：{', '.join(dimension_label(str(item)) for item in slow_dimensions) or '无'}",
        "- 耗时单位：秒",
    ]
    if warnings:
        lines.append(f"- Warnings：{'; '.join(str(item) for item in warnings)}")

    overall_rows = []
    for item in (payload.get("overall") or {}).get("items") or []:
        overall_rows.append(duration_metric_row(aggregate_group_label(item.get("group") or {}), item.get("metrics") or {}))
    lines.extend(
        [
            "",
            "## 总体",
            "",
            markdown_table(overall_rows, duration_metric_headers()) if overall_rows else "无数据",
        ]
    )

    for dimension, aggregate_payload in (payload.get("breakdowns") or {}).items():
        rows = []
        for item in aggregate_payload.get("items") or []:
            rows.append(duration_metric_row(aggregate_group_value(item), item.get("metrics") or {}))
        lines.extend(
            [
                "",
                f"## 按{dimension_label(str(dimension))}",
                "",
                markdown_table(rows, duration_metric_headers("取值")) if rows else "无数据",
            ]
        )

    slow_payload = payload.get("slow_buckets") or {}
    slow_facets = slow_payload.get("facets") or {}
    if slow_facets:
        lines.extend(["", "## 慢项", ""])
        lines.append(f"以下按平均耗时倒序，保留引用次数 >= {slow_min_occurrences} 的取值，每个维度最多展示 {slow_top_n} 项。")
        for dimension in slow_dimensions:
            items = [
                item
                for item in slow_facets.get(dimension) or []
                if safe_number(item.get("avg_duration")) is not None
                and int(item.get("occurrence_count") or item.get("count") or 0) >= slow_min_occurrences
            ]
            items = sorted(items, key=lambda item: safe_number(item.get("avg_duration")) or 0, reverse=True)[:slow_top_n]
            rows = [
                [
                    item.get("value", ""),
                    item.get("task_count", item.get("count", "")),
                    item.get("occurrence_count", item.get("count", "")),
                    item.get("success_count", ""),
                    item.get("failed_count", ""),
                    format_number(item.get("avg_duration")),
                ]
                for item in items
            ]
            lines.extend(
                [
                    "",
                    f"### {dimension_label(str(dimension))}",
                    "",
                    markdown_table(rows, ["取值", "任务数", "引用次数", "成功", "失败", "平均耗时"]) if rows else "无数据",
                ]
            )
    return "\n".join(lines) + "\n"


def token_metric_headers(first_column: str = "分组") -> list[str]:
    return [
        first_column,
        "有 token 任务数",
        "总 token",
        "Prompt token",
        "Completion token",
        "缓存 token",
        "LLM 调用数",
        "平均 token/任务",
        "P50",
        "P90",
        "P95",
        "平均调用/任务",
    ]


def token_metric_row(label: str, metrics: dict[str, Any]) -> list[Any]:
    return [
        label,
        metrics.get("token_task_count", ""),
        metrics.get("total_tokens", ""),
        metrics.get("prompt_tokens", ""),
        metrics.get("completion_tokens", ""),
        metrics.get("cached_tokens", ""),
        metrics.get("llm_call_count", ""),
        format_number(metrics.get("avg_total_tokens")),
        format_number(metrics.get("p50_total_tokens")),
        format_number(metrics.get("p90_total_tokens")),
        format_number(metrics.get("p95_total_tokens")),
        format_number(metrics.get("avg_llm_calls")),
    ]


def render_token_stats_markdown(payload: dict[str, Any], args: argparse.Namespace) -> str:
    task_stats = payload.get("task_stats") if isinstance(payload.get("task_stats"), dict) else {}
    group_by = payload.get("group_by") or []
    token_dimensions = payload.get("token_dimensions") or []
    token_metrics = task_stats.get("token_metrics") if isinstance(task_stats.get("token_metrics"), dict) else {}
    warnings = payload.get("warnings") or []
    coverage = task_stats.get("coverage") if isinstance(task_stats.get("coverage"), dict) else {}
    coverage_warnings = coverage.get("warnings") or []
    if coverage_warnings:
        warnings = [*warnings, *coverage_warnings]

    lines = [
        "# PaiWork Token Stats Report",
        "",
        f"- 过滤条件：{filter_summary(payload.get('filters') or task_stats.get('filters') or {})}",
        f"- 聚合拆分：{', '.join(dimension_label(str(item)) for item in group_by) or '无'}",
        f"- Token Top 维度：{', '.join(dimension_label(str(item)) for item in token_dimensions) or '无'}",
        "- 统计对象：任务执行过程中的 LLM 调用 token usage；一个任务可能包含多次 LLM 调用。",
        "- 口径说明：`有 token 任务数` 是解析到 LLM usage 的任务数；`总 token` 为 prompt+completion 的任务级汇总后再聚合。",
    ]
    if warnings:
        lines.append(f"- Warnings：{'; '.join(str(item) for item in warnings)}")
    lines.extend(["", "## 总体", ""])
    if token_metrics:
        lines.append(markdown_table([token_metric_row("ALL", token_metrics)], token_metric_headers()))
    else:
        lines.append("当前网关未返回 token_metrics。请确认 Observability Gateway 已支持 `include_token_stats`，或升级网关后重试。")

    facets = task_stats.get("facets") if isinstance(task_stats.get("facets"), dict) else {}
    for dimension in group_by:
        rows = []
        for item in facets.get(dimension) or []:
            if not item.get("token_task_count"):
                continue
            rows.append(token_metric_row(str(item.get("value", "")), item))
        lines.extend(["", f"## 按{dimension_label(str(dimension))}", ""])
        lines.append(markdown_table(rows, token_metric_headers("取值")) if rows else "无 token 数据")

    for dimension in token_dimensions:
        rows = []
        for item in facets.get(dimension) or []:
            if not item.get("token_task_count"):
                continue
            rows.append(token_metric_row(str(item.get("value", "")), item))
        lines.extend(["", f"## Token 较高的{dimension_label(str(dimension))}", ""])
        lines.append(markdown_table(rows, token_metric_headers("取值")) if rows else "无 token 数据")
    return "\n".join(lines) + "\n"


def credit_metric_headers(first_column: str = "分组") -> list[str]:
    return [
        first_column,
        "有 credit 任务数",
        "订单数",
        "确认订单数",
        "总 credit/研究值",
        "冻结研究值",
        "平均 credit/任务",
        "P50",
        "P90",
        "P95",
    ]


def credit_metric_row(label: str, metrics: dict[str, Any]) -> list[Any]:
    return [
        label,
        metrics.get("credit_task_count", ""),
        metrics.get("order_count", ""),
        metrics.get("confirmed_order_count", ""),
        metrics.get("total_credits", metrics.get("consumed_points", "")),
        metrics.get("frozen_points", ""),
        format_number(metrics.get("avg_credits_per_task")),
        format_number(metrics.get("p50_credits")),
        format_number(metrics.get("p90_credits")),
        format_number(metrics.get("p95_credits")),
    ]


def render_credit_stats_markdown(payload: dict[str, Any], args: argparse.Namespace) -> str:
    task_stats = payload.get("task_stats") if isinstance(payload.get("task_stats"), dict) else {}
    group_by = payload.get("group_by") or []
    credit_dimensions = payload.get("credit_dimensions") or []
    credit_metrics = task_stats.get("credit_metrics") if isinstance(task_stats.get("credit_metrics"), dict) else {}
    warnings = payload.get("warnings") or []
    coverage = task_stats.get("coverage") if isinstance(task_stats.get("coverage"), dict) else {}
    coverage_warnings = coverage.get("warnings") or []
    if coverage_warnings:
        warnings = [*warnings, *coverage_warnings]

    lines = [
        "# PaiWork Credit Stats Report",
        "",
        f"- 过滤条件：{filter_summary(payload.get('filters') or task_stats.get('filters') or {})}",
        f"- 聚合拆分：{', '.join(dimension_label(str(item)) for item in group_by) or '无'}",
        f"- Credit Top 维度：{', '.join(dimension_label(str(item)) for item in credit_dimensions) or '无'}",
        "- 统计对象：`saas.saas_point_freeze_order` 中 `business_no = question_id/feedback_question_id` 的研究值订单。",
        "- 口径说明：`总 credit/研究值` 使用 `consumed_points` 汇总；未确认、释放或超时订单通常 consumed_points 为 0。",
    ]
    if credit_metrics:
        lines.append(f"- 来源表：{credit_metrics.get('source_table', '')}；关联键：{credit_metrics.get('join_key', '')}")
    if warnings:
        lines.append(f"- Warnings：{'; '.join(str(item) for item in warnings)}")
    lines.extend(["", "## 总体", ""])
    if credit_metrics:
        lines.append(markdown_table([credit_metric_row("ALL", credit_metrics)], credit_metric_headers()))
    else:
        lines.append("当前网关未返回 credit_metrics。请确认 Observability Gateway 已支持 `include_credit_stats`。")

    facets = task_stats.get("facets") if isinstance(task_stats.get("facets"), dict) else {}
    for dimension in [*group_by, *credit_dimensions]:
        rows = []
        for item in facets.get(dimension) or []:
            if not item.get("credit_task_count"):
                continue
            rows.append(credit_metric_row(str(item.get("value", "")), item))
        lines.extend(["", f"## 按{dimension_label(str(dimension))}", ""])
        lines.append(markdown_table(rows, credit_metric_headers("取值")) if rows else "无 credit 数据")
    return "\n".join(lines) + "\n"


def render_facets_markdown(payload: dict[str, Any], args: argparse.Namespace) -> str:
    lines = [
        "# PaiWork Facets Report",
        "",
        f"- 过滤条件：{filter_summary(payload.get('filters') or build_filters(args))}",
    ]
    for dimension, items in (payload.get("facets") or {}).items():
        rows = []
        for item in items or []:
            ratio = safe_number(item.get("ratio"))
            rows.append(
                [
                    item.get("value", ""),
                    item.get("count", ""),
                    format_percent(ratio * 100 if ratio is not None else None),
                    item.get("success_count", ""),
                    item.get("failed_count", ""),
                    format_number(item.get("avg_duration")),
                ]
            )
        lines.extend(
            [
                "",
                f"## {dimension_label(dimension)}",
                "",
                markdown_table(rows, ["取值", "数量", "占比", "成功", "失败", "平均耗时"]) if rows else "无数据",
            ]
        )
    return "\n".join(lines) + "\n"


def render_task_stats_markdown(payload: dict[str, Any], args: argparse.Namespace) -> str:
    facets = payload.get("facets") or {}
    dimensions = payload.get("dimensions") or list(facets)
    metrics = payload.get("metrics") or {}
    coverage = payload.get("coverage") or {}
    warnings = coverage.get("warnings") or payload.get("warnings") or []
    top_n = getattr(args, "top_n", None)

    lines = [
        "# PaiWork Task Stats Report",
        "",
        f"- 过滤条件：{filter_summary(payload.get('filters') or {})}",
        f"- 总 query 数：{payload.get('total_count', '')}",
        f"- 已扫描任务数：{payload.get('scanned_task_count', '')}",
        f"- 明细任务数：{payload.get('detail_task_count', '')}",
        (
            "- 状态快照："
            f"success={metrics.get('success_count', 0)}, "
            f"failed={metrics.get('failed_count', 0)}, "
            f"running={metrics.get('running_count', 0)}, "
            f"completed={metrics.get('completed_count', 0)}"
        ),
        f"- 覆盖情况：search={coverage.get('search', '')}, detail={coverage.get('detail', '')}",
    ]
    if top_n:
        lines.append(f"- 每个维度最多返回 Top {top_n} 个取值")
    if warnings:
        lines.append(f"- Warnings：{'; '.join(str(item) for item in warnings)}")
    lines.append("")
    lines.append(
        "说明：`任务数` 是命中该取值的去重任务数；`引用次数` 是该取值在任务执行 JSON 中出现的次数，"
        "同一任务可能引用多个来源、文件或工具，因此各 bucket 的任务数/引用次数不会相加等于总 query 数。"
        "`任务占比` 使用接口返回的 `ratio`；`引用占比` 基于当前返回 bucket 的引用次数求和，若 TopN 截断则不包含未返回长尾。"
    )
    if any(dimension in facets for dimension in ("data_source_type", "source_provider", "file_type", "file_role")):
        lines.extend(
            [
                "",
                "### 明细口径说明",
                "",
                "- 数据源相关维度来自 `history_task.source_data`、`research_step.sourceData`、`parsedFileSourceData` 等执行过程来源记录。",
                "- 文件相关维度来自 `mentioned_files`、`current_file_list`、`file_change_list`、子任务文件等任务文件记录。",
                "- `data_source_type` 与 `source_provider` 分层展示：前者是内容类型，后者是平台/渠道名，避免把 `web` 和 `今日头条` 混在同一级。",
                "- `file_type` 与数据源统计不同：它描述任务读写或携带的文件/材料类型，也会包含生成产物和工作区文件。",
            ]
        )

    for dimension in dimensions:
        items = facets.get(dimension) or []
        occurrence_total = sum(int(item.get("occurrence_count") or item.get("count") or 0) for item in items)
        top5_occurrence = sum(int(item.get("occurrence_count") or 0) for item in items[:5])
        top10_occurrence = sum(int(item.get("occurrence_count") or 0) for item in items[:10])
        lines.extend(["", f"## {dimension_label(str(dimension))}", ""])
        description = detail_dimension_description(str(dimension))
        if description:
            lines.extend([description, ""])
        if not items:
            lines.append("无数据")
            continue
        lines.extend(
            [
                f"- 返回取值数：{len(items)}",
                f"- 返回引用次数合计：{occurrence_total}",
                f"- Top5 引用集中度（返回集内）：{ratio_percent(top5_occurrence, occurrence_total)}",
                f"- Top10 引用集中度（返回集内）：{ratio_percent(top10_occurrence, occurrence_total)}",
                "",
            ]
        )

        rows = []
        for item in items:
            task_ratio = safe_number(item.get("ratio"))
            occurrence_count = item.get("occurrence_count", item.get("count", ""))
            rows.append(
                [
                    item.get("value", ""),
                    item.get("task_count", item.get("count", "")),
                    format_percent(task_ratio * 100 if task_ratio is not None else None),
                    occurrence_count,
                    ratio_percent(occurrence_count, occurrence_total),
                    item.get("success_count", ""),
                    item.get("failed_count", ""),
                    format_number(item.get("avg_duration")),
                ]
            )
        lines.append(
            markdown_table(
                rows,
                ["取值", "任务数", "任务占比", "引用次数", "引用占比", "成功", "失败", "平均耗时"],
            )
        )

        slow_items = [
            item
            for item in items
            if safe_number(item.get("avg_duration")) is not None
            and int(item.get("occurrence_count") or item.get("count") or 0) >= 3
        ]
        slow_items = sorted(slow_items, key=lambda item: safe_number(item.get("avg_duration")) or 0, reverse=True)[:5]
        if slow_items:
            lines.extend(["", "耗时较高的取值（引用次数 >= 3）：", ""])
            lines.append(
                markdown_table(
                    [
                        [
                            item.get("value", ""),
                            item.get("occurrence_count", item.get("count", "")),
                            format_number(item.get("avg_duration")),
                        ]
                        for item in slow_items
                    ],
                    ["取值", "引用次数", "平均耗时"],
                )
            )

        failed_items = [item for item in items if int(item.get("failed_count") or 0) > 0]
        if failed_items:
            lines.extend(["", "存在失败的取值：", ""])
            lines.append(
                markdown_table(
                    [
                        [
                            item.get("value", ""),
                            item.get("task_count", ""),
                            item.get("failed_count", ""),
                        ]
                        for item in failed_items
                    ],
                    ["取值", "任务数", "失败数"],
                )
            )
    return "\n".join(lines) + "\n"


def output_payload(payload: Any, args: argparse.Namespace, *, default_format: str = "json") -> None:
    payload = normalize_analytics_payload(payload)
    fmt = getattr(args, "format", None) or default_format
    out = getattr(args, "output", None)
    if fmt == "jsonl":
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            text = json_dumps(payload, compact=True) + "\n"
        else:
            text = "".join(json_dumps(item, compact=True) + "\n" for item in items)
    elif fmt == "markdown":
        text = render_markdown_payload(payload, args)
    elif fmt in ("table", "pretty"):
        style = "pretty" if fmt == "pretty" else "csv"
        text = render_table_payload(payload, style=style, max_cell_chars=getattr(args, "max_cell_chars", None))
    else:
        text = json_dumps(payload) + "\n"

    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
        if text and not text.endswith("\n"):
            sys.stdout.write("\n")


def first_text(mapping: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return ""


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_nodes(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from iter_nodes(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_nodes(child, (*path, str(index)))


def compact_path(path: tuple[str, ...]) -> str:
    return ".".join(path)


def normalize_ref_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("/workspace/"):
        return text[len("/workspace/"):]
    if text.startswith("workspace/"):
        return text[len("workspace/"):]
    return text


def extract_query_info(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") == "query-summary/v1":
        info = payload.get("query_info") if isinstance(payload.get("query_info"), dict) else {}
    elif payload.get("schema_version") == "query-qa/v1":
        info = dict(payload.get("query_info") if isinstance(payload.get("query_info"), dict) else {})
        info.setdefault("question", payload.get("question"))
    elif isinstance(payload.get("query_info"), dict):
        info = payload["query_info"]
    else:
        info = payload
    return {key: info.get(key) for key in QUERY_INFO_KEYS if info.get(key) not in (None, "", [], {})}


def agent_bundles(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("schema_version") == "query-summary/v1" and isinstance(payload.get("agent_tabs"), list):
        return [item for item in payload["agent_tabs"] if isinstance(item, dict)]
    if isinstance(payload.get("agents"), list):
        bundles = [item for item in payload["agents"] if isinstance(item, dict)]
        if payload.get("schema_version") != "query-summary/v1" or any(isinstance(item.get("files"), dict) for item in bundles):
            return bundles
    if payload.get("schema_version") == "query-summary/v1":
        return [
            {
                "folder": "main",
                "agent": {"scope": "main", "id": "main", "name": "main", "title": "Main Agent"},
                "files": {
                    "query_info": payload.get("query_info") or {},
                    "input_tab": {"data": payload.get("input") or {}},
                    "process_tab": {"data": {"progress_updates": (payload.get("process_outline") or {}).get("updates") or []}},
                    "result_tab": {"data": payload.get("result") or {}},
                    "evaluation_tab": {"data": payload.get("evaluation") or {}},
                },
            }
        ]
    return []


def main_agent_files(payload: dict[str, Any]) -> dict[str, Any]:
    bundles = agent_bundles(payload)
    if not bundles:
        return {}
    files = bundles[0].get("files")
    return files if isinstance(files, dict) else {}


def tab_data(files: dict[str, Any], tab_name: str) -> dict[str, Any]:
    tab = files.get(tab_name)
    if isinstance(tab, dict):
        data = tab.get("data")
        if isinstance(data, dict):
            return data
        return tab
    return {}


def load_pack_dir(path: str | Path) -> dict[str, Any]:
    base = Path(path)
    if not base.is_dir():
        raise SystemExit(f"pack dir not found: {base}")
    json_files = sorted(item for item in base.rglob("*.json") if item.is_file())
    return context_from_tab_jsons({str(item.relative_to(base)): read_json_file(item) for item in json_files})


def load_pack_zip(path: str | Path) -> dict[str, Any]:
    bundle = Path(path)
    if not bundle.is_file():
        raise SystemExit(f"bundle not found: {bundle}")
    with zipfile.ZipFile(bundle) as zf:
        names = sorted(name for name in zf.namelist() if name.endswith(".json") and not name.endswith("/"))
        for name in names:
            if name.endswith("context.json") or name.endswith("query_agent_tabs.json"):
                candidate = json.loads(zf.read(name).decode("utf-8"))
                if isinstance(candidate, dict) and candidate.get("schema_version") == "query-agent-tabs/v1":
                    return candidate
        return context_from_tab_jsons({name: json.loads(zf.read(name).decode("utf-8")) for name in names})


def context_from_tab_jsons(items: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for name, payload in items.items():
        if not isinstance(payload, dict):
            continue
        parts = Path(name).parts
        if not parts:
            continue
        folder = parts[0] if len(parts) > 1 else "main"
        stem = Path(parts[-1]).stem
        if stem not in {"query_info", "input_tab", "process_tab", "result_tab", "evaluation_tab"}:
            continue
        grouped.setdefault(folder, {})[stem] = payload

    if not grouped:
        raise SystemExit("no query-agent-tabs JSON files found in pack")

    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        folder, _ = item
        return (0 if folder == "main" else 1, folder)

    agents = []
    root_info = {}
    for folder, files in sorted(grouped.items(), key=sort_key):
        query_info = files.get("query_info") if isinstance(files.get("query_info"), dict) else {}
        if folder == "main":
            root_info = query_info
        agent = query_info.get("agent") if isinstance(query_info.get("agent"), dict) else {}
        if not agent:
            for tab_name in ("input_tab", "process_tab", "result_tab", "evaluation_tab"):
                tab = files.get(tab_name)
                if isinstance(tab, dict) and isinstance(tab.get("agent"), dict):
                    agent = tab["agent"]
                    break
        agents.append({"folder": folder, "agent": agent, "files": files})

    context = {key: value for key, value in root_info.items() if key != "agent"}
    context.setdefault("schema_version", "query-agent-tabs/v1")
    context["agents"] = agents
    return context


def load_context_source(client: PaiObsClient, args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "context_json", None):
        payload = read_json_file(args.context_json)
    elif getattr(args, "pack_dir", None):
        payload = load_pack_dir(args.pack_dir)
    elif getattr(args, "bundle", None):
        payload = load_pack_zip(args.bundle)
    elif getattr(args, "session_id", None) and getattr(args, "task_index", None):
        payload = client.request(
            "GET",
            f"/history/sessions/{args.session_id}/tasks/{args.task_index}/context",
            query={"env": client.env},
        )
    else:
        raise SystemExit("provide session_id task_index, --context-json, --pack-dir, or --bundle")
    if not isinstance(payload, dict):
        raise SystemExit("context source must be a JSON object")
    return payload


def extract_files(payload: dict[str, Any]) -> list[dict[str, Any]]:
    query_info = extract_query_info(payload)
    target = query_info.get("target") or ""
    user_id = query_info.get("user_id") or ""
    files = []
    seen = set()
    for path, node in iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        file_path = first_text(node, FILE_PATH_KEYS)
        raw_file_path = first_text(node, RAW_FILE_PATH_KEYS)
        file_id = first_text(node, FILE_ID_KEYS)
        name = first_text(node, FILE_NAME_KEYS)
        if not any((file_path, raw_file_path, file_id, name)):
            continue
        if not any(key in node for key in [*FILE_PATH_KEYS, *RAW_FILE_PATH_KEYS, *FILE_ID_KEYS]):
            continue
        key = normalize_ref_path(raw_file_path or file_path or file_id or name).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        roles = node.get("roles") if isinstance(node.get("roles"), list) else []
        files.append(
            {
                "name": name or os.path.basename(normalize_ref_path(file_path or raw_file_path).rstrip("/")),
                "file_path": file_path,
                "raw_file_path": raw_file_path,
                "file_id": file_id,
                "file_type": first_text(node, FILE_TYPE_KEYS),
                "roles": roles,
                "target": first_text(node, ["target"]) or target,
                "owner_user_id": first_text(node, ["owner_user_id", "ownerUserId", "nas_user_id", "nasUserId"]) or user_id,
                "source_path": compact_path(path),
            }
        )
    return files


def detail_entries_map(entries: Any) -> dict[str, str]:
    result = {}
    for item in as_list(entries):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("name") or "").strip()
        value = item.get("value")
        if label and value not in (None, "", [], {}):
            result[label] = str(value)
    return result


def stable_json_key(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value)


def compact_tool_entry(entry: dict[str, Any], *, path: str = "", owner: dict[str, Any] | None = None) -> dict[str, Any]:
    owner = owner or {}
    result = {
        "path": path,
        "subtask_id": first_text(owner, ["subtask_id", "subTaskId", "id"]),
        "title": first_text(owner, ["title", "subTaskTitle", "task_step_desc"]),
        "label": entry.get("label"),
        "value": entry.get("value"),
    }
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def extract_tool_inputs(payload: dict[str, Any], limit: int = 200) -> list[dict[str, Any]]:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        if not isinstance(item, dict) or item.get("value") in (None, "", [], {}):
            return
        key = stable_json_key(item)
        if key in seen:
            return
        seen.add(key)
        collected.append(item)

    for item in as_list(evidence.get("tool_inputs")):
        if isinstance(item, dict):
            add(item)
    for path, node in iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        for index, entry in enumerate(as_list(node.get("tool_input_entries"))):
            if isinstance(entry, dict):
                add(compact_tool_entry(entry, path=f"{compact_path(path)}.tool_input_entries.{index}", owner=node))
        raw_tool_call = node.get("raw_tool_call")
        if isinstance(raw_tool_call, dict) and raw_tool_call.get("arguments") not in (None, "", [], {}):
            add(
                {
                    "path": f"{compact_path(path)}.raw_tool_call",
                    "subtask_id": first_text(node, ["subtask_id", "subTaskId", "id"]),
                    "title": first_text(node, ["title", "subTaskTitle", "task_step_desc"]),
                    "label": "raw_tool_call",
                    "tool_name": raw_tool_call.get("name"),
                    "value": raw_tool_call.get("arguments"),
                }
            )
        if len(collected) >= limit:
            break
    return collected[:limit]


def extract_skills(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def add(name: str, occurrence: dict[str, Any]) -> None:
        skill_name = str(name or "").strip()
        if not skill_name:
            return
        item = found.setdefault(skill_name, {"skill_name": skill_name, "occurrence_count": 0, "occurrences": [], "_seen": {}})
        marker = (
            occurrence.get("subtask_id")
            or f"{occurrence.get('source', '')}:{occurrence.get('title', '')}:{occurrence.get('path', '')}"
        )
        if marker in item["_seen"]:
            existing = item["_seen"][marker]
            if occurrence.get("status") and occurrence.get("status") != existing.get("status"):
                existing.update({key: value for key, value in occurrence.items() if value not in (None, "", [], {})})
            return
        item["_seen"][marker] = occurrence
        item["occurrence_count"] += 1
        if len(item["occurrences"]) < 20:
            item["occurrences"].append(occurrence)

    for path, node in iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        title = first_text(node, ["title", "subTaskTitle", "subtask_title"])
        match = SKILL_TITLE_RE.search(title)
        entries = detail_entries_map(node.get("tool_input_entries"))
        tool_name = entries.get("tool_name", "")
        title_argument = entries.get("title_argument", "")
        if match:
            add(
                match.group(1),
                {
                    "source": "read_skill_title",
                    "subtask_id": first_text(node, ["subtask_id", "subTaskId", "id"]),
                    "title": title,
                    "status": first_text(node, ["status_desc", "subTaskStatusDesc"]),
                    "time": first_text(node, ["time", "timestamp"]),
                    "path": compact_path(path),
                },
            )
        if tool_name == "读取技能" and title_argument:
            add(
                title_argument,
                {
                    "source": "read_skill_input",
                    "subtask_id": first_text(node, ["subtask_id", "subTaskId", "id"]),
                    "title": title,
                    "status": first_text(node, ["status_desc", "subTaskStatusDesc"]),
                    "time": first_text(node, ["time", "timestamp"]),
                    "path": compact_path(path),
                },
            )
        for key in ("mentioned_skills", "mentionedSkills", "skills"):
            if key not in node:
                continue
            for skill in as_list(node.get(key)):
                if isinstance(skill, dict):
                    skill_name = first_text(skill, ["skill_name", "skillName", "name", "id"])
                else:
                    skill_name = str(skill or "")
                add(skill_name, {"source": key, "path": compact_path(path)})

    result = []
    for item in found.values():
        clean = {key: value for key, value in item.items() if key != "_seen"}
        result.append(clean)
    return sorted(result, key=lambda item: item["skill_name"])


def extract_process_steps(payload: dict[str, Any], limit: int = 120) -> list[dict[str, Any]]:
    files = main_agent_files(payload)
    process_data = tab_data(files, "process_tab")
    updates = process_data.get("progress_updates")
    if not isinstance(updates, list) and payload.get("schema_version") == "query-summary/v1":
        updates = (payload.get("process_outline") or {}).get("updates")
    steps = []
    for index, update in enumerate(as_list(updates), 1):
        if not isinstance(update, dict):
            continue
        subtasks = [item for item in as_list(update.get("subtasks")) if isinstance(item, dict)]
        file_change_count = len(update.get("file_changes") or [])
        if not update.get("task_step_desc") and not subtasks and not file_change_count:
            continue
        titles = [first_text(item, ["title", "subTaskTitle"]) for item in subtasks]
        compact_subtasks = []
        for subtask in subtasks[:12]:
            compact_subtasks.append(
                {
                    "subtask_id": first_text(subtask, ["subtask_id", "subTaskId", "id"]),
                    "title": first_text(subtask, ["title", "subTaskTitle"]),
                    "desc": clip(first_text(subtask, ["desc", "subTaskDesc"]), 1000),
                    "status": first_text(subtask, ["status", "subTaskStatus"]),
                    "status_desc": first_text(subtask, ["status_desc", "subTaskStatusDesc"]),
                    "tool_input_entries": as_list(subtask.get("tool_input_entries"))[:20],
                    "input_entries": as_list(subtask.get("input_entries"))[:20],
                }
            )
        steps.append(
            {
                "index": index,
                "seq": update.get("seq"),
                "elapsed_seconds": update.get("elapsed_seconds"),
                "task_step_desc": update.get("task_step_desc"),
                "task_status_desc": update.get("task_status_desc"),
                "subtask_count": len(subtasks) or update.get("subtask_count") or 0,
                "subtask_titles": [title for title in titles if title][:8],
                "subtasks": compact_subtasks,
                "file_change_count": file_change_count,
            }
        )
        if len(steps) >= limit:
            break
    return steps


def extract_result_summary(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") == "query-qa/v1":
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        final_answer = first_text(payload, ["answer", "final_answer", "final_answer_excerpt"])
        return {
            "final_answer_excerpt": clip(final_answer, 2000),
            "sources": as_list(payload.get("sources"))[:20],
            "source_counts": {"cited_sources": len(as_list(payload.get("sources"))), "all_sources": len(as_list(payload.get("all_sources")))},
            "file_counts": {"answer_files": len(as_list(payload.get("answer_files"))), "artifacts": len(as_list(result.get("generated_files")))},
        }
    if payload.get("schema_version") == "query-summary/v1":
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    else:
        result = tab_data(main_agent_files(payload), "result_tab")
    final_answer = first_text(result, ["final_answer", "final_answer_excerpt", "answer"])
    return {
        "final_answer_excerpt": clip(final_answer, 2000),
        "sources": as_list(result.get("sources"))[:20],
        "source_counts": result.get("source_counts") or {},
        "file_counts": result.get("file_counts") or {},
    }


def build_file_query(args: argparse.Namespace, client: PaiObsClient, *, download: bool = False) -> dict[str, Any]:
    file_path = getattr(args, "file_path", None) or getattr(args, "remote_path", None) or getattr(args, "path", None) or ""
    query = {
        "env": client.env,
        "target": getattr(args, "target", None) or "",
        "name": getattr(args, "name", None) or "",
        "owner_user_id": getattr(args, "owner_user_id", None) or "",
        "file_id": getattr(args, "file_id", None) or "",
    }
    if download:
        query["file_path"] = file_path
    else:
        local_path = getattr(args, "local_path", None) or ""
        if local_path:
            query["path"] = local_path
        query["file_path"] = file_path
    fallback_paths = []
    for value in as_list(getattr(args, "fallback_path", None)):
        fallback_paths.extend(split_path_values(value))
    if fallback_paths:
        query["fallback_path"] = fallback_paths
    return query


def skill_search_roots(args: argparse.Namespace) -> list[Path]:
    raw_roots = []
    raw_roots.extend(getattr(args, "skill_root", None) or [])
    raw_roots.extend(split_path_values(os.environ.get("PAI_OBS_SKILL_ROOTS", "").replace(os.pathsep, "\n")))
    raw_roots.extend(
        [
            str(SKILL_DIR.parent),
            str(Path.cwd() / "skills"),
            str(Path.home() / ".codex" / "skills"),
            "/root/.codex/skills/.system",
        ]
    )
    roots = []
    seen = set()
    for raw in raw_roots:
        path = Path(raw).expanduser()
        marker = str(path.resolve()) if path.exists() else str(path)
        if marker in seen:
            continue
        seen.add(marker)
        if path.is_dir():
            roots.append(path)
    return roots


def find_skill_content(skill_name: str, roots: list[Path]) -> dict[str, Any]:
    name = skill_name.strip().strip("/")
    candidates = []
    for root in roots:
        candidates.extend(
            [
                root / name / "SKILL.md",
                root / name / "skill.md",
                root / ".system" / name / "SKILL.md",
                root / ".system" / name / "skill.md",
                root / "user" / name / "SKILL.md",
                root / "user" / name / "skill.md",
                root / "skills" / "user" / name / "SKILL.md",
                root / "skills" / "user" / name / "skill.md",
                root / f"{name}.md",
            ]
        )
        for depth_one in root.glob("*"):
            if depth_one.is_dir() and depth_one.name == name:
                candidates.append(depth_one / "SKILL.md")
            nested = depth_one / name / "SKILL.md" if depth_one.is_dir() else None
            if nested:
                candidates.append(nested)

    seen = set()
    for candidate in candidates:
        marker = str(candidate)
        if marker in seen:
            continue
        seen.add(marker)
        if candidate.is_file():
            return {"path": str(candidate), "content": candidate.read_text(encoding="utf-8", errors="replace")}
    return {}


def skill_content_role(path: Path, primary_path: Path | None = None) -> str:
    try:
        if primary_path and path.resolve() == primary_path.resolve():
            return "body"
    except OSError:
        pass
    name = path.name.lower()
    normalized = str(path).replace("\\", "/").lower()
    if name == "skill.md":
        return "body"
    if name == "readme.md":
        return "doc"
    if name in {"requirements.txt", "requirements.in"}:
        return "dependency"
    if name in {"pyproject.toml", "package.json"}:
        return "manifest"
    if "/scripts/" in normalized or name.endswith((".py", ".sh")):
        return "script"
    if "/references/" in normalized:
        return "reference"
    return "file"


def is_skill_text_file(path: Path) -> bool:
    return path.suffix.lower() in SKILL_CONTENT_TEXT_EXTENSIONS


def read_local_skill_files(skill_name: str, body_path: str, max_chars: int) -> list[dict[str, Any]]:
    primary = Path(body_path)
    root = primary.parent
    files: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        if len(files) >= SKILL_CONTENT_MAX_FILES:
            return
        try:
            marker = str(path.resolve())
        except OSError:
            marker = str(path)
        if marker in seen or not path.is_file() or not is_skill_text_file(path):
            return
        seen.add(marker)
        content = path.read_text(encoding="utf-8", errors="replace")
        files.append(
            {
                "path": str(path),
                "name": path.name,
                "role": skill_content_role(path, primary),
                "content": content if max_chars <= 0 else content[:max_chars],
                "truncated": bool(max_chars > 0 and len(content) > max_chars),
                "content_chars": len(content),
                "source": "local",
            }
        )

    add(primary)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".") and name not in {"__pycache__", "node_modules", ".venv"}]
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            add(Path(dirpath) / filename)
            if len(files) >= SKILL_CONTENT_MAX_FILES:
                break
        if len(files) >= SKILL_CONTENT_MAX_FILES:
            break
    return files


def render_table_payload(payload: Any, *, style: str = "csv", max_cell_chars: int | None = None) -> str:
    if not isinstance(payload, dict):
        return json_dumps(payload)
    if max_cell_chars is None:
        max_cell_chars = 120 if style == "pretty" else 0

    def rows_table(rows: list[list[Any]], headers: list[str]) -> str:
        return table(rows, headers, style=style, max_cell_chars=max_cell_chars)

    schema = payload.get("schema_version", "")
    if schema == "internal-meta/v1":
        limits = payload.get("limits") or {}
        features = payload.get("features") or {}
        rows = [
            ["environment", payload.get("environment")],
            ["default_environment", payload.get("default_environment")],
            ["profiles", ", ".join(payload.get("profiles") or [])],
            ["limits", ", ".join(f"{k}={v}" for k, v in limits.items())],
            ["features", ", ".join(k for k, v in features.items() if v)],
        ]
        return rows_table(rows, ["key", "value"])
    if schema == "question-search/v1":
        rows = []
        for index, item in enumerate(payload.get("items") or [], 1):
            rows.append(
                [
                    index,
                    item.get("status"),
                    item.get("request_time"),
                    item.get("duration_seconds"),
                    item.get("user_name"),
                    item.get("institution"),
                    f"{item.get('session_id')}:{item.get('task_index')}",
                    item.get("question") or item.get("question_text") or item.get("question_preview"),
                ]
            )
        return rows_table(rows, ["#", "status", "request_time", "dur", "user", "institution", "ref", "question"])
    if schema == "duration-stats/v1":
        rows = []
        for item in (payload.get("overall") or {}).get("items") or []:
            metrics = item.get("metrics") or {}
            rows.append(
                [
                    "overall",
                    aggregate_group_label(item.get("group") or {}),
                    metrics.get("question_count", ""),
                    "",
                    metrics.get("success_count", ""),
                    metrics.get("failed_count", ""),
                    metrics.get("running_count", ""),
                    format_ended_success_rate(metrics),
                    format_number(metrics.get("avg_duration")),
                    format_number(metrics.get("p50_duration")),
                    format_number(metrics.get("p90_duration")),
                    format_number(metrics.get("p95_duration")),
                ]
            )
        for dimension, aggregate_payload in (payload.get("breakdowns") or {}).items():
            for item in aggregate_payload.get("items") or []:
                metrics = item.get("metrics") or {}
                rows.append(
                    [
                        f"by_{dimension}",
                        aggregate_group_value(item),
                        metrics.get("question_count", ""),
                        "",
                        metrics.get("success_count", ""),
                        metrics.get("failed_count", ""),
                        metrics.get("running_count", ""),
                        format_ended_success_rate(metrics),
                        format_number(metrics.get("avg_duration")),
                        format_number(metrics.get("p50_duration")),
                        format_number(metrics.get("p90_duration")),
                        format_number(metrics.get("p95_duration")),
                    ]
                )
        slow_min_occurrences = int(payload.get("slow_min_occurrences") or 3)
        slow_top_n = int(payload.get("slow_top_n") or 10)
        for dimension, items in ((payload.get("slow_buckets") or {}).get("facets") or {}).items():
            slow_items = [
                item
                for item in items or []
                if safe_number(item.get("avg_duration")) is not None
                and int(item.get("occurrence_count") or item.get("count") or 0) >= slow_min_occurrences
            ]
            slow_items = sorted(slow_items, key=lambda item: safe_number(item.get("avg_duration")) or 0, reverse=True)[:slow_top_n]
            for item in slow_items:
                rows.append(
                    [
                        f"slow_{dimension}",
                        item.get("value", ""),
                        item.get("task_count", item.get("count", "")),
                        item.get("occurrence_count", item.get("count", "")),
                        item.get("success_count", ""),
                        item.get("failed_count", ""),
                        "",
                        "",
                        format_number(item.get("avg_duration")),
                        "",
                        "",
                        "",
                    ]
                )
        return rows_table(
            rows,
            [
                "section",
                "value",
                "query_or_task_count",
                "occurrence_count",
                "success",
                "failed",
                "running",
                "ended_success_rate",
                "avg_dur",
                "p50",
                "p90",
                "p95",
            ],
        )
    if schema == "token-stats/v1":
        rows = []
        task_stats = payload.get("task_stats") if isinstance(payload.get("task_stats"), dict) else {}
        token_metrics = task_stats.get("token_metrics") if isinstance(task_stats.get("token_metrics"), dict) else {}
        if token_metrics:
            rows.append(
                [
                    "overall",
                    "ALL",
                    token_metrics.get("token_task_count", ""),
                    token_metrics.get("total_tokens", ""),
                    token_metrics.get("prompt_tokens", ""),
                    token_metrics.get("completion_tokens", ""),
                    token_metrics.get("cached_tokens", ""),
                    token_metrics.get("llm_call_count", ""),
                    token_metrics.get("avg_total_tokens", ""),
                    token_metrics.get("p50_total_tokens", ""),
                    token_metrics.get("p90_total_tokens", ""),
                    token_metrics.get("p95_total_tokens", ""),
                    token_metrics.get("avg_llm_calls", ""),
                ]
            )
        for dimension, items in (task_stats.get("facets") or {}).items():
            for item in items or []:
                if not item.get("token_task_count"):
                    continue
                rows.append(
                    [
                        f"by_{dimension}",
                        item.get("value", ""),
                        item.get("token_task_count", ""),
                        item.get("total_tokens", ""),
                        item.get("prompt_tokens", ""),
                        item.get("completion_tokens", ""),
                        item.get("cached_tokens", ""),
                        item.get("llm_call_count", ""),
                        item.get("avg_total_tokens", ""),
                        item.get("p50_total_tokens", ""),
                        item.get("p90_total_tokens", ""),
                        item.get("p95_total_tokens", ""),
                        item.get("avg_llm_calls", ""),
                    ]
                )
        return rows_table(
            rows,
            [
                "section",
                "value",
                "token_task_count",
                "total_tokens",
                "prompt_tokens",
                "completion_tokens",
                "cached_tokens",
                "llm_call_count",
                "avg_total_tokens",
                "p50_total_tokens",
                "p90_total_tokens",
                "p95_total_tokens",
                "avg_llm_calls",
            ],
        )
    if schema == "credit-stats/v1":
        rows = []
        task_stats = payload.get("task_stats") if isinstance(payload.get("task_stats"), dict) else {}
        credit_metrics = task_stats.get("credit_metrics") if isinstance(task_stats.get("credit_metrics"), dict) else {}
        if credit_metrics:
            rows.append(
                [
                    "overall",
                    "ALL",
                    credit_metrics.get("credit_task_count", ""),
                    credit_metrics.get("order_count", ""),
                    credit_metrics.get("confirmed_order_count", ""),
                    credit_metrics.get("total_credits", ""),
                    credit_metrics.get("frozen_points", ""),
                    credit_metrics.get("avg_credits_per_task", ""),
                    credit_metrics.get("p50_credits", ""),
                    credit_metrics.get("p90_credits", ""),
                    credit_metrics.get("p95_credits", ""),
                ]
            )
        for dimension, items in (task_stats.get("facets") or {}).items():
            for item in items or []:
                if not item.get("credit_task_count"):
                    continue
                rows.append(
                    [
                        f"by_{dimension}",
                        item.get("value", ""),
                        item.get("credit_task_count", ""),
                        item.get("order_count", ""),
                        item.get("confirmed_order_count", ""),
                        item.get("total_credits", ""),
                        item.get("frozen_points", ""),
                        item.get("avg_credits_per_task", ""),
                        item.get("p50_credits", ""),
                        item.get("p90_credits", ""),
                        item.get("p95_credits", ""),
                    ]
                )
        return rows_table(
            rows,
            [
                "section",
                "value",
                "credit_task_count",
                "order_count",
                "confirmed_order_count",
                "total_credits",
                "frozen_points",
                "avg_credits_per_task",
                "p50_credits",
                "p90_credits",
                "p95_credits",
            ],
        )
    if schema == "analytics-aggregate/v1":
        rows = []
        for item in payload.get("items") or []:
            group = item.get("group") or {}
            metrics = item.get("metrics") or {}
            rows.append(
                [
                    ", ".join(f"{k}={v}" for k, v in group.items()),
                    ", ".join(f"{k}={v}" for k, v in metrics.items()),
                ]
            )
        return rows_table(rows, ["group", "metrics"])
    if schema == "analytics-facets/v1":
        rows = []
        for dimension, items in (payload.get("facets") or {}).items():
            for item in items or []:
                rows.append(
                    [
                        dimension,
                        item.get("value"),
                        item.get("count"),
                        item.get("ratio"),
                        item.get("success_count", ""),
                        item.get("failed_count", ""),
                        item.get("avg_duration", ""),
                    ]
                )
        return rows_table(rows, ["dimension", "value", "count", "ratio", "success", "failed", "avg_dur"])
    if schema == "case-candidates/v1":
        rows = []
        for index, item in enumerate(payload.get("items") or [], 1):
            ref = item.get("query_ref") or {}
            evidence = item.get("evidence") or {}
            rows.append(
                [
                    index,
                    item.get("kind"),
                    item.get("severity"),
                    item.get("confidence"),
                    ",".join(item.get("labels") or []),
                    f"{ref.get('session_id')}:{ref.get('task_index')}",
                    evidence.get("matched_keywords"),
                    evidence.get("user_text"),
                ]
            )
        return rows_table(rows, ["#", "kind", "severity", "conf", "labels", "ref", "keywords", "evidence"])
    if schema == "session-queries/v1":
        rows = []
        for item in payload.get("items") or []:
            rows.append(
                [
                    item.get("task_index"),
                    item.get("status"),
                    item.get("request_time"),
                    item.get("duration_seconds", ""),
                    item.get("question") or item.get("question_text") or item.get("question_preview"),
                ]
            )
        return rows_table(rows, ["task", "status", "request_time", "dur", "question"])
    if schema == "session-trace/v1":
        rows = []
        current = payload.get("task_index")
        for item in payload.get("items") or []:
            rows.append(
                [
                    "*" if item.get("task_index") == current else "",
                    item.get("task_index"),
                    item.get("status"),
                    item.get("request_time"),
                    item.get("question") or item.get("question_text") or item.get("question_preview"),
                ]
            )
        return rows_table(rows, ["", "task", "status", "request_time", "question"])
    if schema == "task-files/v1":
        rows = []
        for index, item in enumerate(payload.get("files") or [], 1):
            rows.append(
                [
                    index,
                    ",".join(item.get("roles") or []),
                    item.get("file_type", ""),
                    item.get("name", ""),
                    item.get("file_path", ""),
                    item.get("raw_file_path", ""),
                ]
            )
        return rows_table(rows, ["#", "roles", "type", "name", "file_path", "raw_file_path"])
    if schema == "task-skills/v1":
        rows = []
        for item in payload.get("skills") or []:
            first = (item.get("occurrences") or [{}])[0]
            rows.append(
                [
                    item.get("skill_name"),
                    item.get("occurrence_count"),
                    first.get("status", ""),
                    first.get("time", ""),
                    first.get("source", ""),
                ]
            )
        return rows_table(rows, ["skill", "count", "first_status", "first_time", "source"])
    if schema == "task-stats/v1":
        rows = []
        for dimension, items in (payload.get("facets") or {}).items():
            for item in items or []:
                rows.append(
                    [
                        dimension,
                        item.get("value", ""),
                        item.get("task_count", ""),
                        item.get("occurrence_count", ""),
                        item.get("ratio", ""),
                        item.get("success_count", ""),
                        item.get("failed_count", ""),
                        item.get("avg_duration", ""),
                    ]
                )
        return rows_table(rows, ["dimension", "value", "task_count", "occurrence_count", "ratio", "success", "failed", "avg_dur"])
    if schema == "skill-content/v1":
        rows = []
        files = payload.get("files") or []
        if files:
            for index, item in enumerate(files, 1):
                rows.append(
                    [
                        index,
                        item.get("role", ""),
                        item.get("source", payload.get("source", "")),
                        item.get("content_chars", ""),
                        "yes" if item.get("truncated") else "",
                        item.get("path", ""),
                    ]
                )
        elif payload.get("path"):
            rows.append(
                [
                    1,
                    "body",
                    payload.get("source", ""),
                    payload.get("content_chars", ""),
                    "yes" if payload.get("truncated") else "",
                    payload.get("path", ""),
                ]
            )
        return rows_table(rows, ["#", "role", "source", "chars", "truncated", "path"])
    if schema == "task-inspection/v1":
        info = payload.get("query_info") or {}
        lines = [
            "Query",
            rows_table(
                [
                    ["session", info.get("session_id", "")],
                    ["task", info.get("task_index", "")],
                    ["question", info.get("question", "")],
                    ["status", info.get("status", "")],
                    ["request_time", info.get("request_time", "")],
                ],
                ["key", "value"],
            ),
        ]
        if payload.get("skills"):
            lines.extend(
                [
                    "",
                    "Skills",
                    render_table_payload(
                        {"schema_version": "task-skills/v1", "skills": payload.get("skills")},
                        style=style,
                        max_cell_chars=max_cell_chars,
                    ),
                ]
            )
        if payload.get("files"):
            lines.extend(
                [
                    "",
                    "Files",
                    render_table_payload(
                        {"schema_version": "task-files/v1", "files": payload.get("files")},
                        style=style,
                        max_cell_chars=max_cell_chars,
                    ),
                ]
            )
        rows = []
        for step in payload.get("process_steps") or []:
            rows.append(
                [
                    step.get("index"),
                    step.get("seq", ""),
                    step.get("elapsed_seconds", ""),
                    step.get("task_status_desc", ""),
                    " | ".join(step.get("subtask_titles") or []),
                    step.get("task_step_desc", ""),
                ]
            )
        if rows:
            lines.extend(["", "Process", rows_table(rows, ["#", "seq", "elapsed", "status", "subtasks", "step_desc"])])
        return "\n".join(lines)
    return json_dumps(payload)


def cmd_health(client: PaiObsClient, args: argparse.Namespace) -> None:
    output_payload(client.request("GET", "/health"), args, default_format=args.format)


def cmd_meta(client: PaiObsClient, args: argparse.Namespace) -> None:
    output_payload(client.request("GET", "/meta", query={"env": client.env}), args, default_format=args.format)


def cmd_search(client: PaiObsClient, args: argparse.Namespace) -> None:
    body = {
        "env": client.env,
        "filters": build_filters(args),
        "page": {"limit": args.limit, "cursor": args.cursor or ""},
        "profile": args.profile,
    }
    output_payload(client.request("POST", "/history/questions/search", body=body), args, default_format=args.format)


def _search_session_items(client: PaiObsClient, session_id: str, limit: int, profile: str = "summary") -> list[dict[str, Any]]:
    body = {
        "env": client.env,
        "filters": {"session_id": session_id},
        "page": {"limit": limit},
        "profile": profile,
    }
    payload = client.request("POST", "/history/questions/search", body=body)
    items = payload.get("items") if isinstance(payload, dict) else []
    return sorted([item for item in items if isinstance(item, dict)], key=lambda item: int(item.get("task_index") or 0))


def cmd_session(client: PaiObsClient, args: argparse.Namespace) -> None:
    items = _search_session_items(client, args.session_id, args.limit, args.profile)
    output_payload(
        {
            "schema_version": "session-queries/v1",
            "session_id": args.session_id,
            "count": len(items),
            "items": items,
        },
        args,
        default_format=args.format,
    )


def cmd_trace(client: PaiObsClient, args: argparse.Namespace) -> None:
    items = [
        item
        for item in _search_session_items(client, args.session_id, args.limit, args.profile)
        if int(item.get("task_index") or 0) <= args.task_index
    ]
    payload: dict[str, Any] = {
        "schema_version": "session-trace/v1",
        "session_id": args.session_id,
        "task_index": args.task_index,
        "count": len(items),
        "items": items,
    }
    if args.with_context and items:
        payload["contexts"] = client.request(
            "POST",
            "/history/questions/batch-context",
            body={
                "env": client.env,
                "items": [{"session_id": args.session_id, "task_index": item.get("task_index")} for item in items],
                "profile": args.context_profile,
                "max_items": min(args.max_items, len(items)),
            },
        )
    output_payload(payload, args, default_format=args.format)


def cmd_task(client: PaiObsClient, args: argparse.Namespace) -> None:
    payload = client.request(
        "GET",
        f"/history/sessions/{args.session_id}/tasks/{args.task_index}",
        query={"env": client.env, "profile": args.profile},
    )
    output_payload(payload, args)


def cmd_context(client: PaiObsClient, args: argparse.Namespace) -> None:
    payload = client.request(
        "GET",
        f"/history/sessions/{args.session_id}/tasks/{args.task_index}/context",
        query={"env": client.env},
    )
    output_payload(payload, args)


def cmd_bundle(client: PaiObsClient, args: argparse.Namespace) -> None:
    data, headers = client.request(
        "GET",
        f"/history/sessions/{args.session_id}/tasks/{args.task_index}/bundle.zip",
        query={"env": client.env},
        binary=True,
    )
    out = args.output or f"{args.session_id}_{args.task_index}_bundle.zip"
    Path(out).write_bytes(data)
    print(f"wrote {out} ({len(data)} bytes)")


def cmd_inspect(client: PaiObsClient, args: argparse.Namespace) -> None:
    context = load_context_source(client, args)
    output_payload(
        {
            "schema_version": "task-inspection/v1",
            "query_info": extract_query_info(context),
            "skills": extract_skills(context),
            "files": extract_files(context),
            "process_steps": extract_process_steps(context, limit=args.process_limit),
            "tool_inputs": extract_tool_inputs(context),
            "evidence": context.get("evidence") if isinstance(context.get("evidence"), dict) else {},
            "result": extract_result_summary(context),
        },
        args,
        default_format=args.format,
    )


def cmd_files(client: PaiObsClient, args: argparse.Namespace) -> None:
    context = load_context_source(client, args)
    files = extract_files(context)
    output_payload(
        {
            "schema_version": "task-files/v1",
            "query_info": extract_query_info(context),
            "count": len(files),
            "files": files,
        },
        args,
        default_format=args.format,
    )


def cmd_skills(client: PaiObsClient, args: argparse.Namespace) -> None:
    context = load_context_source(client, args)
    skills = extract_skills(context)
    output_payload(
        {
            "schema_version": "task-skills/v1",
            "query_info": extract_query_info(context),
            "count": len(skills),
            "skills": skills,
        },
        args,
        default_format=args.format,
    )


def cmd_file_preview(client: PaiObsClient, args: argparse.Namespace) -> None:
    payload = client.request(
        "GET",
        "/api/files/preview",
        query=build_file_query(args, client),
        internal=False,
    )
    output_payload(payload, args, default_format=args.format)


def cmd_file_download(client: PaiObsClient, args: argparse.Namespace) -> None:
    data, headers = client.request(
        "GET",
        "/api/files/content",
        query=build_file_query(args, client, download=True),
        binary=True,
        internal=False,
    )
    out = args.output
    if not out:
        disposition = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
        filename = ""
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"')
        out = filename or os.path.basename(normalize_ref_path(args.file_path or args.path).rstrip("/")) or "paiobs-file.data"
    Path(out).write_bytes(data)
    print(f"wrote {out} ({len(data)} bytes)")


def cmd_skill_content(client: PaiObsClient, args: argparse.Namespace) -> None:
    user_id = getattr(args, "user_id", "") or getattr(args, "owner_user_id", "") or ""
    if user_id or getattr(args, "remote", False):
        query = {
            "env": client.env,
            "skill_name": args.skill_name,
            "user_id": user_id,
            "max_chars": args.max_chars,
            "include_files": "true" if args.include_files else "false",
        }
        if getattr(args, "file_path", None):
            query["file_path"] = args.file_path
        payload = client.request(
            "GET",
            "/skills/content",
            query=query,
        )
        output_payload(payload, args, default_format=args.format)
        return

    roots = skill_search_roots(args)
    found = find_skill_content(args.skill_name, roots)
    payload = {
        "schema_version": "skill-content/v1",
        "skill_name": args.skill_name,
        "status": "found" if found else "not_found",
        "searched_roots": [str(root) for root in roots],
    }
    if found:
        content = found["content"]
        max_chars = args.max_chars
        files = read_local_skill_files(args.skill_name, found["path"], max_chars) if args.include_files else []
        payload.update(
            {
                "path": found["path"],
                "content": content if max_chars <= 0 else content[:max_chars],
                "truncated": bool(max_chars > 0 and len(content) > max_chars),
                "content_chars": len(content),
                "include_files": args.include_files,
                "files": files,
                "file_count": len(files),
            }
        )
    else:
        payload["message"] = (
            "Skill body was not present in the local configured roots. "
            "Set PAI_OBS_SKILL_ROOTS or pass --skill-root pointing at the runtime skill repository."
        )
    output_payload(payload, args, default_format=args.format)


def cmd_batch_context(client: PaiObsClient, args: argparse.Namespace) -> None:
    body = {
        "env": client.env,
        "items": parse_items(args),
        "profile": args.profile,
        "max_items": args.max_items,
    }
    if getattr(args, "max_concurrency", None):
        body["max_concurrency"] = args.max_concurrency
    output_payload(client.request("POST", "/history/questions/batch-context", body=body), args)


def cmd_batch(client: PaiObsClient, args: argparse.Namespace) -> None:
    items = parse_items(args)
    filters = build_filters(args)
    # When explicit refs are provided, auto-expand max_items to cover all of them
    # unless the user explicitly set --max-items.
    effective_max_items = args.max_items
    if items and len(items) > effective_max_items:
        effective_max_items = len(items)
    body = {
        "env": client.env,
        "profile": args.profile,
        "max_items": effective_max_items,
        "max_concurrency": args.max_concurrency,
        "include_search_item": args.include_search_item,
    }
    if items:
        body["items"] = items
    if filters:
        body["filters"] = filters
    output_payload(client.request("POST", "/history/questions/batch", body=body), args)


def cmd_analyze(client: PaiObsClient, args: argparse.Namespace) -> None:
    body = {
        "env": client.env,
        "session_id": args.session_id,
        "task_index": args.task_index,
        "focus": args.focus or "",
    }
    output_payload(client.request("POST", "/analysis/query", body=body), args)


def cmd_analyze_batch(client: PaiObsClient, args: argparse.Namespace) -> None:
    body = {
        "env": client.env,
        "items": parse_items(args),
        "focus": args.focus or "",
        "max_items": args.max_items,
    }
    output_payload(client.request("POST", "/analysis/batch", body=body), args)


def cmd_aggregate(client: PaiObsClient, args: argparse.Namespace) -> None:
    body = {
        "env": client.env,
        "filters": build_filters(args),
        "group_by": parse_csv_values(args.group_by),
        "metrics": parse_csv_values(args.metrics),
        "limit": args.limit,
    }
    output_payload(client.request("POST", "/analytics/aggregate", body=body), args, default_format=args.format)


def request_aggregate(
    client: PaiObsClient,
    filters: dict[str, Any],
    group_by: list[str],
    metrics: list[str],
    limit: int,
) -> dict[str, Any]:
    body = {
        "env": client.env,
        "filters": filters,
        "group_by": group_by,
        "metrics": metrics,
        "limit": limit,
    }
    return client.request("POST", "/analytics/aggregate", body=body)


def cmd_duration_stats(client: PaiObsClient, args: argparse.Namespace) -> None:
    filters = build_filters(args)
    metrics = parse_csv_values(args.metrics) or DURATION_STATS_DEFAULT_METRICS
    group_by = parse_csv_values(args.group_by)
    slow_dimensions = parse_csv_values(args.slow_dimensions)
    payload: dict[str, Any] = {
        "schema_version": "duration-stats/v1",
        "env": client.env,
        "filters": filters,
        "metrics": metrics,
        "group_by": group_by,
        "slow_dimensions": slow_dimensions,
        "slow_min_occurrences": args.slow_min_occurrences,
        "slow_top_n": args.slow_top_n,
        "warnings": [],
        "overall": request_aggregate(client, filters, [], metrics, args.limit),
        "breakdowns": {},
    }

    for dimension in group_by:
        payload["breakdowns"][dimension] = request_aggregate(client, filters, [dimension], metrics, args.limit)

    if slow_dimensions:
        body = {
            "env": client.env,
            "filters": filters,
            "dimensions": slow_dimensions,
            "top_n": args.top_n,
            "max_tasks": args.max_tasks,
            "include_sample_refs": False,
            "sample_ref_limit": 0,
        }
        try:
            slow_payload = client.request("POST", "/analytics/task-stats", body=body)
            payload["slow_buckets"] = slow_payload
            slow_warnings = (slow_payload.get("coverage") or {}).get("warnings") or slow_payload.get("warnings") or []
            payload["warnings"].extend(f"slow buckets: {item}" for item in slow_warnings)
        except ApiError as exc:
            if exc.status not in {404, 405}:
                raise
            payload["warnings"].append("server-side /analytics/task-stats is unavailable; skipped slow dimension buckets")

    output_payload(payload, args, default_format=args.format)


def cmd_facets(client: PaiObsClient, args: argparse.Namespace) -> None:
    body = {
        "env": client.env,
        "filters": build_filters(args),
        "dimensions": parse_csv_values(args.dimensions),
        "limit": args.limit,
        "sample_limit": args.sample_limit,
    }
    output_payload(client.request("POST", "/analytics/facets", body=body), args, default_format=args.format)


def cmd_mine(client: PaiObsClient, args: argparse.Namespace) -> None:
    rules = {}
    include = parse_csv_values(args.include_keywords)
    exclude = parse_csv_values(args.exclude_keywords)
    if include:
        rules["include_keywords"] = include
    if exclude:
        rules["exclude_keywords"] = exclude
    if args.min_duration_seconds:
        rules["min_duration_seconds"] = args.min_duration_seconds
    body = {
        "env": client.env,
        "kind": args.kind,
        "filters": build_filters(args),
        "rules": rules,
        "sample": {"limit": args.limit},
        "with_context": args.with_context,
    }
    output_payload(client.request("POST", "/mining/cases", body=body), args, default_format=args.format)


def cmd_export(client: PaiObsClient, args: argparse.Namespace) -> None:
    body = {
        "env": client.env,
        "type": args.type,
        "filters": build_filters(args),
        "limit": args.limit,
    }
    if args.type == "context_zip":
        body["items"] = parse_items(args)
        body["max_items"] = args.max_items
    metadata = client.request("POST", "/exports", body=body)
    if args.download_to and metadata.get("export_id"):
        data, _ = client.request("GET", f"/exports/{metadata['export_id']}/download", binary=True)
        Path(args.download_to).write_bytes(data)
        metadata["downloaded_to"] = args.download_to
        metadata["downloaded_bytes"] = len(data)
    output_payload(metadata, args)


def cmd_download(client: PaiObsClient, args: argparse.Namespace) -> None:
    data, headers = client.request("GET", f"/exports/{args.export_id}/download", binary=True)
    out = args.output
    if not out:
        disposition = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
        filename = ""
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('"')
        out = filename or f"{args.export_id}.data"
    Path(out).write_bytes(data)
    print(f"wrote {out} ({len(data)} bytes)")


def cmd_job_create(client: PaiObsClient, args: argparse.Namespace) -> None:
    payload = read_json_value(args.payload_json, default={})
    if not isinstance(payload, dict):
        raise SystemExit("--payload-json must be a JSON object")
    body = {"type": args.type, "env": client.env, "payload": payload}
    output_payload(client.request("POST", "/jobs", body=body), args)


def cmd_job(client: PaiObsClient, args: argparse.Namespace) -> None:
    output_payload(client.request("GET", f"/jobs/{args.job_id}"), args)


def cmd_job_result(client: PaiObsClient, args: argparse.Namespace) -> None:
    output_payload(client.request("GET", f"/jobs/{args.job_id}/result"), args)


def cmd_task_stats(client: PaiObsClient, args: argparse.Namespace) -> None:
    from paiobs_task_stats import run_task_stats

    run_task_stats(client, args)


def add_output_args(parser: argparse.ArgumentParser, default_format: str = "json") -> None:
    parser.add_argument(
        "--format",
        choices=["json", "jsonl", "table", "pretty", "markdown"],
        default=default_format,
        help="Output format. table is CSV-compatible; pretty is an aligned console table; markdown renders compact reports for analytics payloads.",
    )
    parser.add_argument(
        "--max-cell-chars",
        type=int,
        default=None,
        help="Max chars per table/pretty cell; 0 disables CLI truncation. Default: table=0, pretty=120.",
    )
    parser.add_argument("-o", "--output", help="Write output to a file")


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--filters-json", help="JSON object or path merged into filters")
    parser.add_argument("--session-id")
    parser.add_argument("--question-id")
    parser.add_argument("--keyword")
    parser.add_argument("--answer-keyword", help="Filter by keywords contained in answer content")
    parser.add_argument("--user-id")
    parser.add_argument("--username")
    parser.add_argument("--institution")
    parser.add_argument("--entry-scene")
    parser.add_argument("--status")
    parser.add_argument("--scheduled")
    parser.add_argument("--end-type")
    parser.add_argument("--is-web-search")
    parser.add_argument("--start-time")
    parser.add_argument("--end-time")
    parser.add_argument("--institution-nature")
    parser.add_argument("--inst-type")
    parser.add_argument("--product-type")
    parser.add_argument("--user-type")
    parser.add_argument("--user-role")


def add_items_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("refs", nargs="*", help="Optional refs in session_id:task_index form")
    parser.add_argument("--items-json", help="JSON list/object or path containing items")


def add_context_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("session_id", nargs="?")
    parser.add_argument("task_index", nargs="?", type=int)
    parser.add_argument("--context-json", help="Path to a query-agent-tabs JSON/context payload")
    parser.add_argument("--pack-dir", help="Unzipped QueryAgentTabs bundle directory")
    parser.add_argument("--bundle", help="QueryAgentTabs bundle zip")


def add_file_proxy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", nargs="?", help="Remote file path from a task data pack")
    parser.add_argument("--file-path", dest="file_path", help="Remote file_path/content_path")
    parser.add_argument("--target", default="", help="AlphaPai target URL; defaults to gateway environment config")
    parser.add_argument("--name", default="", help="Display filename")
    parser.add_argument("--fallback-path", action="append", default=[], help="Fallback remote path; repeatable")
    parser.add_argument("--owner-user-id", default="", help="Owner user id for historical workspace files")
    parser.add_argument("--file-id", default="", help="Workspace file id when available")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PaiWork Observability Gateway CLI")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--gateway-profile", default=None, help="Gateway profile: release/prod -> 30100, local/test/debug -> localhost:6193")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env", default=None)
    parser.add_argument("--timeout", default=None)
    parser.add_argument("--file-auth-token", default=None, help="Optional PaiWork auth token for /api/files preview/content")

    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="Check gateway health")
    add_output_args(health)
    health.set_defaults(func=cmd_health)

    meta = sub.add_parser("meta", help="Show gateway metadata")
    add_output_args(meta, default_format="json")
    meta.set_defaults(func=cmd_meta)

    search = sub.add_parser("search", help="Search historical questions")
    add_filter_args(search)
    search.add_argument("--profile", default="lite", choices=["lite", "summary", "raw"])
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--cursor", default="")
    add_output_args(search, default_format="table")
    search.set_defaults(func=cmd_search)

    session = sub.add_parser("session", help="List queries in one session, ordered by task_index")
    session.add_argument("session_id")
    session.add_argument("--profile", default="summary", choices=["lite", "summary", "raw"])
    session.add_argument("--limit", type=int, default=100)
    add_output_args(session, default_format="table")
    session.set_defaults(func=cmd_session)

    trace = sub.add_parser("trace", help="Trace current and previous queries in the same session")
    trace.add_argument("session_id")
    trace.add_argument("task_index", type=int)
    trace.add_argument("--profile", default="summary", choices=["lite", "summary", "raw"])
    trace.add_argument("--limit", type=int, default=100)
    trace.add_argument("--with-context", action="store_true", help="Also batch-load context for traced queries")
    trace.add_argument("--context-profile", default="summary", choices=["summary", "context", "qa", "full"])
    trace.add_argument("--max-items", type=int, default=20)
    add_output_args(trace, default_format="table")
    trace.set_defaults(func=cmd_trace)

    task = sub.add_parser("task", help="Load one task payload")
    task.add_argument("session_id")
    task.add_argument("task_index", type=int)
    task.add_argument("--profile", default="summary", choices=["summary", "context", "qa", "full"])
    add_output_args(task)
    task.set_defaults(func=cmd_task)

    context = sub.add_parser("context", help="Load QueryAgentTabs context")
    context.add_argument("session_id")
    context.add_argument("task_index", type=int)
    add_output_args(context)
    context.set_defaults(func=cmd_context)

    bundle = sub.add_parser("bundle", help="Download one task context bundle zip")
    bundle.add_argument("session_id")
    bundle.add_argument("task_index", type=int)
    bundle.add_argument("-o", "--output")
    bundle.set_defaults(func=cmd_bundle)

    inspect = sub.add_parser("inspect", help="Inspect one task from context or a downloaded data pack")
    add_context_source_args(inspect)
    inspect.add_argument("--process-limit", type=int, default=120)
    add_output_args(inspect, default_format="table")
    inspect.set_defaults(func=cmd_inspect)

    files = sub.add_parser("files", help="List input/process/result files from context or a data pack")
    add_context_source_args(files)
    add_output_args(files, default_format="table")
    files.set_defaults(func=cmd_files)

    skills = sub.add_parser("skills", help="List mentioned/read skills from context or a data pack")
    add_context_source_args(skills)
    add_output_args(skills, default_format="table")
    skills.set_defaults(func=cmd_skills)

    file_preview = sub.add_parser("file-preview", help="Preview a remote text file referenced by a task data pack")
    add_file_proxy_args(file_preview)
    file_preview.add_argument("--local-path", default="", help="Optional local file path to preview first")
    add_output_args(file_preview)
    file_preview.set_defaults(func=cmd_file_preview)

    file_download = sub.add_parser("file-download", help="Download a remote file referenced by a task data pack")
    add_file_proxy_args(file_download)
    file_download.add_argument("-o", "--output")
    file_download.set_defaults(func=cmd_file_download)

    skill_content = sub.add_parser("skill-content", help="Read full skill content from local roots or the gateway")
    skill_content.add_argument("skill_name")
    skill_content.add_argument("--skill-root", action="append", default=[], help="Skill root directory; repeatable")
    skill_content.add_argument("--user-id", "--owner-user-id", dest="user_id", default="", help="User id whose workspace skills should be searched through the gateway")
    skill_content.add_argument("--remote", action="store_true", help="Ask the gateway instead of only searching local roots")
    skill_content.add_argument("--max-chars", type=int, default=40000)
    skill_content.add_argument("--include-files", action=argparse.BooleanOptionalAction, default=True, help="Include SKILL.md plus discovered sidecar files in files[]")
    skill_content.add_argument("--file-path", action="append", default=[], help="Extra skill file path to fetch; repeatable")
    add_output_args(skill_content)
    skill_content.set_defaults(func=cmd_skill_content)

    batch = sub.add_parser("batch", help="Load batch task payloads by refs or search filters")
    add_items_args(batch)
    add_filter_args(batch)
    batch.add_argument("--profile", default="summary", choices=["summary", "context", "qa", "full"])
    batch.add_argument("--max-items", type=int, default=20)
    batch.add_argument("--max-concurrency", type=int, default=8)
    batch.add_argument("--include-search-item", action="store_true", help="Include compact search metadata when loading by filters")
    add_output_args(batch)
    batch.set_defaults(func=cmd_batch)

    batch_context = sub.add_parser("batch-context", help="Compatibility alias for loading batch task payloads")
    add_items_args(batch_context)
    batch_context.add_argument("--profile", default="context", choices=["summary", "context", "qa", "full"])
    batch_context.add_argument("--max-items", type=int, default=20)
    batch_context.add_argument("--max-concurrency", type=int, default=8)
    add_output_args(batch_context)
    batch_context.set_defaults(func=cmd_batch_context)

    analyze = sub.add_parser("analyze", help="Run LLM analysis for one task")
    analyze.add_argument("session_id")
    analyze.add_argument("task_index", type=int)
    analyze.add_argument("--focus", default="")
    add_output_args(analyze)
    analyze.set_defaults(func=cmd_analyze)

    analyze_batch = sub.add_parser("analyze-batch", help="Run LLM analysis for several tasks")
    add_items_args(analyze_batch)
    analyze_batch.add_argument("--focus", default="")
    analyze_batch.add_argument("--max-items", type=int, default=5)
    add_output_args(analyze_batch)
    analyze_batch.set_defaults(func=cmd_analyze_batch)

    aggregate = sub.add_parser("aggregate", help="Read aggregate metrics")
    add_filter_args(aggregate)
    aggregate.add_argument("--group-by", default="")
    aggregate.add_argument("--metrics", default="")
    aggregate.add_argument("--limit", type=int, default=500)
    add_output_args(aggregate, default_format="table")
    aggregate.set_defaults(func=cmd_aggregate)

    duration_stats = sub.add_parser("duration-stats", help="Read duration-focused metrics and slow buckets")
    add_filter_args(duration_stats)
    duration_stats.add_argument(
        "--group-by",
        default=",".join(DURATION_STATS_DEFAULT_GROUP_BY),
        help="Comma-separated aggregate breakdown dimensions. Default: status,entry_scene.",
    )
    duration_stats.add_argument(
        "--metrics",
        default=",".join(DURATION_STATS_DEFAULT_METRICS),
        help="Comma-separated aggregate metrics. Defaults to question/status counts plus avg/P50/P90/P95 duration.",
    )
    duration_stats.add_argument("--limit", type=int, default=500)
    duration_stats.add_argument(
        "--slow-dimensions",
        default=",".join(DURATION_STATS_DEFAULT_SLOW_DIMENSIONS),
        help="Comma-separated task-stats dimensions used to find slow buckets. Empty disables slow bucket lookup.",
    )
    duration_stats.add_argument("--top-n", type=int, default=100, help="Top values to request per slow dimension before sorting by duration.")
    duration_stats.add_argument("--slow-top-n", type=int, default=10, help="Slow values shown per dimension in markdown output.")
    duration_stats.add_argument("--slow-min-occurrences", type=int, default=3, help="Minimum occurrence count for slow buckets.")
    duration_stats.add_argument("--max-tasks", type=int, default=TASK_STATS_DEFAULT_MAX_TASKS, help="Maximum tasks scanned by server-side task-stats for slow buckets.")
    add_output_args(duration_stats, default_format="markdown")
    duration_stats.set_defaults(func=cmd_duration_stats)

    facets = sub.add_parser("facets", help="Read facets")
    add_filter_args(facets)
    facets.add_argument("--dimensions", default="")
    facets.add_argument("--limit", type=int, default=20)
    facets.add_argument("--sample-limit", type=int, default=20)
    add_output_args(facets, default_format="table")
    facets.set_defaults(func=cmd_facets)

    task_stats = sub.add_parser("task-stats", help="Compute task stats, including detail-derived tools/skills/sources")
    add_filter_args(task_stats)
    from paiobs_task_stats import add_task_stats_specific_args

    add_task_stats_specific_args(task_stats)
    add_output_args(task_stats, default_format="table")
    task_stats.set_defaults(func=cmd_task_stats)

    mine = sub.add_parser("mine", help="Mine case candidates")
    add_filter_args(mine)
    mine.add_argument(
        "kind",
        choices=["badcase", "goodcase", "complaint", "new_requirement", "inability", "cost_outlier", "tool_failure"],
    )
    mine.add_argument("--include-keywords", default="")
    mine.add_argument("--exclude-keywords", default="")
    mine.add_argument("--min-duration-seconds", type=float, default=0)
    mine.add_argument("--with-context", action="store_true")
    mine.add_argument("--limit", type=int, default=50)
    add_output_args(mine, default_format="table")
    mine.set_defaults(func=cmd_mine)

    export = sub.add_parser("export", help="Create an export")
    add_filter_args(export)
    export.add_argument("type", choices=["search_json", "search_jsonl", "search_csv", "context_zip"])
    add_items_args(export)
    export.add_argument("--limit", type=int, default=100)
    export.add_argument("--max-items", type=int, default=20)
    export.add_argument("--download-to", help="Download created export to this path")
    add_output_args(export)
    export.set_defaults(func=cmd_export)

    download = sub.add_parser("download", help="Download an export by id")
    download.add_argument("export_id")
    download.add_argument("-o", "--output")
    download.set_defaults(func=cmd_download)

    job_create = sub.add_parser("job-create", help="Create an async metrics job")
    job_create.add_argument("type", choices=["analytics.aggregate", "analytics.facets", "analytics.task_stats"])
    job_create.add_argument("--payload-json", required=True, help="JSON object or path")
    add_output_args(job_create)
    job_create.set_defaults(func=cmd_job_create)

    job = sub.add_parser("job", help="Get job status")
    job.add_argument("job_id")
    add_output_args(job)
    job.set_defaults(func=cmd_job)

    job_result = sub.add_parser("job-result", help="Get completed job result")
    job_result.add_argument("job_id")
    add_output_args(job_result)
    job_result.set_defaults(func=cmd_job_result)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = build_client(args)
    try:
        args.func(client, args)
        return 0
    except ApiError as exc:
        sys.stderr.write(f"ERROR: {exc.message}\n")
        if exc.payload is not None:
            sys.stderr.write(json_dumps(exc.payload) + "\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
