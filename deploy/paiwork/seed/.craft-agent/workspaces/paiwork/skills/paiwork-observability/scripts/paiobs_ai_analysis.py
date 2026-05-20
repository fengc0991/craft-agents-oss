#!/usr/bin/env python3.11
"""AI classification and quality judging for PaiWork tasks."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import paiobs
from paiobs_task_query import collect_search_items, search_questions


PROMPT_VERSION = "paiwork-quality-judge/v2"
QUALITY_SCHEMA_VERSION = "paiwork-quality-analysis/v1"
DEFAULT_AI_PROVIDER = "openai-compatible"
DEFAULT_AI_BASE_URL = "https://test-llm.rabyte.cn"
DEFAULT_MODEL = "openai-gpt-4.1"
DEFAULT_AI_TIMEOUT = 45.0
DEFAULT_MAX_TOKENS = 1100
DEFAULT_CONTEXT_PROFILE = "summary"
DEFAULT_MAX_CONTEXT_CHARS = 0
DEFAULT_ANALYZE_LIMIT = 1000
try:
    DEFAULT_AI_WORKERS = max(1, int(os.environ.get("PAI_OBS_AI_WORKERS", "4") or "4"))
except ValueError:
    DEFAULT_AI_WORKERS = 4
DEFAULT_GATEWAY_BATCH_SIZE = 20
DEFAULT_SEARCH_SLICE_MINUTES = 3.0
DEFAULT_SEARCH_PAGE_LIMIT = 100
DEFAULT_SEARCH_WORKERS = 8
DEFAULT_PROGRESS_INTERVAL = 5.0
DEFAULT_DEEPTASK_LLM_CONFIG = Path("/root/rabit/deeptask/overseadata-server/config.yaml")
DEFAULT_TIMEOUT_SECONDS = 3590
QUALITY_QUERY_INFO_KEYS = [
    "session_id",
    "task_index",
    "question_id",
    "request_time",
    "response_time",
    "status",
    "end_type",
    "task_status_desc",
    "success",
    "entry_scene",
    "scheduled",
    "is_web_search",
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
    "duration",
    "dur",
    "total_elapsed",
    "average_time",
    "question",
    "env",
    "target",
    "history_db",
    "source_kind",
    "system",
    "error",
    "session_question_count",
]
_AI_CONFIG_CACHE: dict[str, str] | None = None

QUERY_TAXONOMY = """
query_major_category / query_minor_category:
1. 市场情报与监控: 新闻事件跟踪、会议日程监控、市场叙事归因
2. 基本面研究与比较分析: 单公司研究、行业主题研究、同业比较、信用与债券研究
3. 数据提取筛选与结构化分析: 跨来源数据提取、规则筛选排序、历史统计描述分析
4. 量化策略因子与回测: 策略设计与因子定义、回测与绩效评估、监控看板与量化产品
5. 多模态重建与研究交付物: 图像PDF表格重建、Excel/PPT/报告生成
6. Agent 工作流工程与自动化: Skill/工具/环境配置、定时工作流与投递、跨应用执行
7. 其他: 通用问答、管理或配置、不明确或非金融问题
"""

ISSUE_TAXONOMY = """
低分问题归因 taxonomy:
一级分类只能使用：数据类、算法类、业务类。
二级分类必须只输出下面的分类名称，不要把责任人写进 issue_level2；责任人单独写入 owner 字段：
- 数据类: 外资研报、外资纪要、会议纪要、路演日历、研报、固收、个股基本面、调研、策略会日历、公告、未分类
- 算法类: 技术问题。用于搜索设计、tool 调用、tool 入参、SQL/DSL、接口、文件读写、联网检索、规划/路由等 tools/算法层问题。
- 业务类: 国内股票类、海外类、基金、蓝宝书类、其他未归类。用于 skill 执行流程、金融行业规范、业务口径、交付格式、结果不符合研究使用习惯等业务层问题。
责任人映射：外资研报/外资纪要 -> @周宛蓉；会议纪要 -> @王宝涵 @王意荃；路演日历/研报/固收 -> @王宝涵；个股基本面/调研、策略会日历/公告/未分类 -> @陆雯；技术问题 -> @王能；国内股票类 -> @韩字杰；海外类 -> @凤超；基金、蓝宝书类 -> @齐涤非；其他未归类 -> @王峰伊。
"""

UNIFIED_INSTRUCTION = f"""
你是 PaiWork 任务质量标注员。只能基于输入的任务 context 判断，不要编造未提供的证据。

目标：
1. 对用户 query 做 1-2 级分类，参考 taxonomy。
   - query_major_category 和 query_minor_category 必须使用 taxonomy 中的中文名称，不要输出英文枚举名。
2. 给结果评分 result_score，0-10 分。
   - result_score 只看输入需求和最终输出，包括 answer 与最终文件产物内容/摘要，做 end-to-end 判断。
   - 批量质量扫描禁止查看或引用过程轨迹：不要使用 process_steps、research_step、tool calls、Agent 委托链路、内部思考或逐步执行日志作为评分依据。
   - 失败、超时、无最终输出任务必须 result_score=0。
   - 批量扫描只允许依据基本数据、用户输入、最终输出、产物摘要和指标判断；context 中即使存在过程字段，也必须忽略。
   - 如果任务成功、用户需求较简单且最终回答已经满足需求，过程字段缺省不能扣分。
   - 只有当任务需要明确附件/文件产物/证据链，而最终输出显示缺失、报错、超时、未完成时，才把 result_score 压到 0-4。
3. 对所有任务做问题归因，包含一级和二级分类。
   - issue_level1 和 issue_level2 必须使用 taxonomy 中的中文名称，不要输出英文枚举名。
   - result_score >= 5 且无明显问题时，可输出「其他 / 证据不足」。
4. 对低分任务给出 low_score_reason 和 improvement_suggestion；非低分任务这两个字段可留空。
5. token_total 和 credit_total 必须原样引用 context 中的 token_usage/credit_usage；context 未返回时留 0 或空，不要估算。

{QUERY_TAXONOMY}

{ISSUE_TAXONOMY}

输出必须是一个 JSON object，不要输出 Markdown，不要包代码块。字段固定如下：
{{
  "schema_version": "{QUALITY_SCHEMA_VERSION}",
  "prompt_version": "{PROMPT_VERSION}",
  "record_id": "string",
  "session_id": "string",
  "task_index": 0,
  "question_id": "string",
  "query_complexity": "simple|complex",
  "query_major_category": "string",
  "query_minor_category": "string",
  "token_total": 0,
  "credit_total": 0,
  "result_score": 0,
  "overall_score": 0,
  "is_low_score": true,
  "issue_level1": "string",
  "issue_level2": "string",
  "owner": "string",
  "low_score_reason": "string",
  "improvement_suggestion": "string",
  "evidence": ["string"],
  "confidence": 0.0
}}

