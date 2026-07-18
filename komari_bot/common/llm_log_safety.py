"""LLM 调用日志的脱敏与历史迁移工具。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

LOG_SCHEMA_VERSION = 2
_SAFE_LABEL_MAX_CHARS = 128
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_INPUT_SCALAR_KEYS = (
    "temperature",
    "max_tokens",
    "enable_knowledge",
    "knowledge_limit",
    "parallel_tool_calls",
    "tools_count",
)
_SAFE_INPUT_SUMMARY_KEYS = (
    "payload_fingerprint",
    "prompt_summary",
    "system_instruction_summary",
    "knowledge_query_summary",
    "payload_summary",
    "response_format_summary",
    "tools_summary",
    "tool_choice_summary",
)
_SAFE_INPUT_LIST_KEYS = ("parameter_keys", "response_format_keys")


def _safe_label(value: object) -> str:
    raw_value = str(value).strip()[:_SAFE_LABEL_MAX_CHARS]
    return "".join(
        char
        for char in raw_value
        if char.isascii() and (char.isalnum() or char in "-_.:/")
    )


def _safe_timestamp(value: object) -> str:
    raw_value = str(value).strip()
    if not raw_value:
        return ""
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return ""
    return parsed.isoformat()


def _serialize_for_fingerprint(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=repr,
        )
    except (TypeError, ValueError, RecursionError):
        return repr(value)


def build_content_summary(value: object) -> dict[str, int | str]:
    """生成不可逆的内容体量与 SHA-256 摘要。"""
    serialized = _serialize_for_fingerprint(value)
    return {
        "chars": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _sanitize_summary_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return build_content_summary(value)

    sanitized: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if key == "sha256" and isinstance(raw_value, str):
            digest = raw_value.lower()
            if _SHA256_PATTERN.fullmatch(digest):
                sanitized[key] = digest
            continue
        if key == "type" and isinstance(raw_value, str):
            sanitized[key] = _safe_label(raw_value)
            continue
        if isinstance(raw_value, bool):
            sanitized[key] = raw_value
            continue
        if isinstance(raw_value, (int, float)) and (
            key in {"chars", "serialized_chars", "turns", "count"}
            or key.endswith(("_chars", "_bytes", "_count", "_parts"))
        ):
            sanitized[key] = raw_value
    return sanitized


def _sanitize_input_metadata(input_data: object) -> dict[str, Any]:
    sanitized: dict[str, Any] = {
        "payload_fingerprint": build_content_summary(input_data),
    }
    if not isinstance(input_data, dict):
        return sanitized

    for key in _SAFE_INPUT_SCALAR_KEYS:
        if key not in input_data:
            continue
        value = input_data[key]
        if value is None or isinstance(value, (bool, int, float)):
            sanitized[key] = value

    for key in _SAFE_INPUT_SUMMARY_KEYS:
        if key in input_data:
            sanitized[key] = _sanitize_summary_dict(input_data[key])

    for key in _SAFE_INPUT_LIST_KEYS:
        value = input_data.get(key)
        if isinstance(value, list):
            sanitized[key] = [
                label for item in value if (label := _safe_label(item))
            ][:64]
    return sanitized


def _extract_trace_metadata(input_data: object) -> tuple[str, str]:
    if not isinstance(input_data, dict):
        return "", ""
    return (
        _safe_label(input_data.get("trace_id", "")),
        _safe_label(input_data.get("phase", "")),
    )


def _sanitize_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    allowed_keys = (
        "input_tokens",
        "cached_input_tokens",
        "cache_miss_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    return {
        key: token_count
        for key in allowed_keys
        if isinstance((token_count := value.get(key)), int) and token_count >= 0
    }


def sanitize_persisted_log_record(record: dict[str, Any]) -> dict[str, Any]:
    """将新旧日志记录统一转换为不含正文的 v2 安全格式。"""
    input_source = record.get("input_summary", record.get("input"))
    trace_id, phase = _extract_trace_metadata(input_source)
    trace_id = _safe_label(record.get("trace_id", trace_id))
    phase = _safe_label(record.get("phase", phase))

    output_source = record.get("output_summary", record.get("output"))
    error_source = record.get("error_summary", record.get("error"))
    reasoning_source = record.get("reasoning_content")
    reasoning_chars = record.get("reasoning_chars")
    if not isinstance(reasoning_chars, int) or reasoning_chars < 0:
        reasoning_chars = len(str(reasoning_source)) if reasoning_source else 0

    safe_record: dict[str, Any] = {
        "schema_version": LOG_SCHEMA_VERSION,
        "timestamp": _safe_timestamp(record.get("timestamp", "")),
        "method": _safe_label(record.get("method", "")),
        "model": _safe_label(record.get("model", "")),
        "trace_id": trace_id,
        "phase": phase,
        "status": "error" if error_source not in (None, "", {}) else "success",
        "input_summary": _sanitize_input_metadata(input_source),
        "reasoning_chars": reasoning_chars,
    }

    if output_source not in (None, "", {}):
        safe_record["output_summary"] = _sanitize_summary_dict(output_source)
    if error_source not in (None, "", {}):
        error_summary = _sanitize_summary_dict(error_source)
        if "type" not in error_summary:
            error_summary["type"] = _safe_label(
                record.get("error_type", "LegacyError")
            )
        safe_record["error_summary"] = error_summary

    finish_reason = _safe_label(record.get("finish_reason", ""))
    if finish_reason:
        safe_record["finish_reason"] = finish_reason

    tool_calls_count = record.get("tool_calls_count")
    if isinstance(tool_calls_count, int) and tool_calls_count >= 0:
        safe_record["tool_calls_count"] = tool_calls_count

    duration_ms = record.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
        safe_record["duration_ms"] = round(float(duration_ms), 2)

    usage = _sanitize_usage(record.get("usage"))
    if usage:
        safe_record["usage"] = usage
    return safe_record


def sanitize_log_text(original_text: str) -> str:
    """将一段 JSONL 文本转换为安全格式，不执行文件写入。"""
    sanitized_lines: list[str] = []
    for line in original_text.splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = {
                "method": "legacy_unparseable",
                "input": line,
                "error_type": "InvalidJsonLine",
                "error": line,
            }
        if not isinstance(parsed, dict):
            parsed = {"method": "legacy_invalid", "input": parsed}
        sanitized_lines.append(
            json.dumps(
                sanitize_persisted_log_record(parsed),
                ensure_ascii=False,
            )
        )

    sanitized_text = "\n".join(sanitized_lines)
    if sanitized_lines:
        sanitized_text += "\n"
    return sanitized_text


def scrub_log_file(log_file: Path) -> bool:
    """原子净化一个历史 JSONL 文件，返回文件是否发生变化。"""
    original_text = log_file.read_text(encoding="utf-8")
    sanitized_text = sanitize_log_text(original_text)
    if sanitized_text == original_text:
        return False

    temporary_file = log_file.with_name(
        f".{log_file.name}.{uuid.uuid4().hex}.tmp"
    )
    temporary_file.write_text(sanitized_text, encoding="utf-8")
    temporary_file.chmod(0o600)
    temporary_file.replace(log_file)
    return True


def scrub_log_directory(log_dir: Path) -> int:
    """净化目录内全部 LLM JSONL 日志。"""
    if not log_dir.exists():
        return 0
    return sum(scrub_log_file(log_file) for log_file in log_dir.glob("*.jsonl"))
