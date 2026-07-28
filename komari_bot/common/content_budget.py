"""用户或管理入口可写文本的统一内容预算。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TextBudget:
    """一段文本允许占用的三维预算。"""

    max_characters: int
    max_utf8_bytes: int
    max_estimated_tokens: int


TITLE_TEXT_BUDGET = TextBudget(
    max_characters=128,
    max_utf8_bytes=512,
    max_estimated_tokens=128,
)
CONTENT_TEXT_BUDGET = TextBudget(
    max_characters=12_000,
    max_utf8_bytes=36_000,
    max_estimated_tokens=6_000,
)
PROPOSAL_CONTENT_TEXT_BUDGET = TextBudget(
    max_characters=8_000,
    max_utf8_bytes=24_000,
    max_estimated_tokens=4_096,
)
NOTES_TEXT_BUDGET = TextBudget(
    max_characters=2_000,
    max_utf8_bytes=6_000,
    max_estimated_tokens=2_000,
)
QUERY_TEXT_BUDGET = TextBudget(
    max_characters=512,
    max_utf8_bytes=2_048,
    max_estimated_tokens=512,
)
IDENTIFIER_TEXT_BUDGET = TextBudget(
    max_characters=128,
    max_utf8_bytes=512,
    max_estimated_tokens=128,
)
KEYWORD_TEXT_BUDGET = TextBudget(
    max_characters=128,
    max_utf8_bytes=512,
    max_estimated_tokens=128,
)
KEYWORDS_TOTAL_TEXT_BUDGET = TextBudget(
    max_characters=1_024,
    max_utf8_bytes=4_096,
    max_estimated_tokens=1_024,
)
MAX_KEYWORD_COUNT = 20
IDENTIFIERS_TOTAL_TEXT_BUDGET = TextBudget(
    max_characters=4_096,
    max_utf8_bytes=16_384,
    max_estimated_tokens=4_096,
)
MAX_IDENTIFIER_COUNT = 100


@dataclass(frozen=True, slots=True)
class JsonBudget:
    """管理入口 JSON 文档允许占用的结构预算。"""

    max_depth: int
    max_nodes: int
    max_container_items: int
    text_budget: TextBudget


MANAGEMENT_JSON_BUDGET = JsonBudget(
    max_depth=8,
    max_nodes=2_000,
    max_container_items=100,
    text_budget=CONTENT_TEXT_BUDGET,
)


class ContentValidationError(ValueError):
    """内容为空、编码非法或超过统一预算。"""


def estimate_text_tokens(value: str) -> int:
    """保守估算中英文混合文本 token 数，不替代模型 tokenizer。"""
    utf8_length = len(_encode_utf8(value, label="文本"))
    return _estimate_tokens(len(value), utf8_length)


def validate_text_budget(
    value: str,
    *,
    label: str,
    budget: TextBudget,
) -> str:
    """校验字符、UTF-8 字节与估算 token 三维预算。"""
    character_count = len(value)
    if character_count > budget.max_characters:
        message = (
            f"{label}超过字符上限（当前 {character_count}，"
            f"最多 {budget.max_characters}）"
        )
        raise ContentValidationError(message)

    utf8_length = len(_encode_utf8(value, label=label))
    if utf8_length > budget.max_utf8_bytes:
        message = (
            f"{label}超过 UTF-8 字节上限（当前 {utf8_length}，"
            f"最多 {budget.max_utf8_bytes}）"
        )
        raise ContentValidationError(message)

    estimated_tokens = _estimate_tokens(character_count, utf8_length)
    if estimated_tokens > budget.max_estimated_tokens:
        message = (
            f"{label}超过估算 token 上限（当前约 {estimated_tokens}，"
            f"最多 {budget.max_estimated_tokens}）"
        )
        raise ContentValidationError(message)
    return value


def truncate_text_to_budget(
    value: str,
    *,
    label: str,
    budget: TextBudget,
) -> tuple[str, bool]:
    """显式截断文本到三维预算内，并返回是否发生截断。"""
    normalized = value.strip()
    _encode_utf8(normalized, label=label)
    try:
        return validate_text_budget(normalized, label=label, budget=budget), False
    except ContentValidationError:
        pass

    marker = "…"
    validate_text_budget(marker, label=label, budget=budget)
    lower = 0
    upper = len(normalized)
    best = marker
    while lower <= upper:
        middle = (lower + upper) // 2
        candidate = f"{normalized[:middle].rstrip()}{marker}"
        try:
            validate_text_budget(candidate, label=label, budget=budget)
        except ContentValidationError:
            upper = middle - 1
        else:
            best = candidate
            lower = middle + 1
    return best, True


def normalize_required_text(
    value: str,
    *,
    label: str,
    budget: TextBudget,
) -> str:
    """清理必填文本并执行统一预算。"""
    normalized = value.strip()
    if not normalized:
        message = f"{label}不能为空"
        raise ContentValidationError(message)
    return validate_text_budget(normalized, label=label, budget=budget)


def normalize_optional_text(
    value: str | None,
    *,
    label: str,
    budget: TextBudget,
) -> str | None:
    """把空白可选文本归一为 None，并校验非空值。"""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return validate_text_budget(normalized, label=label, budget=budget)


def normalize_keywords(
    value: list[str],
    *,
    require_nonempty: bool,
) -> list[str]:
    """清理、去重并校验关键词个数、单项与总量预算。"""
    if len(value) > MAX_KEYWORD_COUNT:
        message = f"关键词数量超过上限（当前 {len(value)}，最多 {MAX_KEYWORD_COUNT}）"
        raise ContentValidationError(message)

    normalized: list[str] = []
    seen: set[str] = set()
    for keyword in value:
        cleaned = keyword.strip()
        if not cleaned:
            continue
        lowered = cleaned.casefold()
        if lowered in seen:
            continue
        validate_text_budget(
            cleaned,
            label="单个关键词",
            budget=KEYWORD_TEXT_BUDGET,
        )
        seen.add(lowered)
        normalized.append(cleaned)

    if require_nonempty and not normalized:
        raise ContentValidationError("关键词不能为空")

    validate_text_budget(
        "\n".join(normalized),
        label="关键词总量",
        budget=KEYWORDS_TOTAL_TEXT_BUDGET,
    )
    return normalized


def normalize_identifiers(
    value: list[str],
    *,
    label: str,
    require_nonempty: bool,
) -> list[str]:
    """清理、去重并校验一组外部标识符。"""
    if len(value) > MAX_IDENTIFIER_COUNT:
        message = (
            f"{label}数量超过上限（当前 {len(value)}，最多 {MAX_IDENTIFIER_COUNT}）"
        )
        raise ContentValidationError(message)

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_identifier in value:
        identifier = normalize_required_text(
            raw_identifier,
            label=f"单个{label}",
            budget=IDENTIFIER_TEXT_BUDGET,
        )
        if identifier in seen:
            continue
        seen.add(identifier)
        normalized.append(identifier)

    if require_nonempty and not normalized:
        raise ContentValidationError(f"{label}不能为空")

    validate_text_budget(
        "\n".join(normalized),
        label=f"{label}总量",
        budget=IDENTIFIERS_TOTAL_TEXT_BUDGET,
    )
    return normalized


def validate_json_budget(
    value: Any,
    *,
    label: str,
    budget: JsonBudget = MANAGEMENT_JSON_BUDGET,
) -> Any:
    """校验 JSON 兼容值的深度、节点、容器与序列化文本预算。"""
    node_count = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    seen_containers: set[int] = set()

    while stack:
        current, depth = stack.pop()
        node_count += 1
        if node_count > budget.max_nodes:
            message = f"{label}节点数量超过上限（最多 {budget.max_nodes}）"
            raise ContentValidationError(message)
        if depth > budget.max_depth:
            message = f"{label}嵌套深度超过上限（最多 {budget.max_depth} 层）"
            raise ContentValidationError(message)

        if isinstance(current, dict):
            container_id = id(current)
            if container_id in seen_containers:
                raise ContentValidationError(f"{label}不能包含循环引用")
            seen_containers.add(container_id)
            if len(current) > budget.max_container_items:
                message = (
                    f"{label}单个对象字段数量超过上限"
                    f"（最多 {budget.max_container_items}）"
                )
                raise ContentValidationError(message)
            for key, item in current.items():
                if not isinstance(key, str):
                    raise ContentValidationError(f"{label}对象键必须是字符串")
                validate_text_budget(
                    key,
                    label=f"{label}对象键",
                    budget=KEYWORD_TEXT_BUDGET,
                )
                stack.append((item, depth + 1))
            continue

        if isinstance(current, list):
            container_id = id(current)
            if container_id in seen_containers:
                raise ContentValidationError(f"{label}不能包含循环引用")
            seen_containers.add(container_id)
            if len(current) > budget.max_container_items:
                message = (
                    f"{label}单个数组元素数量超过上限"
                    f"（最多 {budget.max_container_items}）"
                )
                raise ContentValidationError(message)
            stack.extend((item, depth + 1) for item in current)
            continue

        if isinstance(current, float) and not math.isfinite(current):
            raise ContentValidationError(f"{label}不能包含非有限数值")
        if not isinstance(current, (str, int, float, bool, type(None))):
            message = f"{label}包含不支持的 JSON 类型"
            raise ContentValidationError(message)

    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (RecursionError, TypeError, ValueError) as exc:
        message = f"{label}不是有效 JSON 文档"
        raise ContentValidationError(message) from exc
    validate_text_budget(serialized, label=label, budget=budget.text_budget)
    return value


def _encode_utf8(value: str, *, label: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        message = f"{label}包含无效 Unicode 字符"
        raise ContentValidationError(message) from exc


def _estimate_tokens(character_count: int, utf8_length: int) -> int:
    return max((character_count + 3) // 4, (utf8_length + 2) // 3)