评分校准：
- 9-10: 高质量完成需求，证据充分，产物可直接使用。
- 7-8: 基本完成，有轻微遗漏或效率问题。
- 5-6: 部分完成，但存在明显缺口。
- 3-4: 方向或执行有较大问题，用户需明显返工。
- 1-2: 几乎不可用，仅有少量相关内容。
- 0: 失败、超时、无输出、完全答非所问或关键产物缺失。
overall_score 必须等于 result_score。overall_score >= 5 时，low_score_reason 和 improvement_suggestion 可留空，不要为了 process_steps 为空强行输出低分原因。
"""

QUERY_MAJOR_CATEGORY_LABELS = {
    "market_intelligence_and_monitoring": "市场情报与监控",
    "market intelligence and monitoring": "市场情报与监控",
    "市场情报与监控": "市场情报与监控",
    "fundamental_research_and_comparative_analysis": "基本面研究与比较分析",
    "fundamental research and comparative analysis": "基本面研究与比较分析",
    "基本面研究与比较分析": "基本面研究与比较分析",
    "data_extraction_screening_and_structured_analytics": "数据提取筛选与结构化分析",
    "data extraction screening and structured analytics": "数据提取筛选与结构化分析",
    "数据提取筛选与结构化分析": "数据提取筛选与结构化分析",
    "quant_strategy_factor_research_and_backtesting": "量化策略因子与回测",
    "quant strategy factor research and backtesting": "量化策略因子与回测",
    "量化策略因子与回测": "量化策略因子与回测",
    "multimodal_reconstruction_and_research_deliverables": "多模态重建与研究交付物",
    "multimodal reconstruction and research deliverables": "多模态重建与研究交付物",
    "多模态重建与研究交付物": "多模态重建与研究交付物",
    "agent_workflow_engineering_and_automation": "Agent 工作流工程与自动化",
    "agent workflow engineering and automation": "Agent 工作流工程与自动化",
    "agent 工作流工程与自动化": "Agent 工作流工程与自动化",
    "other": "其他",
    "其他": "其他",
}

QUERY_MINOR_CATEGORY_LABELS = {
    "news_event_tracking": "新闻事件跟踪",
    "meeting_calendar_monitoring": "会议日程监控",
    "market_narrative_attribution": "市场叙事归因",
    "single_company_research": "单公司研究",
    "industry_theme_research": "行业主题研究",
    "peer_comparison": "同业比较",
    "credit_and_bond_research": "信用与债券研究",
    "cross_source_data_extraction": "跨来源数据提取",
    "rule_based_screening_ranking": "规则筛选排序",
    "historical_statistics_descriptive_analysis": "历史统计描述分析",
    "strategy_design_factor_definition": "策略设计与因子定义",
    "backtesting_and_performance_evaluation": "回测与绩效评估",
    "monitoring_dashboard_quant_product": "监控看板与量化产品",
    "image_pdf_table_reconstruction": "图像PDF表格重建",
    "excel_ppt_report_generation": "Excel/PPT/报告生成",
    "skill_tool_environment_enablement": "Skill/工具/环境配置",
    "scheduled_workflow_and_delivery": "定时工作流与投递",
    "cross_application_execution": "跨应用执行",
    "general_chat": "通用问答",
    "admin_or_setup": "管理或配置",
    "unclear_or_non_financial": "不明确或非金融问题",
}

ISSUE_LEVEL1_LABELS = {
    "data_issue": "数据类",
    "data issues": "数据类",
    "数据问题": "数据类",
    "数据类": "数据类",
    "process_issue": "算法类",
    "process issues": "算法类",
    "流程问题": "算法类",
    "tool_issue": "算法类",
    "tool issues": "算法类",
    "工具问题": "算法类",
    "reasoning_calculation_issue": "算法类",
    "reasoning and calculation issues": "算法类",
    "推理计算问题": "算法类",
    "delivery_issue": "业务类",
    "delivery issues": "业务类",
    "交付问题": "业务类",
    "product_issue": "业务类",
    "product issues": "业务类",
    "产品问题": "业务类",
    "other": "业务类",
    "其他": "业务类",
    "unknown": "业务类",
}

ISSUE_LEVEL2_LABELS = {
    "foreign_report": "外资研报",
    "外资研报": "外资研报",
    "foreign_minutes": "外资纪要",
    "外资纪要": "外资纪要",
    "meeting_minutes": "会议纪要",
    "会议纪要": "会议纪要",
    "roadshow_calendar": "路演日历",
    "路演日历": "路演日历",
    "report": "研报",
    "研报": "研报",
    "fixed_income": "固收",
    "固收": "固收",
    "stock_fundamentals": "个股基本面",
    "个股基本面": "个股基本面",
    "research_strategy_calendar": "调研、策略会日历",
    "调研、策略会日历": "调研、策略会日历",
    "调研策略会日历": "调研、策略会日历",
    "announcement": "公告",
    "公告": "公告",
    "uncategorized_data": "未分类",
    "未分类": "未分类",
    "technical_issue": "技术问题",
    "技术问题": "技术问题",
    "技术问题（@王能）": "技术问题",
    "技术问题(@王能)": "技术问题",
    "domestic_stock": "国内股票类",
    "国内股票类": "国内股票类",
    "overseas": "海外类",
    "海外类": "海外类",
    "fund_bluebook": "基金、蓝宝书类",
    "基金、蓝宝书类": "基金、蓝宝书类",
    "基金蓝宝书类": "基金、蓝宝书类",
    "other_business": "其他未归类",
    "其他未归类": "其他未归类",
    "其他 / 证据不足": "其他未归类",
    "其他/证据不足": "其他未归类",
    "data_missing": "未分类",
    "数据缺失": "未分类",
    "data_wrong": "未分类",
    "数据错误": "未分类",
    "data_outdated": "未分类",
    "数据过期": "未分类",
    "data_permission": "未分类",
    "数据权限不足": "未分类",
    "source_unavailable": "未分类",
    "数据源不可用": "未分类",
    "source_quality_low": "未分类",
    "数据质量低": "未分类",
    "task_timeout": "技术问题",
    "任务超时": "技术问题",
    "task_failed": "技术问题",
    "任务失败": "技术问题",
    "planning_error": "技术问题",
    "规划错误": "技术问题",
    "routing_error": "技术问题",
    "路由错误": "技术问题",
    "tool_error": "技术问题",
    "工具错误": "技术问题",
    "api_error": "技术问题",
    "接口错误": "技术问题",
    "sql_or_dsl_error": "技术问题",
    "SQL/DSL错误": "技术问题",
    "search_error": "技术问题",
    "搜索错误": "技术问题",
    "file_io_error": "技术问题",
    "文件读写错误": "技术问题",
    "calculation_error": "技术问题",
    "计算错误": "技术问题",
    "logic_error": "技术问题",
    "逻辑错误": "技术问题",
    "instruction_not_followed": "其他未归类",
    "未遵循指令": "其他未归类",
    "incomplete_execution": "其他未归类",
    "执行不完整": "其他未归类",
    "context_loss": "其他未归类",
    "上下文丢失": "其他未归类",
    "lark_or_delivery_error": "其他未归类",
    "飞书或投递错误": "其他未归类",
    "hallucination": "其他未归类",
    "幻觉": "其他未归类",
    "unsupported_inference": "其他未归类",
    "无依据推断": "其他未归类",
    "citation_mismatch": "其他未归类",
    "引用不匹配": "其他未归类",
    "format_error": "其他未归类",
    "格式错误": "其他未归类",
    "artifact_missing": "其他未归类",
    "产物缺失": "其他未归类",
    "artifact_wrong": "其他未归类",
    "产物错误": "其他未归类",
    "answer_too_shallow": "其他未归类",
    "回答过浅": "其他未归类",
    "user_need_mismatch": "其他未归类",
    "用户需求不匹配": "其他未归类",
    "product_bug": "其他未归类",
    "产品缺陷": "其他未归类",
    "missing_capability": "其他未归类",
    "能力缺失": "其他未归类",
    "permission_design_gap": "其他未归类",
    "权限设计缺口": "其他未归类",
    "ux_gap": "其他未归类",
    "体验缺口": "其他未归类",
    "unknown": "其他未归类",
    "未知": "其他未归类",
    "insufficient_evidence": "其他未归类",
    "证据不足": "其他未归类",
}

ISSUE_OWNER_BY_LEVEL2 = {
    "外资研报": "@周宛蓉",
    "外资纪要": "@周宛蓉",
    "会议纪要": "@王宝涵 @王意荃",
    "路演日历": "@王宝涵",
    "研报": "@王宝涵",
    "固收": "@王宝涵",
    "个股基本面": "@陆雯",
    "调研、策略会日历": "@陆雯",
    "公告": "@陆雯",
    "未分类": "@陆雯",
    "技术问题": "@王能",
    "国内股票类": "@韩字杰",
    "海外类": "@凤超",
    "基金、蓝宝书类": "@齐涤非",
    "其他未归类": "@王峰伊",
}

ISSUE_LEVEL1_BY_LEVEL2 = {
    "外资研报": "数据类",
    "外资纪要": "数据类",
    "会议纪要": "数据类",
    "路演日历": "数据类",
    "研报": "数据类",
    "固收": "数据类",
    "个股基本面": "数据类",
    "调研、策略会日历": "数据类",
    "公告": "数据类",
    "未分类": "数据类",
    "技术问题": "算法类",
    "国内股票类": "业务类",
    "海外类": "业务类",
    "基金、蓝宝书类": "业务类",
    "其他未归类": "业务类",
}

RUNNING_STATUS_TOKENS = {
    "running",
    "pending",
    "queued",
    "processing",
    "in_progress",
    "in progress",
    "进行中",
    "运行中",
    "排队",
    "待处理",
}

ENDED_STATUS_TOKENS = {
    "success",
    "succeeded",
    "completed",
    "complete",
    "done",
    "finished",
    "finish",
    "failed",
    "failure",
    "error",
    "timeout",
    "cancelled",
    "canceled",
    "terminated",
    "stopped",
    "成功",
    "完成",
    "已完成",
    "失败",
    "异常",
    "超时",
    "取消",
    "已取消",
}


TERMINAL_STATUS_FIELDS = ("status", "end_type", "task_status_desc", "state")


def record_id_from_ref(ref: dict[str, Any]) -> str:
    session_id = str(ref.get("session_id") or "")
    task_index = str(ref.get("task_index") or "")
    question_id = str(ref.get("question_id") or "")
    return ref.get("record_id") or f"{session_id}:{task_index}:{question_id}".strip(":")


def read_jsonl(path: str | Path) -> list[Any]:
    items = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if text:
                items.append(json.loads(text))
    return items


def write_jsonl(items: list[dict[str, Any]], path: str | None) -> None:
    text = "".join(paiobs.json_dumps(item, compact=True) + "\n" for item in items)
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def category_key(value: Any) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^\w\u4e00-\u9fff/]+", "_", str(value or "").strip().lower())).strip("_")


def chinese_label(value: Any, labels: dict[str, str], default: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if text in labels.values():
        return text
    return labels.get(text) or labels.get(category_key(text)) or text


def normalize_issue_level2(value: Any) -> str:
    return chinese_label(value, ISSUE_LEVEL2_LABELS, "其他未归类")


def issue_level1_for_level2(level2: str, fallback: Any = "") -> str:
    if level2 in ISSUE_LEVEL1_BY_LEVEL2:
        return ISSUE_LEVEL1_BY_LEVEL2[level2]
    normalized = chinese_label(fallback, ISSUE_LEVEL1_LABELS, "")
    if normalized in {"数据类", "算法类", "业务类"}:
        return normalized
    return "业务类"


def issue_owner_for_level2(level2: str) -> str:
    return ISSUE_OWNER_BY_LEVEL2.get(level2) or ISSUE_OWNER_BY_LEVEL2["其他未归类"]


def terminal_status_values(item: dict[str, Any]) -> list[str]:
    values = []
    for key in TERMINAL_STATUS_FIELDS:
        value = str(item.get(key) or "").strip().lower()
        if value:
            values.append(value)
    return values


def is_ended_ref(item: dict[str, Any]) -> bool:
    status_values = terminal_status_values(item)
    if any(token in value for value in status_values for token in RUNNING_STATUS_TOKENS):
        return False
    if any(token in value for value in status_values for token in ENDED_STATUS_TOKENS):
        return True
    if item.get("response_time"):
        return True
    success = item.get("success")
    if success is True:
        return True
    return False


def is_ended_context(context: dict[str, Any]) -> bool:
    info = context.get("query_info") if isinstance(context.get("query_info"), dict) else {}
    metadata = context.get("task_metadata") if isinstance(context.get("task_metadata"), dict) else {}
    candidate = dict(metadata)
    candidate.update(info)
    for key in ("response_time", "success", *TERMINAL_STATUS_FIELDS):
        if key not in candidate and context.get(key) not in (None, "", [], {}):
            candidate[key] = context.get(key)
    return is_ended_ref(candidate)


def filter_ended_contexts(contexts: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if not getattr(args, "ended_only", False):
        return contexts
    filtered = [context for context in contexts if is_ended_context(context)]
    progress_log(args, f"collect contexts: ended-only filter {len(contexts)} -> {len(filtered)}")
    return filtered


def filter_ended_refs(items: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if not getattr(args, "ended_only", False):
        return items
    filtered = [item for item in items if is_ended_ref(item)]
    progress_log(args, f"collect refs: ended-only filter {len(items)} -> {len(filtered)}")
    return filtered


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def compact_json(value: Any, max_chars: int) -> str:
    text = paiobs.json_dumps(value, compact=False)
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "\n...[truncated]"
    return text


def analysis_value_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return paiobs.json_dumps(value)
    return str(value or "")


def clip_text(value: Any, max_chars: int) -> str:
    text = str(value or "")
    if max_chars > 0 and len(text) > max_chars:
        return text[: max(0, max_chars - 15)].rstrip() + "\n...[truncated]"
    return text


def progress_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "progress", True))


def progress_log(args: argparse.Namespace, message: str) -> None:
    if not progress_enabled(args):
        return
    sys.stderr.write(f"[paiobs-ai] {message}\n")
    sys.stderr.flush()


def progress_status(
    args: argparse.Namespace,
    *,
    phase: str,
    completed: int,
    total: int,
    ok: int,
    errors: int,
    started: float,
    last_emit: float,
    force: bool = False,
) -> float:
    if not progress_enabled(args):
        return last_emit
    now = time.time()
    interval = float(getattr(args, "progress_interval", DEFAULT_PROGRESS_INTERVAL) or DEFAULT_PROGRESS_INTERVAL)
    if not force and now - last_emit < interval:
        return last_emit
    elapsed = max(0.001, now - started)
    rate = completed / elapsed
    remaining = max(0, total - completed)
    eta_text = f"{remaining / rate:.1f}s" if rate > 0 else "unknown"
    progress_log(
        args,
        f"{phase}: {completed}/{total} ok={ok} errors={errors} elapsed={elapsed:.1f}s rate={rate:.2f}/s eta={eta_text}",
    )
    return now


def progress_percent(completed: int, total: int) -> str:
    if total <= 0:
        return "100.0%"
    return f"{(completed / total) * 100:.1f}%"


def progress_eta(completed: int, total: int, started: float) -> str:
    elapsed = max(0.001, time.time() - started)
    rate = completed / elapsed if completed > 0 else 0.0
    if rate <= 0:
        return "unknown"
    return f"{max(0, total - completed) / rate:.1f}s"


def progress_item_label(ref: dict[str, Any]) -> str:
    normalized = normalize_ref_item(ref)
    if ref.get("record_id") and not normalized.get("record_id"):
        normalized["record_id"] = ref.get("record_id")
    parts = [
        f"record_id={record_id_from_ref(normalized)}",
        f"session_id={normalized.get('session_id') or ''}",
        f"task_index={normalized.get('task_index') or ''}",
    ]
    if normalized.get("question_id"):
        parts.append(f"question_id={normalized.get('question_id')}")
    return " ".join(parts)


def progress_item_start(args: argparse.Namespace, phase: str, index: int, total: int, ref: dict[str, Any]) -> None:
    progress_log(
        args,
        f"{phase} item start: {index}/{total} submitted={progress_percent(index, total)} {progress_item_label(ref)}",
    )


def progress_item_done(
    args: argparse.Namespace,
    phase: str,
    index: int,
    total: int,
    *,
    ok_count: int,
    error_count: int,
    item_started: float,
    batch_started: float,
    skip_count: int = 0,
    result: dict[str, Any] | None = None,
    error: Exception | str | None = None,
) -> None:
    item_elapsed = time.time() - item_started
    completed = ok_count + error_count + skip_count
    elapsed = time.time() - batch_started
    rate = completed / max(0.001, elapsed)
    if error is None:
        score = result.get("overall_score") if isinstance(result, dict) else ""
        low_score = result.get("is_low_score") if isinstance(result, dict) else ""
        provider = result.get("analysis_provider") if isinstance(result, dict) else ""
        progress_log(
            args,
            (
                f"{phase} item done: {index}/{total} ({progress_percent(completed, total)}) "
                f"status=ok item_elapsed={item_elapsed:.2f}s ok={ok_count} errors={error_count} skipped={skip_count} "
                f"score={score if score not in (None, '') else 'n/a'} low_score={low_score if low_score not in (None, '') else 'n/a'} "
                f"provider={provider or 'n/a'} total_elapsed={elapsed:.1f}s rate={rate:.2f}/s eta={progress_eta(completed, total, batch_started)}"
            ),
        )
        return
    error_text = clip_text(str(error), 240).replace("\n", " ")
    progress_log(
        args,
        (
            f"{phase} item done: {index}/{total} ({progress_percent(completed, total)}) "
            f"status=error item_elapsed={item_elapsed:.2f}s ok={ok_count} errors={error_count} skipped={skip_count} "
            f"error={error_text} total_elapsed={elapsed:.1f}s rate={rate:.2f}/s eta={progress_eta(completed, total, batch_started)}"
        ),
    )


def progress_item_skipped(
    args: argparse.Namespace,
    phase: str,
    index: int,
    total: int,
    *,
    ok_count: int,
    error_count: int,
    skip_count: int,
    item_started: float,
    batch_started: float,
    reason: str,
) -> None:
    item_elapsed = time.time() - item_started
    completed = ok_count + error_count + skip_count
    elapsed = time.time() - batch_started
    rate = completed / max(0.001, elapsed)
    progress_log(
        args,
        (
            f"{phase} item done: {index}/{total} ({progress_percent(completed, total)}) "
            f"status=skipped reason={reason} item_elapsed={item_elapsed:.2f}s "
            f"ok={ok_count} errors={error_count} skipped={skip_count} "
            f"total_elapsed={elapsed:.1f}s rate={rate:.2f}/s eta={progress_eta(completed, total, batch_started)}"
        ),
    )


def parse_filter_time(value: Any) -> datetime:
    text = str(value or "").strip().replace("T", " ")
    if not text:
        raise ValueError("empty time")
    if "." in text:
        text = text.split(".", 1)[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(text)


def format_filter_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def split_time_windows(start: datetime, end: datetime, step: timedelta) -> list[tuple[datetime, datetime]]:
    windows = []
    current = start
    while current < end:
        nxt = min(end, current + step)
        windows.append((current, nxt))
        current = nxt
    return windows


def ref_item_key(item: dict[str, Any]) -> str:
    session_id = str(item.get("session_id") or "")
    task_index = str(item.get("task_index") or "")
    question_id = str(item.get("question_id") or "")
    return f"{session_id}:{task_index}:{question_id}"


def normalize_ref_item(item: dict[str, Any]) -> dict[str, Any]:
    question = item.get("question") or item.get("query_text") or item.get("question_preview") or item.get("match_preview")
    institution = item.get("user_institution") or item.get("institution")
    normalized = {
        "session_id": item.get("session_id"),
        "task_index": item.get("task_index"),
        "question_id": item.get("question_id"),
        "request_time": item.get("request_time"),
        "response_time": item.get("response_time"),
        "status": item.get("status"),
        "end_type": item.get("end_type"),
        "task_status_desc": item.get("task_status_desc"),
        "success": item.get("success"),
        "entry_scene": item.get("entry_scene"),
        "scheduled": item.get("scheduled"),
        "is_web_search": item.get("is_web_search"),
        "user_id": item.get("user_id"),
        "user_name": item.get("user_name") or item.get("username"),
        "user_institution": institution,
        "institution": institution,
        "institution_nature": item.get("institution_nature"),
        "inst_type": item.get("inst_type"),
        "product_type": item.get("product_type"),
        "user_type": item.get("user_type"),
        "user_role": item.get("user_role"),
        "duration_seconds": item.get("duration_seconds") or item.get("dur") or item.get("duration"),
        "total_elapsed": item.get("total_elapsed"),
        "average_time": item.get("average_time"),
        "question": question,
        "env": item.get("env"),
        "target": item.get("target"),
        "history_db": item.get("history_db"),
        "source_kind": item.get("source_kind"),
        "system": item.get("system"),
        "error": item.get("error"),
        "session_question_count": item.get("session_question_count"),
    }
    return {key: value for key, value in normalized.items() if value not in (None, "", [], {})}


def context_from_ref_item(item: dict[str, Any], mode: str = "gateway_direct") -> dict[str, Any]:
    info = normalize_ref_item(item)
    return {
        "schema_version": "paiwork-task-quality-context/v1",
        "context_mode": mode,
        "record_id": record_id_from_ref(info),
        "query_info": info,
        "result": {
            "final_answer_excerpt": item.get("answer") or item.get("answer_preview") or item.get("final_answer_excerpt") or "",
        },
        "token_usage": item.get("token_usage") or {},
    }


def collect_search_items_adaptive(
    client: paiobs.PaiObsClient,
    *,
    filters: dict[str, Any],
    profile: str,
    limit: int,
    page_limit: int,
    initial_window_minutes: float,
    min_slice_seconds: float,
    max_workers: int,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    start_dt = parse_filter_time(filters.get("start_time"))
    end_dt = parse_filter_time(filters.get("end_time"))
    if end_dt <= start_dt:
        raise SystemExit("end_time must be greater than start_time")

    page_limit = max(1, int(page_limit or DEFAULT_SEARCH_PAGE_LIMIT))
    step = timedelta(minutes=max(0.1, float(initial_window_minutes or DEFAULT_SEARCH_SLICE_MINUTES)))
    min_slice_seconds = max(0.1, float(min_slice_seconds or 1.0))
    max_workers = max(1, int(max_workers or DEFAULT_SEARCH_WORKERS))
    target_limit = max(1, int(limit or DEFAULT_ANALYZE_LIMIT))
    pending = split_time_windows(start_dt, end_dt, step)
    seen: dict[str, dict[str, Any]] = {}
    query_count = 0
    round_no = 0
    progress_log(args, f"collect refs: adaptive search start windows={len(pending)} limit={target_limit}")

    def fetch_window(start: datetime, end: datetime) -> tuple[datetime, datetime, list[dict[str, Any]]]:
        window_filters = dict(filters)
        window_filters["start_time"] = format_filter_time(start)
        window_filters["end_time"] = format_filter_time(end)
        payload = search_questions(client, filters=window_filters, profile=profile, limit=page_limit)
        batch = payload.get("items") if isinstance(payload, dict) else []
        return start, end, [item for item in batch or [] if isinstance(item, dict)]

    while pending:
        round_no += 1
        next_pending: list[tuple[datetime, datetime]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(fetch_window, start, end) for start, end in pending]
            for future in as_completed(futures):
                start, end, items = future.result()
                query_count += 1
                span_seconds = (end - start).total_seconds()
                if len(items) >= page_limit and span_seconds > min_slice_seconds:
                    midpoint = start + (end - start) / 2
                    next_pending.append((start, midpoint))
                    next_pending.append((midpoint, end))
                    continue
                for item in items:
                    key = ref_item_key(item)
                    if key and key not in seen:
                        seen[key] = item
        progress_log(
            args,
            f"collect refs: round={round_no} queries={query_count} unique={len(seen)} split_next={len(next_pending)}",
        )
        pending = next_pending

    items = sorted(
        seen.values(),
        key=lambda item: (
            str(item.get("request_time") or ""),
            str(item.get("session_id") or ""),
            safe_int(item.get("task_index")),
        ),
    )
    if len(items) > target_limit:
        progress_log(args, f"collect refs: truncate {len(items)} -> {target_limit}")
        items = items[:target_limit]
    return items


def parse_simple_yaml_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if "#" in text and not (text.startswith('"') or text.startswith("'")):
        text = text.split("#", 1)[0].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [str(parse_simple_yaml_scalar(item.strip())) for item in inner.split(",")]
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def load_deeptask_llm_config() -> dict[str, str]:
    """Read the local DeepTask LLM config without requiring PyYAML."""
    global _AI_CONFIG_CACHE
    if _AI_CONFIG_CACHE is not None:
        return _AI_CONFIG_CACHE
    path = Path(os.environ.get("PAI_OBS_AI_CONFIG_PATH") or DEFAULT_DEEPTASK_LLM_CONFIG)
    config: dict[str, str] = {}
    if not path.is_file():
        _AI_CONFIG_CACHE = config
        return config
    in_llm = False
    llm_indent = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if not in_llm:
            if stripped == "llm:":
                in_llm = True
                llm_indent = indent
            continue
        if indent <= llm_indent and not stripped.startswith("#"):
            break
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        if key not in {"base_url", "api_key", "model", "timeout_sec", "max_completion_tokens", "fallback_models"}:
            continue
        parsed = parse_simple_yaml_scalar(value)
        if isinstance(parsed, list):
            config[key] = ",".join(str(item) for item in parsed if str(item).strip())
        else:
            config[key] = str(parsed).strip()
    _AI_CONFIG_CACHE = config
    return config


def skill_ai_config() -> dict[str, str]:
    return paiobs.load_skill_local_config()


def env_or_config(names: list[str], local_config: dict[str, str], deeptask_config: dict[str, str], deeptask_key: str = "") -> str:
    for name in names:
        value = os.environ.get(name) or local_config.get(name)
        if value not in (None, ""):
            return str(value).strip()
    if deeptask_key:
        return str(deeptask_config.get(deeptask_key) or "").strip()
    return ""


def split_model_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;\s]+", str(value or "")) if item.strip()]


def default_ai_provider() -> str:
    local_config = skill_ai_config()
    return (
        os.environ.get("PAI_OBS_AI_PROVIDER")
        or local_config.get("PAI_OBS_AI_PROVIDER")
        or DEFAULT_AI_PROVIDER
    )


def resolve_openai_compatible_config(args: argparse.Namespace) -> dict[str, Any]:
    local_config = skill_ai_config()
    deeptask_config = load_deeptask_llm_config()
    api_key = (
        getattr(args, "ai_api_key", "")
        or env_or_config(
            ["PAI_OBS_AI_API_KEY", "GLOBAL_DATA_LLM_API_KEY", "OVERSEADATA_LLM_API_KEY", "OPENAI_API_KEY"],
            local_config,
            deeptask_config,
            "api_key",
        )
    ).strip()
    base_url = (
        getattr(args, "ai_base_url", "")
        or env_or_config(
            ["PAI_OBS_AI_BASE_URL", "GLOBAL_DATA_LLM_BASE_URL", "OVERSEADATA_LLM_BASE_URL", "OPENAI_BASE_URL"],
            local_config,
            deeptask_config,
            "base_url",
        )
        or DEFAULT_AI_BASE_URL
    ).strip()
    model = (
        getattr(args, "ai_model", "")
        or env_or_config(
            ["PAI_OBS_AI_MODEL", "GLOBAL_DATA_LLM_MODEL", "OVERSEADATA_LLM_MODEL", "OPENAI_MODEL"],
            local_config,
            deeptask_config,
            "model",
        )
        or DEFAULT_MODEL
    ).strip()
    fallback_models = split_model_list(
        getattr(args, "ai_fallback_models", "")
        or env_or_config(
            ["PAI_OBS_AI_FALLBACK_MODELS", "GLOBAL_DATA_LLM_FALLBACK_MODELS", "OVERSEADATA_LLM_FALLBACK_MODELS"],
            local_config,
            deeptask_config,
            "fallback_models",
        )
    )
    fallback_models = [item for item in fallback_models if item != model]
    timeout = getattr(args, "ai_timeout", None)
    if timeout in (None, ""):
        timeout = (
            env_or_config(["PAI_OBS_AI_TIMEOUT", "GLOBAL_DATA_LLM_TIMEOUT_SEC", "OVERSEADATA_LLM_TIMEOUT_SEC"], local_config, deeptask_config, "timeout_sec")
            or DEFAULT_AI_TIMEOUT
        )
    max_tokens = getattr(args, "max_tokens", None)
    if max_tokens in (None, ""):
        max_tokens = (
            env_or_config(["PAI_OBS_AI_MAX_TOKENS", "GLOBAL_DATA_LLM_MAX_COMPLETION_TOKENS", "OVERSEADATA_LLM_MAX_COMPLETION_TOKENS"], local_config, deeptask_config, "max_completion_tokens")
            or DEFAULT_MAX_TOKENS
        )
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "fallback_models": fallback_models,
        "timeout": float(timeout),
        "max_tokens": int(float(max_tokens)),
    }


def ai_retries(args: argparse.Namespace) -> int:
    local_config = skill_ai_config()
    value = getattr(args, "ai_retries", None)
    if value in (None, ""):
        value = os.environ.get("PAI_OBS_AI_RETRIES") or local_config.get("PAI_OBS_AI_RETRIES") or 1
    return max(0, int(value))


def status_text(context: dict[str, Any]) -> str:
    info = context.get("query_info") if isinstance(context.get("query_info"), dict) else {}
    return str(
        info.get("status")
        or info.get("end_type")
        or info.get("task_status_desc")
        or context.get("status")
        or context.get("end_type")
        or context.get("task_status_desc")
        or ""
    ).strip().lower()


def is_failed_or_timeout(context: dict[str, Any]) -> bool:
    status = status_text(context)
    if any(token in status for token in ["failed", "failure", "error", "timeout", "超时", "失败", "异常"]):
        return True
    info = context.get("query_info") if isinstance(context.get("query_info"), dict) else {}
    if info.get("success") is False or context.get("success") is False:
        return True
    duration = (
        paiobs.safe_number(info.get("duration_seconds"))
        or paiobs.safe_number(info.get("total_elapsed"))
        or paiobs.safe_number(info.get("average_time"))
    )
    if duration is not None and duration >= DEFAULT_TIMEOUT_SECONDS and info.get("success") is not True:
        return True
    return False


def find_token_usage(payload: Any) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for _path, node in paiobs.iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        keys = set(node)
        if "total_tokens" in keys or "totalTokens" in keys or "promptTokens" in keys or "completionTokens" in keys:
            candidate = {
                "total_tokens": node.get("total_tokens") or node.get("totalTokens") or node.get("total"),
                "prompt_tokens": node.get("prompt_tokens") or node.get("promptTokens"),
                "completion_tokens": node.get("completion_tokens") or node.get("completionTokens"),
                "cached_tokens": node.get("cached_tokens") or node.get("cachedTokens"),
                "llm_call_count": node.get("llm_call_count") or node.get("llmCallCount"),
                "model": node.get("model") or node.get("llmModel") or node.get("realModel"),
            }
            if candidate.get("total_tokens") and not best:
                best = candidate
    return {key: value for key, value in best.items() if value not in (None, "", [], {})}


def find_credit_usage(payload: Any) -> dict[str, Any]:
    best: dict[str, Any] = {}
    total_keys = (
        "total_credits",
        "totalCredits",
        "credit_total",
        "creditTotal",
        "credits",
        "credit",
        "used_credits",
        "usedCredits",
        "consumed_credits",
        "consumedCredits",
        "consumed_points",
        "consumedPoints",
        "credit_cost",
        "creditCost",
    )
    prompt_keys = ("prompt_credits", "promptCredits", "input_credits", "inputCredits")
    completion_keys = ("completion_credits", "completionCredits", "output_credits", "outputCredits")
    for _path, node in paiobs.iter_nodes(payload):
        if not isinstance(node, dict):
            continue
        total = next((node.get(key) for key in total_keys if node.get(key) not in (None, "")), None)
        if total in (None, ""):
            continue
        candidate = {
            "total_credits": total,
            "prompt_credits": next((node.get(key) for key in prompt_keys if node.get(key) not in (None, "")), None),
            "completion_credits": next((node.get(key) for key in completion_keys if node.get(key) not in (None, "")), None),
            "model": node.get("model") or node.get("llmModel") or node.get("realModel"),
        }
        if not best:
            best = candidate
            break
    return {key: value for key, value in best.items() if value not in (None, "", [], {})}


def compact_query_info(info: dict[str, Any]) -> dict[str, Any]:
    compacted = {key: info.get(key) for key in QUALITY_QUERY_INFO_KEYS if info.get(key) not in (None, "", [], {})}
    if "duration_seconds" not in compacted:
        duration = first_non_empty(info.get("total_elapsed"), info.get("average_time"), info.get("duration"), info.get("dur"))
        if duration is not None:
            compacted["duration_seconds"] = duration
    if "question" in compacted:
        compacted["question"] = str(compacted["question"])
    return compacted


def compact_file_items(files: list[dict[str, Any]], limit: int = 16) -> list[dict[str, Any]]:
    compacted = []
    for file_item in files[:limit]:
        if not isinstance(file_item, dict):
            continue
        compacted.append(
            {
                key: value
                for key, value in {
                    "name": clip_text(file_item.get("name"), 160),
                    "file_type": file_item.get("file_type"),
                    "roles": file_item.get("roles") or [],
                    "file_path": file_item.get("file_path"),
                    "raw_file_path": file_item.get("raw_file_path"),
                    "file_id": file_item.get("file_id"),
                    "target": file_item.get("target"),
                    "owner_user_id": file_item.get("owner_user_id"),
                    "source_path": file_item.get("source_path"),
                }.items()
                if value not in (None, "", [], {})
            }
        )
    return compacted


def summarize_files(files: list[dict[str, Any]]) -> dict[str, Any]:
    role_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    output_like_count = 0
    for file_item in files:
        if not isinstance(file_item, dict):
            continue
        roles = [str(role) for role in (file_item.get("roles") or [])]
        for role in roles:
            role_counts[role] = role_counts.get(role, 0) + 1
        file_type = str(file_item.get("file_type") or "unknown")
        type_counts[file_type] = type_counts.get(file_type, 0) + 1
        text = " ".join([str(file_item.get("name") or ""), str(file_item.get("file_path") or ""), " ".join(roles)]).lower()
        if any(token in text for token in ["output", "result", "answer", "generated", "产物", "结果"]):
            output_like_count += 1
    return {
        "total_count": len(files),
        "output_like_count": output_like_count,
        "role_counts": role_counts,
        "file_type_counts": type_counts,
        "sample_files": compact_file_items(files, limit=10),
    }


def compact_skill_items(skills: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    compacted = []
    for item in skills[:limit]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                key: value
                for key, value in {
                    "skill_name": item.get("skill_name"),
                    "occurrence_count": item.get("occurrence_count"),
                }.items()
                if value not in (None, "", [], {})
            }
        )
    return compacted


def compact_process_steps(steps: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    if len(steps) <= limit:
        chosen = steps
    else:
        head = max(2, limit // 3)
        tail = max(1, limit - head)
        chosen = [*steps[:head], *steps[-tail:]]
    compacted = []
    for step in chosen:
        if not isinstance(step, dict):
            continue
        compacted.append(
            {
                key: value
                for key, value in {
                    "index": step.get("index"),
                    "elapsed_seconds": step.get("elapsed_seconds"),
                    "task_step_desc": clip_text(step.get("task_step_desc"), 220),
                    "task_status_desc": clip_text(step.get("task_status_desc"), 120),
                    "subtask_count": step.get("subtask_count"),
                    "subtask_titles": [clip_text(title, 120) for title in (step.get("subtask_titles") or [])[:5]],
                    "file_change_count": step.get("file_change_count"),
                }.items()
                if value not in (None, "", [], {})
            }
        )
    return compacted


def summarize_process_steps(steps: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed_values = [
        float(step.get("elapsed_seconds"))
        for step in steps
        if isinstance(step, dict) and paiobs.safe_number(step.get("elapsed_seconds")) is not None
    ]
    last = steps[-1] if steps else {}
    return {
        "step_count": len(steps),
        "max_elapsed_seconds": max(elapsed_values) if elapsed_values else None,
        "last_status": last.get("task_status_desc") if isinstance(last, dict) else "",
        "notable_steps": compact_process_steps(steps, limit=12),
    }


def compact_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    return {
        key: value
        for key, value in {
            "final_answer_excerpt": clip_text(result.get("final_answer_excerpt"), 1800),
            "source_counts": result.get("source_counts") or {},
            "file_counts": result.get("file_counts") or {},
            "sources": [
                {
                    "title": clip_text(item.get("title") or item.get("name") or item.get("source_title"), 160),
                    "url": item.get("url") or item.get("source_url") or item.get("domain"),
                }
                for item in (result.get("sources") or [])[:8]
                if isinstance(item, dict)
            ],
        }.items()
        if value not in (None, "", [], {})
    }


def compact_quality_context(
    context: dict[str, Any],
    *,
    include_process: bool = False,
    include_raw_context: bool = False,
    max_context_chars: int = 0,
) -> dict[str, Any]:
    info = context.get("query_info") if isinstance(context.get("query_info"), dict) else {}
    files = context.get("input_files") if isinstance(context.get("input_files"), list) else []
    skills = context.get("skills") if isinstance(context.get("skills"), list) else []
    steps = context.get("process_steps") if isinstance(context.get("process_steps"), list) else []
    result = context.get("result") if isinstance(context.get("result"), dict) else {}
    compacted = {
        "schema_version": "paiwork-task-quality-context/v1",
        "context_mode": "compact_with_process" if include_process else "bulk_fast",
        "record_id": context.get("record_id") or record_id_from_ref(info),
        "query_info": compact_query_info(info),
        "input_files": compact_file_items(files, limit=16),
        "file_summary": context.get("file_summary") if isinstance(context.get("file_summary"), dict) else summarize_files(files),
        "final_output_files": context.get("final_output_files") if isinstance(context.get("final_output_files"), list) else final_output_file_items(files, None, limit=8),
        "skills": compact_skill_items(skills, limit=20),
        "result": compact_result_summary(result),
        "token_usage": context.get("token_usage") if isinstance(context.get("token_usage"), dict) else {},
        "credit_usage": context.get("credit_usage") if isinstance(context.get("credit_usage"), dict) else {},
    }
    if include_process:
        compacted["process_summary"] = context.get("process_summary") if isinstance(context.get("process_summary"), dict) else summarize_process_steps(steps)
        compacted["process_steps"] = compact_process_steps(steps, limit=12)
    if include_raw_context and context.get("raw_context_excerpt"):
        compacted["raw_context_excerpt"] = clip_text(context.get("raw_context_excerpt"), max_context_chars)
    return compacted


def output_file_candidates(files: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    scored = []
    for file_item in files:
        roles = ",".join(file_item.get("roles") or []).lower()
        text = " ".join(str(file_item.get(key) or "") for key in ("name", "file_path", "raw_file_path", "source_path")).lower()
        score = 0
        if any(token in roles for token in ["output", "result", "answer", "generated"]):
            score += 3
        if any(token in text for token in ["output", "outputs", "/img/", "result", "answer", "final", "generated", "file_change", "产物", "结果"]):
            score += 2
        if file_item.get("file_path") or file_item.get("raw_file_path"):
            score += 1
        scored.append((score, file_item))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item for score, item in scored if score > 1][:limit]


def file_proxy_url(client: paiobs.PaiObsClient | None, file_item: dict[str, Any], *, endpoint: str = "/api/files/content") -> str:
    if client is None:
        return ""
    file_path = file_item.get("file_path") or file_item.get("raw_file_path") or ""
    if not file_path:
        return ""
    query: dict[str, Any] = {
        "env": client.env,
        "target": file_item.get("target") or "",
        "name": file_item.get("name") or "",
        "owner_user_id": file_item.get("owner_user_id") or "",
        "file_id": file_item.get("file_id") or "",
        "file_path": file_path,
    }
    fallback_path = file_item.get("raw_file_path") or ""
    if fallback_path and fallback_path != file_path:
        query["fallback_path"] = [fallback_path]
    query = {key: value for key, value in query.items() if value not in (None, "", [], {})}
    return f"{client.base_url.rstrip('/')}{endpoint}?{urlencode(query, doseq=True)}"


def final_output_file_items(files: list[dict[str, Any]], client: paiobs.PaiObsClient | None, limit: int = 8) -> list[dict[str, Any]]:
    output_files = []
    for file_item in output_file_candidates(files, limit):
        output_files.append(
            {
                key: value
                for key, value in {
                    "name": file_item.get("name"),
                    "file_type": file_item.get("file_type"),
                    "roles": file_item.get("roles") or [],
                    "file_path": file_item.get("file_path"),
                    "raw_file_path": file_item.get("raw_file_path"),
                    "file_id": file_item.get("file_id"),
                    "target": file_item.get("target"),
                    "owner_user_id": file_item.get("owner_user_id"),
                    "source_path": file_item.get("source_path"),
                    "content_url": file_proxy_url(client, file_item, endpoint="/api/files/content"),
                    "preview_url": file_proxy_url(client, file_item, endpoint="/api/files/preview"),
                }.items()
                if value not in (None, "", [], {})
            }
        )
    return output_files


def preview_file(client: paiobs.PaiObsClient, file_item: dict[str, Any], max_chars: int) -> dict[str, Any]:
    args = argparse.Namespace(
        path=file_item.get("file_path") or file_item.get("raw_file_path") or "",
        file_path=file_item.get("file_path") or file_item.get("raw_file_path") or "",
        target=file_item.get("target") or "",
        name=file_item.get("name") or "",
        fallback_path=[file_item.get("raw_file_path") or ""],
        owner_user_id=file_item.get("owner_user_id") or "",
        file_id=file_item.get("file_id") or "",
        local_path="",
    )
    payload = client.request("GET", "/api/files/preview", query=paiobs.build_file_query(args, client), internal=False)
    text = ""
    if isinstance(payload, dict):
        for key in ("text", "content", "preview", "data"):
            if isinstance(payload.get(key), str):
                text = payload[key]
                break
    return {
        "name": file_item.get("name"),
        "file_path": file_item.get("file_path") or file_item.get("raw_file_path"),
        "preview": text[:max_chars] if max_chars > 0 else text,
        "preview_chars": len(text),
    }


def normalize_context(
    payload: dict[str, Any],
    *,
    client: paiobs.PaiObsClient | None = None,
    include_file_previews: bool = False,
    include_process: bool = False,
    include_raw_context: bool = False,
    max_file_previews: int = 3,
    file_preview_chars: int = 2000,
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
) -> dict[str, Any]:
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    query_info = paiobs.extract_query_info(payload)
    files = paiobs.extract_files(payload)
    skills = paiobs.extract_skills(payload)
    process_steps = paiobs.extract_process_steps(payload, limit=160) if include_process else []
    result = paiobs.extract_result_summary(payload)
    context = {
        "schema_version": "paiwork-task-quality-context/v1",
        "context_mode": "compact_with_process" if include_process else "bulk_fast",
        "record_id": record_id_from_ref(query_info),
        "query_info": compact_query_info(query_info),
        "input_files": compact_file_items(files, limit=16),
        "file_summary": summarize_files(files),
        "final_output_files": final_output_file_items(files, client, limit=8),
        "skills": compact_skill_items(skills, limit=20),
        "result": compact_result_summary(result),
        "token_usage": find_token_usage(payload),
        "credit_usage": find_credit_usage(payload),
    }
    if include_process:
        context["process_summary"] = summarize_process_steps(process_steps)
        context["process_steps"] = compact_process_steps(process_steps, limit=12)
    if include_raw_context and max_context_chars > 0:
        context["raw_context_excerpt"] = compact_json(payload, max_context_chars)
    if include_file_previews and client is not None:
        previews = []
        for file_item in output_file_candidates(files, max_file_previews):
            try:
                previews.append(preview_file(client, file_item, file_preview_chars))
            except Exception as exc:
                previews.append(
                    {
                        "name": file_item.get("name"),
                        "file_path": file_item.get("file_path") or file_item.get("raw_file_path"),
                        "error": str(exc),
                    }
                )
        context["final_file_previews"] = previews
    return context


def quality_prompt(context: dict[str, Any]) -> str:
    return (
        UNIFIED_INSTRUCTION.strip()
        + "\n\n任务 context JSON:\n"
        + "```json\n"
        + compact_json(context, 0)
        + "\n```"
    )


def extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.S)
    if fenced:
        raw = fenced.group(1)
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(raw[index:])
            if isinstance(value, dict):
                return value
        except ValueError:
                continue
    raise ValueError("LLM output did not contain a JSON object")


def extract_quality_json(text: str) -> dict[str, Any]:
    parsed = extract_json_object(text)
    for key in ("analysis", "result", "output", "data"):
        inner = parsed.get(key) if isinstance(parsed, dict) else None
        if isinstance(inner, dict) and (
            inner.get("schema_version") == QUALITY_SCHEMA_VERSION or "overall_score" in inner or "result_score" in inner
        ):
            return inner
        if isinstance(inner, str) and inner.strip():
            try:
                inner_parsed = extract_json_object(inner)
            except ValueError:
                continue
            if inner_parsed.get("schema_version") == QUALITY_SCHEMA_VERSION or "overall_score" in inner_parsed or "result_score" in inner_parsed:
                return inner_parsed
    return parsed


def normalize_quality_result(result: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    info = context.get("query_info") if isinstance(context.get("query_info"), dict) else {}
    token_usage = context.get("token_usage") if isinstance(context.get("token_usage"), dict) else {}
    credit_usage = context.get("credit_usage") if isinstance(context.get("credit_usage"), dict) else {}
    result_summary = context.get("result") if isinstance(context.get("result"), dict) else {}
    normalized = dict(result)
    normalized["schema_version"] = QUALITY_SCHEMA_VERSION
    normalized.setdefault("prompt_version", PROMPT_VERSION)
    normalized["record_id"] = str(normalized.get("record_id") or context.get("record_id") or record_id_from_ref(info))
    normalized["session_id"] = str(normalized.get("session_id") or info.get("session_id") or "")
    normalized["task_index"] = safe_int(normalized.get("task_index") or info.get("task_index"))
    normalized["question_id"] = str(normalized.get("question_id") or info.get("question_id") or "")
    result_score = normalized.get("result_score")
    if result_score in (None, ""):
        result_score = normalized.get("overall_score")
    try:
        normalized["result_score"] = max(0, min(10, float(result_score)))
    except (TypeError, ValueError):
        normalized["result_score"] = 0
    normalized["overall_score"] = normalized["result_score"]
    normalized.pop("process_score", None)
    normalized.pop("issue_inference", None)
    normalized["is_low_score"] = bool(float(normalized.get("overall_score") or 0) < 5)
    normalized.setdefault("query_complexity", "complex")
    normalized["query_major_category"] = chinese_label(
        normalized.get("query_major_category"),
        QUERY_MAJOR_CATEGORY_LABELS,
        "其他",
    )
    normalized["query_minor_category"] = chinese_label(
        normalized.get("query_minor_category"),
        QUERY_MINOR_CATEGORY_LABELS,
        "不明确或非金融问题",
    )
    normalized["issue_level2"] = normalize_issue_level2(normalized.get("issue_level2"))
    normalized["issue_level1"] = issue_level1_for_level2(normalized["issue_level2"], normalized.get("issue_level1"))
    normalized["owner"] = issue_owner_for_level2(normalized["issue_level2"])
    if normalized["is_low_score"]:
        normalized.setdefault("low_score_reason", "")
    else:
        normalized["low_score_reason"] = ""
    normalized.setdefault("improvement_suggestion", "")
    metadata = normalized.get("task_metadata") if isinstance(normalized.get("task_metadata"), dict) else {}
    metadata.setdefault("user_id", info.get("user_id"))
    metadata.setdefault("user_name", info.get("user_name"))
    metadata.setdefault("user_institution", info.get("user_institution") or info.get("institution"))
    metadata.setdefault("institution", info.get("institution") or info.get("user_institution"))
    metadata.setdefault("institution_nature", info.get("institution_nature"))
    metadata.setdefault("inst_type", info.get("inst_type"))
    metadata.setdefault("user_type", info.get("user_type"))
    metadata.setdefault("user_role", info.get("user_role"))
    metadata.setdefault("product_type", info.get("product_type"))
    metadata.setdefault("request_time", info.get("request_time"))
    metadata.setdefault("response_time", info.get("response_time"))
    metadata.setdefault("entry_scene", info.get("entry_scene"))
    metadata.setdefault("scheduled", info.get("scheduled"))
    metadata.setdefault("is_web_search", info.get("is_web_search"))
    metadata.setdefault("question", info.get("question"))
    metadata.setdefault("question_id", info.get("question_id"))
    metadata.setdefault("session_id", info.get("session_id"))
    metadata.setdefault("task_index", info.get("task_index"))
    metadata.setdefault("success", info.get("success"))
    metadata.setdefault("status", info.get("status"))
    metadata.setdefault("end_type", info.get("end_type"))
    metadata.setdefault("task_status_desc", info.get("task_status_desc"))
    metadata.setdefault("env", info.get("env"))
    metadata.setdefault("target", info.get("target"))
    metadata.setdefault("history_db", info.get("history_db"))
    metadata.setdefault("source_kind", info.get("source_kind"))
    metadata.setdefault("system", info.get("system"))
    metadata.setdefault("error", info.get("error"))
    metadata.setdefault("session_question_count", info.get("session_question_count"))
    metadata.setdefault("answer", result_summary.get("final_answer_excerpt"))
    metadata.setdefault("final_answer_excerpt", result_summary.get("final_answer_excerpt"))
    metadata.setdefault(
        "duration_seconds",
        first_non_empty(info.get("duration_seconds"), info.get("total_elapsed"), info.get("average_time"), info.get("duration"), info.get("dur")),
    )
    if not metadata.get("token_usage"):
        metadata["token_usage"] = token_usage
    if not metadata.get("credit_usage"):
        metadata["credit_usage"] = credit_usage
    metadata.setdefault(
        "skills",
        [
            item.get("skill_name")
            for item in context.get("skills") or []
            if isinstance(item, dict) and item.get("skill_name")
        ][:20],
    )
    metadata.setdefault("has_attachments", 1 if context.get("input_files") else 0)
    metadata.setdefault("final_output_files", context.get("final_output_files") if isinstance(context.get("final_output_files"), list) else [])
    metadata.setdefault("final_file_previews", context.get("final_file_previews") if isinstance(context.get("final_file_previews"), list) else [])
    normalized["task_metadata"] = {key: value for key, value in metadata.items() if value not in (None, "", [], {})}
    if token_usage:
        normalized["token_total"] = first_non_empty(token_usage.get("total_tokens"), token_usage.get("total"), normalized.get("token_total"))
        normalized["prompt_tokens"] = first_non_empty(token_usage.get("prompt_tokens"), normalized.get("prompt_tokens"))
        normalized["completion_tokens"] = first_non_empty(token_usage.get("completion_tokens"), normalized.get("completion_tokens"))
        normalized["cached_tokens"] = first_non_empty(token_usage.get("cached_tokens"), normalized.get("cached_tokens"))
        normalized["llm_call_count"] = first_non_empty(token_usage.get("llm_call_count"), normalized.get("llm_call_count"))
    else:
        normalized.setdefault("token_total", "")
        normalized.setdefault("prompt_tokens", "")
        normalized.setdefault("completion_tokens", "")
        normalized.setdefault("cached_tokens", "")
        normalized.setdefault("llm_call_count", "")
    if credit_usage:
        normalized["credit_total"] = first_non_empty(credit_usage.get("total_credits"), credit_usage.get("total"), normalized.get("credit_total"))
        normalized["prompt_credits"] = first_non_empty(credit_usage.get("prompt_credits"), normalized.get("prompt_credits"))
        normalized["completion_credits"] = first_non_empty(credit_usage.get("completion_credits"), normalized.get("completion_credits"))
    else:
        normalized.setdefault("credit_total", "")
        normalized.setdefault("prompt_credits", "")
        normalized.setdefault("completion_credits", "")
    evidence = normalized.get("evidence")
    if isinstance(evidence, str):
        normalized["evidence"] = [evidence]
    elif not isinstance(evidence, list):
        normalized["evidence"] = []
    try:
        normalized["confidence"] = max(0.0, min(1.0, float(normalized.get("confidence", 0.0))))
    except (TypeError, ValueError):
        normalized["confidence"] = 0.0
    return normalized


def failure_zero_result(context: dict[str, Any]) -> dict[str, Any]:
    info = context.get("query_info") if isinstance(context.get("query_info"), dict) else {}
    status = status_text(context)
    issue2 = "任务超时" if "timeout" in status or "超时" in status else "任务失败"
    return normalize_quality_result(
        {
            "record_id": context.get("record_id"),
            "session_id": info.get("session_id"),
            "task_index": info.get("task_index"),
            "question_id": info.get("question_id"),
            "query_complexity": "complex",
            "query_major_category": "其他",
            "query_minor_category": "不明确或非金融问题",
            "result_score": 0,
            "overall_score": 0,
            "is_low_score": True,
            "issue_level1": "流程问题",
            "issue_level2": issue2,
            "low_score_reason": f"任务状态为 {status or 'unknown'}，未形成可用的 end-to-end 结果。",
            "improvement_suggestion": "先定位失败链路和异常日志；修复后用同 query 回放验证结果和过程。",
            "evidence": [f"status={status or 'unknown'}", f"ref={record_id_from_ref(info)}"],
            "confidence": 0.95,
        },
        context,
    )


def chat_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def call_openai_compatible(prompt: str, args: argparse.Namespace) -> tuple[str, dict[str, Any] | None]:
    ai_config = resolve_openai_compatible_config(args)
    api_key = ai_config["api_key"]
    if not api_key:
        raise RuntimeError("missing AI API key; set PAI_OBS_AI_API_KEY/GLOBAL_DATA_LLM_API_KEY or provide a local DeepTask config")
    base_url = ai_config["base_url"]
    models = [ai_config["model"], *ai_config.get("fallback_models", [])]
    last_error: RuntimeError | None = None
    for model_index, model in enumerate(models, 1):
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a strict JSON-only evaluation assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": getattr(args, "temperature", 0.0),
            "max_tokens": ai_config["max_tokens"],
        }
        if getattr(args, "json_mode", True):
            payload["response_format"] = {"type": "json_object"}
        request = Request(
            chat_endpoint(base_url),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=ai_config["timeout"]) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"AI request failed HTTP {exc.code} model={model}: {raw[:1200]}")
            continue
        except URLError as exc:
            last_error = RuntimeError(f"AI request failed model={model}: {exc.reason}")
            continue
        choices = data.get("choices") if isinstance(data, dict) else []
        message = (choices[0].get("message") if choices and isinstance(choices[0], dict) else {}) or {}
        usage = data.get("usage") if isinstance(data, dict) and isinstance(data.get("usage"), dict) else {}
        usage = dict(usage)
        usage.setdefault("model", data.get("model") if isinstance(data, dict) else model)
        usage.setdefault("requested_model", model)
        usage.setdefault("base_url", base_url)
        usage.setdefault("fallback_attempt_index", model_index)
        return str(message.get("content") or ""), usage
    raise last_error or RuntimeError("AI request failed for all configured models")


def call_gateway_quality(
    client: paiobs.PaiObsClient,
    context: dict[str, Any],
    prompt: str,
) -> tuple[str, dict[str, Any] | None]:
    info = context.get("query_info") if isinstance(context.get("query_info"), dict) else {}
    session_id = str(info.get("session_id") or "")
    task_index = safe_int(info.get("task_index"))
    if not session_id or task_index < 1:
        raise RuntimeError("gateway provider requires session_id and task_index")
    payload = client.request(
        "POST",
        "/analysis/query",
        body={
            "env": client.env,
            "session_id": session_id,
            "task_index": task_index,
            "focus": "quality_json",
            "custom_instruction": prompt,
            "output_format": "json",
        },
    )
    text = ""
    if isinstance(payload, dict):
        text = analysis_value_text(payload.get("analysis") or payload.get("result") or "")
    return text or paiobs.json_dumps(payload), payload.get("usage") if isinstance(payload, dict) else None


def analyze_context(
    client: paiobs.PaiObsClient,
    context: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    if getattr(args, "shortcut_failed", True) and is_failed_or_timeout(context):
        result = failure_zero_result(context)
        result["analysis_provider"] = "shortcut_failed"
        result["input_context_mode"] = context.get("context_mode") or ""
        result["judge_latency_seconds"] = 0.0
        result["judge_attempts"] = 0
        return result

    prompt = quality_prompt(context)
    provider = getattr(args, "ai_provider", "gateway")
    if provider == "none":
        return {
            "schema_version": "quality-prompt/v1",
            "record_id": context.get("record_id"),
            "query_info": context.get("query_info") or {},
            "prompt": prompt,
        }
    started = time.time()
    attempts = 0
    raw_text = ""
    usage: dict[str, Any] | None = None
    last_exc: Exception | None = None
    retry_count = ai_retries(args)
    for attempt in range(retry_count + 1):
        attempts = attempt + 1
        try:
            if provider == "gateway":
                raw_text, usage = call_gateway_quality(client, context, UNIFIED_INSTRUCTION.strip())
            elif provider == "openai-compatible":
                raw_text, usage = call_openai_compatible(prompt, args)
            else:
                raise RuntimeError(f"unknown ai provider: {provider}")
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt >= retry_count:
                break
            time.sleep(min(3.0, 0.8 * (attempt + 1)))
    if last_exc is not None:
        raise last_exc
    parsed = extract_quality_json(raw_text)
    result = normalize_quality_result(parsed, context)
    result["analysis_provider"] = provider
    result["input_context_mode"] = context.get("context_mode") or ""
    result["judge_latency_seconds"] = round(time.time() - started, 3)
    result["judge_attempts"] = attempts
    if usage:
        result["judge_usage"] = usage
    return result


def fetch_contexts_for_refs(
    client: paiobs.PaiObsClient,
    refs: list[dict[str, Any]],
    *,
    profile: str,
    max_items_per_request: int = 20,
) -> list[dict[str, Any]]:
    contexts = []
    chunk_size = max(1, int(max_items_per_request or 20))

    def error_context(ref: dict[str, Any], exc: Exception | str) -> dict[str, Any]:
        return {
            "schema_version": "paiwork-task-quality-context-error/v1",
            "session_id": ref.get("session_id"),
            "task_index": ref.get("task_index"),
            "question_id": ref.get("question_id"),
            "error": f"context fetch failed: {exc}",
        }

    def fetch_one_by_one(chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fetched = []
        for ref in chunk:
            try:
                fetched.append(fetch_context_for_ref(client, ref, profile=profile))
            except Exception as exc:
                fetched.append(error_context(ref, exc))
        return fetched

    for offset in range(0, len(refs), chunk_size):
        chunk = refs[offset : offset + chunk_size]
        try:
            payload = client.request(
                "POST",
                "/history/questions/batch",
                body={
                    "env": client.env,
                    "items": [
                        {"session_id": ref.get("session_id"), "task_index": ref.get("task_index"), "question_id": ref.get("question_id")}
                        for ref in chunk
                    ],
                    "profile": profile,
                    "max_items": len(chunk),
                    "max_concurrency": min(8, len(chunk)),
                },
            )
        except Exception:
            contexts.extend(fetch_one_by_one(chunk))
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            contexts.extend(fetch_one_by_one(chunk))
            continue
        items_by_index = {
            safe_int(item.get("index")): item
            for item in payload.get("items") or []
            if isinstance(item, dict)
        }
        errors_by_index = {
            safe_int(item.get("index")): item
            for item in payload.get("errors") or []
            if isinstance(item, dict)
        }
        for local_index, ref in enumerate(chunk, 1):
            item = items_by_index.get(local_index)
            if isinstance(item, dict) and item.get("ok") and isinstance(item.get("data"), dict):
                contexts.append(item["data"])
                continue
            error = errors_by_index.get(local_index) or item or {}
            contexts.append(error_context(ref, error.get("error") if isinstance(error, dict) else "missing batch result"))
    return contexts


def fetch_context_for_ref(client: paiobs.PaiObsClient, ref: dict[str, Any], *, profile: str) -> dict[str, Any]:
    normalized = normalize_ref_item(ref)
    session_id = str(normalized.get("session_id") or "").strip()
    task_index = safe_int(normalized.get("task_index"))
    if not session_id or task_index < 1:
        raise RuntimeError("context fetch requires session_id and task_index")
    return client.request(
        "GET",
        f"/history/sessions/{quote(session_id, safe='')}/tasks/{task_index}",
        query={"env": client.env, "profile": profile},
    )


def refs_from_args(client: paiobs.PaiObsClient, args: argparse.Namespace) -> list[dict[str, Any]]:
    refs = paiobs.parse_items(args)
    if refs:
        items = [normalize_ref_item(item) for item in refs if isinstance(item, dict)]
        return filter_ended_refs(items, args)
    filters = paiobs.build_filters(args)
    if not filters:
        return []
    limit = int(getattr(args, "limit", DEFAULT_ANALYZE_LIMIT) or DEFAULT_ANALYZE_LIMIT)
    profile = getattr(args, "search_profile", "lite")
    use_complete = bool(getattr(args, "complete_search", True))
    if use_complete and filters.get("start_time") and filters.get("end_time"):
        items = collect_search_items_adaptive(
            client,
            filters=filters,
            profile=profile,
            limit=limit,
            page_limit=int(getattr(args, "search_page_limit", DEFAULT_SEARCH_PAGE_LIMIT) or DEFAULT_SEARCH_PAGE_LIMIT),
            initial_window_minutes=float(getattr(args, "search_slice_minutes", DEFAULT_SEARCH_SLICE_MINUTES) or DEFAULT_SEARCH_SLICE_MINUTES),
            min_slice_seconds=float(getattr(args, "min_slice_seconds", 1.0) or 1.0),
            max_workers=int(getattr(args, "search_workers", DEFAULT_SEARCH_WORKERS) or DEFAULT_SEARCH_WORKERS),
            args=args,
        )
    else:
        progress_log(args, f"collect refs: search start limit={limit}")
        items = collect_search_items(
            client,
            filters=filters,
            profile=profile,
            limit=limit,
        )
        progress_log(args, f"collect refs: search done unique={len(items)}")
    normalized = [normalize_ref_item(item) for item in items if isinstance(item, dict)]
    return filter_ended_refs(normalized, args)


def cmd_prompt(_: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    if args.context_json:
        context = normalize_context(
            paiobs.read_json_file(args.context_json),
            include_process=args.include_process,
            include_raw_context=args.include_raw_context,
            max_context_chars=args.max_context_chars,
        )
        sys.stdout.write(quality_prompt(context) + "\n")
    else:
        sys.stdout.write(UNIFIED_INSTRUCTION.strip() + "\n")


def cmd_prepare(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    refs = refs_from_args(client, args)
    if not refs:
        if getattr(args, "ended_only", False):
            progress_log(args, "prepare skipped: no ended refs")
            write_jsonl([], args.output)
            return
        raise SystemExit("provide refs/items-json or search filters")
    raw_contexts = fetch_contexts_for_refs(client, refs, profile=args.context_profile)
    contexts = []
    for ref, payload in zip(refs, raw_contexts):
        if not isinstance(payload, dict):
            continue
        contexts.append(
            merge_ref_info_into_context(
                normalize_context(
                    payload,
                    client=client,
                    include_file_previews=args.include_file_previews,
                    include_process=args.include_process,
                    include_raw_context=args.include_raw_context,
                    max_file_previews=args.max_file_previews,
                    file_preview_chars=args.file_preview_chars,
                    max_context_chars=args.max_context_chars,
                ),
                ref,
            )
        )
    contexts = filter_ended_contexts(contexts, args)
    write_jsonl(contexts, args.output)


def context_inputs(client: paiobs.PaiObsClient, args: argparse.Namespace) -> list[dict[str, Any]]:
    if getattr(args, "context_jsonl", None):
        contexts = [
            compact_quality_context(
                item,
                include_process=args.include_process,
                include_raw_context=args.include_raw_context,
                max_context_chars=args.max_context_chars,
            )
            if isinstance(item, dict) and item.get("schema_version") == "paiwork-task-quality-context/v1"
            else normalize_context(
                item,
                include_process=args.include_process,
                include_raw_context=args.include_raw_context,
                max_context_chars=args.max_context_chars,
            )
            for item in read_jsonl(args.context_jsonl)
            if isinstance(item, dict)
        ]
        return filter_ended_contexts(contexts, args)
    if getattr(args, "context_json", None):
        contexts = [
            normalize_context(
                paiobs.read_json_file(args.context_json),
                client=client,
                include_file_previews=args.include_file_previews,
                include_process=args.include_process,
                include_raw_context=args.include_raw_context,
                max_file_previews=args.max_file_previews,
                file_preview_chars=args.file_preview_chars,
                max_context_chars=args.max_context_chars,
            )
        ]
        return filter_ended_contexts(contexts, args)
    refs = refs_from_args(client, args)
    if not refs:
        raise SystemExit("provide context-jsonl/context-json, refs/items-json, or search filters")
    raw_contexts = fetch_contexts_for_refs(client, refs, profile=args.context_profile)
    contexts = []
    for ref, payload in zip(refs, raw_contexts):
        if not isinstance(payload, dict):
            continue
        contexts.append(
            merge_ref_info_into_context(
                normalize_context(
                    payload,
                    client=client,
                    include_file_previews=args.include_file_previews,
                    include_process=args.include_process,
                    include_raw_context=args.include_raw_context,
                    max_file_previews=args.max_file_previews,
                    file_preview_chars=args.file_preview_chars,
                    max_context_chars=args.max_context_chars,
                ),
                ref,
            )
        )
    return filter_ended_contexts(contexts, args)


def normalize_fetched_context(client: paiobs.PaiObsClient, payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    return normalize_context(
        payload,
        client=client,
        include_file_previews=args.include_file_previews,
        include_process=args.include_process,
        include_raw_context=args.include_raw_context,
        max_file_previews=args.max_file_previews,
        file_preview_chars=args.file_preview_chars,
        max_context_chars=args.max_context_chars,
    )


def merge_ref_info_into_context(context: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    ref_info = normalize_ref_item(ref)
    if not ref_info:
        return context
    merged = dict(context)
    info = dict(merged.get("query_info") if isinstance(merged.get("query_info"), dict) else {})
    for key, value in ref_info.items():
        if value not in (None, "", [], {}) and info.get(key) in (None, "", [], {}):
            info[key] = value
    merged["query_info"] = compact_query_info(info)
    if not merged.get("record_id"):
        merged["record_id"] = record_id_from_ref(info)
    return merged


def normalized_ai_workers(args: argparse.Namespace, total: int) -> int:
    if total <= 0:
        return 0
    try:
        requested = int(getattr(args, "workers", DEFAULT_AI_WORKERS) or DEFAULT_AI_WORKERS)
    except (TypeError, ValueError):
        requested = DEFAULT_AI_WORKERS
    return min(total, max(1, requested))


def progress_ref_for_context(context: dict[str, Any]) -> dict[str, Any]:
    info = context.get("query_info") if isinstance(context.get("query_info"), dict) else {}
    if info:
        merged = dict(info)
        if context.get("record_id") and not merged.get("record_id"):
            merged["record_id"] = context.get("record_id")
        return merged
    return context


def record_id_for_error(item: Any, ref: dict[str, Any]) -> str:
    normalized = normalize_ref_item(ref) if isinstance(ref, dict) else {}
    if isinstance(ref, dict) and ref.get("record_id"):
        normalized["record_id"] = ref.get("record_id")
    record_id = record_id_from_ref(normalized) if normalized else ""
    if not record_id and isinstance(item, dict):
        record_id = str(item.get("record_id") or "")
    return record_id


def run_analysis_items_concurrent(
    items: list[Any],
    args: argparse.Namespace,
    *,
    phase: str,
    analyze_one: Any,
    progress_ref: Any,
    start_detail: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    total = len(items)
    workers = normalized_ai_workers(args, total)
    results_by_index: dict[int, dict[str, Any]] = {}
    errors_by_index: dict[int, dict[str, Any]] = {}
    skipped = 0
    batch_started = time.time()
    mode = "parallel" if workers > 1 else "sequential"
    detail = f" {start_detail}" if start_detail else ""
    progress_log(args, f"{phase} start total={total} mode={mode} workers={workers}{detail}")
    if total <= 0:
        progress_log(args, f"{phase} finished total=0 ok=0 errors=0 skipped=0 elapsed=0.0s")
        return [], []

    next_index = 1
    futures: dict[Any, tuple[int, Any, dict[str, Any], float]] = {}

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        nonlocal next_index
        if next_index > total:
            return False
        item = items[next_index - 1]
        ref = progress_ref(item)
        item_started = time.time()
        progress_item_start(args, phase, next_index, total, ref)
        futures[executor.submit(analyze_one, next_index, item)] = (next_index, item, ref, item_started)
        next_index += 1
        return True

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for _ in range(workers):
            submit_next(executor)
        while futures:
            done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in done:
                index, item, ref, item_started = futures.pop(future)
                try:
                    outcome = future.result()
                    if isinstance(outcome, dict) and outcome.get("status") == "skipped":
                        skipped += 1
                        progress_item_skipped(
                            args,
                            phase,
                            index,
                            total,
                            ok_count=len(results_by_index),
                            error_count=len(errors_by_index),
                            skip_count=skipped,
                            item_started=item_started,
                            batch_started=batch_started,
                            reason=str(outcome.get("reason") or "skipped"),
                        )
                    else:
                        result = outcome.get("result") if isinstance(outcome, dict) else outcome
                        if not isinstance(result, dict):
                            raise RuntimeError("analysis worker returned non-dict result")
                        results_by_index[index] = result
                        progress_item_done(
                            args,
                            phase,
                            index,
                            total,
                            ok_count=len(results_by_index),
                            error_count=len(errors_by_index),
                            skip_count=skipped,
                            item_started=item_started,
                            batch_started=batch_started,
                            result=result,
                        )
                except Exception as exc:
                    errors_by_index[index] = {"record_id": record_id_for_error(item, ref), "error": str(exc)}
                    progress_item_done(
                        args,
                        phase,
                        index,
                        total,
                        ok_count=len(results_by_index),
                        error_count=len(errors_by_index),
                        skip_count=skipped,
                        item_started=item_started,
                        batch_started=batch_started,
                        error=exc,
                    )
                submit_next(executor)

    progress_log(
        args,
        (
            f"{phase} finished total={total} ok={len(results_by_index)} "
            f"errors={len(errors_by_index)} skipped={skipped} elapsed={time.time() - batch_started:.1f}s"
        ),
    )
    results = [results_by_index[index] for index in sorted(results_by_index)]
    errors = [errors_by_index[index] for index in sorted(errors_by_index)]
    return results, errors


def analyze_refs_concurrent(
    client: paiobs.PaiObsClient,
    refs: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    phase: str = "item analyze",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def analyze_one(_: int, ref: dict[str, Any]) -> dict[str, Any]:
        payload = fetch_context_for_ref(client, ref, profile=args.context_profile)
        context = merge_ref_info_into_context(normalize_fetched_context(client, payload, args), ref)
        if getattr(args, "ended_only", False) and not is_ended_context(context):
            return {"status": "skipped", "reason": "not-ended"}
        return {"status": "ok", "result": analyze_context(client, context, args)}

    return run_analysis_items_concurrent(
        refs,
        args,
        phase=phase,
        analyze_one=analyze_one,
        progress_ref=lambda ref: ref,
        start_detail=f"context_profile={getattr(args, 'context_profile', DEFAULT_CONTEXT_PROFILE)}",
    )


def analyze_contexts_concurrent(
    client: paiobs.PaiObsClient,
    contexts: list[dict[str, Any]],
    args: argparse.Namespace,
    *,
    phase: str = "context analyze",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def analyze_one(_: int, context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "result": analyze_context(client, context, args)}

    return run_analysis_items_concurrent(
        contexts,
        args,
        phase=phase,
        analyze_one=analyze_one,
        progress_ref=progress_ref_for_context,
    )


def cmd_analyze_batch(client: paiobs.PaiObsClient, args: argparse.Namespace) -> None:
    if getattr(args, "context_jsonl", "") or getattr(args, "context_json", ""):
        contexts = context_inputs(client, args)
        if args.max_items and len(contexts) > args.max_items:
            contexts = contexts[: args.max_items]
        results, errors = analyze_contexts_concurrent(client, contexts, args, phase="context analyze")
    else:
        refs = refs_from_args(client, args)
        if args.max_items and len(refs) > args.max_items:
            refs = refs[: args.max_items]
        if not refs:
            if getattr(args, "ended_only", False):
                progress_log(args, "item analyze skipped: no ended refs")
                write_jsonl([], args.output)
                return
            raise SystemExit("provide context-jsonl/context-json, refs/items-json, or search filters")
        results, errors = analyze_refs_concurrent(client, refs, args, phase="item analyze")

    if errors:
        error_path = args.error_output or (str(args.output) + ".errors.jsonl" if args.output else "")
        if error_path:
            write_jsonl(errors, error_path)
        else:
            for error in errors:
                sys.stderr.write(paiobs.json_dumps(error, compact=True) + "\n")
    write_jsonl(results, args.output)


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--gateway-profile", default=None, help="Gateway profile: release/prod -> 30100, local/test/debug -> localhost:6193")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--env", default=None)
    parser.add_argument("--timeout", default=None)
    parser.add_argument("--file-auth-token", default=None)


def add_context_build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--context-profile", default=DEFAULT_CONTEXT_PROFILE, choices=["summary", "context", "qa", "full"])
    parser.add_argument("--max-context-chars", type=int, default=DEFAULT_MAX_CONTEXT_CHARS)
    parser.add_argument("--include-process", action="store_true", help="Include compact process summary/steps; off by default for bulk scoring.")
    parser.add_argument("--include-raw-context", action="store_true", help="Include a clipped raw context excerpt; off by default for faster judging.")
    parser.add_argument("--include-file-previews", action="store_true", help="Preview likely final output files through the gateway file proxy")
    parser.add_argument("--max-file-previews", type=int, default=3)
    parser.add_argument("--file-preview-chars", type=int, default=2000)


def add_ai_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ai-provider",
        choices=["gateway", "openai-compatible", "none"],
        default=default_ai_provider(),
        help="openai-compatible is the default AI judging path; gateway uses Observability /analysis/query once per item; none emits prompts only.",
    )
    parser.add_argument("--ai-base-url", default="")
    parser.add_argument("--ai-api-key", default="")
    parser.add_argument("--ai-model", default="", help=f"Model for openai-compatible provider; default resolves to {DEFAULT_MODEL}.")
    parser.add_argument("--ai-fallback-models", default="", help="Comma-separated fallback models for openai-compatible provider")
    parser.add_argument("--ai-timeout", type=float, default=None)
    parser.add_argument("--ai-retries", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--json-mode", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--shortcut-failed", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_AI_WORKERS,
        help=f"Concurrent AI judging workers for analyze-batch; default {DEFAULT_AI_WORKERS} (3-5 is recommended).",
    )
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--error-output", default="")
    parser.add_argument("--gateway-batch-size", type=int, default=DEFAULT_GATEWAY_BATCH_SIZE, help="Deprecated and ignored; gateway judging uses per-item /analysis/query calls.")
    parser.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--progress-interval", type=float, default=DEFAULT_PROGRESS_INTERVAL)


def add_ref_or_search_args(parser: argparse.ArgumentParser) -> None:
    paiobs.add_items_args(parser)
    paiobs.add_filter_args(parser)
    parser.add_argument("--search-profile", default="lite", choices=["lite", "summary", "raw"])
    parser.add_argument("--limit", type=int, default=DEFAULT_ANALYZE_LIMIT)
    parser.add_argument("--complete-search", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--ended-only", action="store_true", help="Analyze only tasks that have reached a terminal status; running/pending tasks are skipped.")
    parser.add_argument("--search-slice-minutes", type=float, default=DEFAULT_SEARCH_SLICE_MINUTES)
    parser.add_argument("--search-page-limit", type=int, default=DEFAULT_SEARCH_PAGE_LIMIT)
    parser.add_argument("--search-workers", type=int, default=DEFAULT_SEARCH_WORKERS)
    parser.add_argument("--min-slice-seconds", type=float, default=1.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PaiWork task AI classification and quality judging")
    add_runtime_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    prompt = sub.add_parser("prompt", help="Print the unified quality judging prompt")
    prompt.add_argument("--context-json", default="")
    prompt.add_argument("--max-context-chars", type=int, default=12000)
    prompt.add_argument("--include-process", action="store_true")
    prompt.add_argument("--include-raw-context", action="store_true")
    prompt.set_defaults(func=cmd_prompt)

    prepare = sub.add_parser("prepare-contexts", help="Fetch and normalize task contexts into JSONL")
    add_ref_or_search_args(prepare)
    add_context_build_args(prepare)
    prepare.add_argument("-o", "--output", required=True)
    prepare.set_defaults(func=cmd_prepare)

    analyze = sub.add_parser("analyze", help="Judge one task/context")
    analyze.add_argument("session_id", nargs="?")
    analyze.add_argument("task_index", nargs="?", type=int)
    analyze.add_argument("--context-json", default="")
    add_context_build_args(analyze)
    add_ai_args(analyze)
    analyze.add_argument("-o", "--output")
    analyze.set_defaults(
        func=lambda client, args: cmd_analyze_batch(
            client,
            argparse.Namespace(
                **{
                    **vars(args),
                    "refs": [f"{args.session_id}:{args.task_index}"] if args.session_id and args.task_index else [],
                    "items_json": "",
                    "context_jsonl": "",
                    "limit": 1,
                    "search_profile": "lite",
                    "filters_json": "",
                    "session_id": None,
                    "question_id": None,
                    "keyword": None,
                    "user_id": None,
                    "username": None,
                    "institution": None,
                    "entry_scene": None,
                    "status": None,
                    "scheduled": None,
                    "end_type": None,
                    "is_web_search": None,
                    "start_time": None,
                    "end_time": None,
                    "institution_nature": None,
                    "inst_type": None,
                    "product_type": None,
                    "user_type": None,
                    "user_role": None,
                }
            ),
        )
    )

    batch = sub.add_parser("analyze-batch", help="Judge a batch from refs, search filters, or prepared context JSONL")
    batch.add_argument("--context-jsonl", default="")
    batch.add_argument("--context-json", default="")
    add_ref_or_search_args(batch)
    add_context_build_args(batch)
    add_ai_args(batch)
    batch.add_argument("-o", "--output")
    batch.set_defaults(func=cmd_analyze_batch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = paiobs.build_client(args)
    started = time.time()
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
    finally:
        if os.environ.get("PAI_OBS_AI_TIMING"):
            sys.stderr.write(f"elapsed_seconds={time.time() - started:.3f}\n")


if __name__ == "__main__":
    raise SystemExit(main())
