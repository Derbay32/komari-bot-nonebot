"""用户画像压缩的共享逻辑。"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Protocol

from .token_counter import estimate_text_tokens

_ALLOWED_CATEGORIES = {"preference", "fact", "relation", "general"}
_COMPACTION_OPERATIONS_JSON_EXAMPLE = (
    '{"operations": [{"op": "replace", "field": "trait", "key": "长期兴趣", '
    '"value": "偏爱策略和养成类游戏", "category": "preference", "importance": 5}, '
    '{"op": "delete", "field": "trait", "key": "短期状态"}]}'
)
_MAX_COMPACTION_PASSES = 6
_DEFAULT_LOGGER = logging.getLogger("komari_memory.profile_compaction")


class GenerateTextCallable(Protocol):
    """兼容 llm_provider.generate_text 的调用约定。"""

    async def __call__(
        self,
        *,
        prompt: str,
        model: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str: ...


class ProfileCompactionConfig(Protocol):
    """画像压缩所需的最小配置约定。"""

    llm_model_summary: str
    llm_temperature_summary: float
    llm_max_tokens_summary: int
    summary_chunk_token_limit: int
    profile_trait_limit: int


class LoggerLike(Protocol):
    """兼容标准 logging 和 nonebot.logger 的最小日志接口。"""

    def info(self, msg: object, *args: object) -> Any: ...

    def warning(self, msg: object, *args: object) -> Any: ...

    def exception(self, msg: object, *args: object) -> Any: ...


def _resolve_logger(log: LoggerLike | None) -> LoggerLike:
    return log or _DEFAULT_LOGGER


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _extract_json_from_markdown(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    pattern = r"```(?:json)?\s*\n([\s\S]*?)\n```"
    match = re.search(pattern, stripped)
    if match:
        return match.group(1).strip()

    lines = stripped.split("\n", 1)
    if len(lines) > 1:
        stripped = lines[1]
    return stripped.removesuffix("```").strip()


def _clamp_importance(value: Any, default: int = 3) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(5, parsed))


def _normalize_category(value: Any) -> str:
    category = str(value or "general").strip() or "general"
    if category not in _ALLOWED_CATEGORIES:
        return "general"
    return category


def _trait_sort_key(trait: dict[str, Any]) -> tuple[int, str, str]:
    return (
        _clamp_importance(trait.get("importance", 3)),
        str(trait.get("updated_at", "")),
        str(trait.get("key", "")),
    )


def _dedupe_traits(traits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for trait in sorted(traits, key=_trait_sort_key, reverse=True):
        key = str(trait.get("key", "")).strip()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(
            {
                "key": key,
                "value": str(trait.get("value", "")).strip(),
                "category": _normalize_category(trait.get("category", "general")),
                "importance": _clamp_importance(trait.get("importance", 3)),
                "updated_at": str(trait.get("updated_at", "")).strip(),
            }
        )
    return deduped


def profile_traits_to_list(profile: dict[str, Any]) -> list[dict[str, Any]]:
    traits_raw = profile.get("traits")
    normalized: list[dict[str, Any]] = []

    if isinstance(traits_raw, dict):
        for key, payload in traits_raw.items():
            if not isinstance(payload, dict):
                continue
            value = str(payload.get("value", "")).strip()
            if not value:
                continue
            normalized.append(
                {
                    "key": str(key).strip(),
                    "value": value,
                    "category": _normalize_category(payload.get("category", "general")),
                    "importance": _clamp_importance(payload.get("importance", 3)),
                    "updated_at": str(payload.get("updated_at", "")).strip(),
                }
            )
    elif isinstance(traits_raw, list):
        for payload in traits_raw:
            if not isinstance(payload, dict):
                continue
            key = str(payload.get("key", "")).strip()
            value = str(payload.get("value", "")).strip()
            if not key or not value:
                continue
            normalized.append(
                {
                    "key": key,
                    "value": value,
                    "category": _normalize_category(payload.get("category", "general")),
                    "importance": _clamp_importance(payload.get("importance", 3)),
                    "updated_at": str(payload.get("updated_at", "")).strip(),
                }
            )

    return _dedupe_traits(normalized)


def count_profile_traits(profile: dict[str, Any]) -> int:
    return len(profile_traits_to_list(profile))


def profile_json_length(profile: dict[str, Any]) -> int:
    return len(json.dumps(profile, ensure_ascii=False))


def normalize_profile_for_storage(
    profile: dict[str, Any],
    *,
    fallback_user_id: str = "",
    fallback_display_name: str = "",
    trait_limit: int | None = None,
) -> dict[str, Any]:
    user_id = str(profile.get("user_id", "")).strip() or fallback_user_id
    display_name = str(profile.get("display_name", "")).strip() or fallback_display_name
    traits = profile_traits_to_list(profile)
    if trait_limit is not None:
        traits = traits[: max(0, trait_limit)]

    traits_payload: dict[str, dict[str, Any]] = {}
    for trait in traits:
        key = str(trait.get("key", "")).strip()
        value = str(trait.get("value", "")).strip()
        if not key or not value:
            continue
        traits_payload[key] = {
            "value": value,
            "category": _normalize_category(trait.get("category", "general")),
            "importance": _clamp_importance(trait.get("importance", 3)),
            "updated_at": str(trait.get("updated_at", "")).strip() or _now_iso(),
        }

    return {
        "version": 1,
        "user_id": user_id,
        "display_name": display_name,
        "traits": traits_payload,
        "updated_at": _now_iso(),
    }


def summarize_profile_compaction_diff(
    before_profile: dict[str, Any],
    after_profile: dict[str, Any],
) -> dict[str, Any]:
    before_traits = profile_traits_to_list(before_profile)
    after_traits = profile_traits_to_list(after_profile)

    before_keys = [trait["key"] for trait in before_traits]
    after_keys = [trait["key"] for trait in after_traits]
    after_key_set = set(after_keys)
    before_key_set = set(before_keys)

    removed_keys = [key for key in before_keys if key not in after_key_set][:5]
    added_keys = [key for key in after_keys if key not in before_key_set][:5]
    kept_keys = [key for key in after_keys if key in before_key_set][:5]

    return {
        "before_traits": len(before_traits),
        "after_traits": len(after_traits),
        "before_chars": profile_json_length(before_profile),
        "after_chars": profile_json_length(after_profile),
        "removed_keys": removed_keys,
        "added_keys": added_keys,
        "kept_keys": kept_keys,
    }


def _build_prompt_traits_payload(
    traits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为压缩提示词构建 traits 列表（去除 updated_at 等内部字段）。"""
    return [
        {
            "key": trait["key"],
            "value": trait["value"],
            "category": trait["category"],
            "importance": trait["importance"],
        }
        for trait in traits
    ]


def _build_profile_compaction_prompt(
    *,
    traits: list[dict[str, Any]],
    trait_limit: int,
) -> str:
    traits_payload = _build_prompt_traits_payload(traits)
    return f"""请将以下用户画像 traits 压缩为最多 {trait_limit} 条长期稳定项，输出增量操作指令。输出必须使用简体中文。

压缩规则：
- 只保留身份、长期偏好、稳定习惯、关系认知、长期事实这类可复用的长期信息
- 删除短期状态、一次性事件、瞬时情绪、临时计划、当天小事
- 合并语义相近或重复的 traits，改写成更稳定、不易过时的表达
- trait 的 key 要简短、稳定、可长期复用，不要使用"当前状态""最近想法"这种时效性强的键名
- 不要编造新信息，不要输出解释，不要输出 Markdown

操作约束：
- op 只允许：add / replace / delete
- field 只允许：trait
- 当 op 为 add 或 replace 时，必须提供 key / value / category / importance
- 当 op 为 delete 时，只需提供 key
- 你输出的是"增量操作"，不是最终完整画像；禁止把完整 traits 全量重写出来
- 未在操作中提及的 trait 将原样保留
- 严禁输出 user_id、display_name 等由程序维护的字段

当前 traits JSON：
{json.dumps(traits_payload, ensure_ascii=False)}

请严格返回以下 JSON 格式：
{_COMPACTION_OPERATIONS_JSON_EXAMPLE}"""


def _estimate_prompt_tokens(
    *,
    traits: list[dict[str, Any]],
    trait_limit: int,
) -> int:
    return estimate_text_tokens(
        _build_profile_compaction_prompt(
            traits=traits,
            trait_limit=trait_limit,
        )
    )


def _chunk_traits_for_prompt(
    *,
    traits: list[dict[str, Any]],
    trait_limit: int,
    token_limit: int,
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current_chunk: list[dict[str, Any]] = []

    for trait in traits:
        candidate = [*current_chunk, trait]
        estimated_tokens = _estimate_prompt_tokens(
            traits=candidate,
            trait_limit=trait_limit,
        )
        if current_chunk and estimated_tokens > token_limit:
            chunks.append(current_chunk)
            current_chunk = [trait]
            continue
        current_chunk = candidate

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _parse_compaction_operations(parsed: Any) -> list[dict[str, Any]]:
    """从 LLM 返回的 JSON 中解析增量操作列表。

    Args:
        parsed: LLM 返回的 JSON 解析结果

    Returns:
        有效的增量操作列表

    Raises:
        TypeError: 当返回的 JSON 格式不符合预期时
    """
    if not isinstance(parsed, dict):
        msg = "画像压缩返回的 JSON 不是对象"
        raise TypeError(msg)
    operations = parsed.get("operations")
    if not isinstance(operations, list):
        msg = "画像压缩返回的 JSON 缺少 operations 数组"
        raise TypeError(msg)
    valid_ops: list[dict[str, Any]] = []
    for op in operations:
        if not isinstance(op, dict):
            continue
        op_type = str(op.get("op", "")).strip()
        field = str(op.get("field", "")).strip()
        if op_type not in {"add", "replace", "delete"} or field != "trait":
            continue
        key = str(op.get("key", "")).strip()
        if not key:
            continue
        valid_ops.append(op)
    return valid_ops


def _apply_compaction_operations(
    current_traits: list[dict[str, Any]],
    operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将增量操作应用到当前 traits，返回处理后的 traits 列表。

    Args:
        current_traits: 当前的 traits 列表
        operations: LLM 输出的增量操作列表

    Returns:
        应用操作后的 traits 列表
    """
    traits_by_key: dict[str, dict[str, Any]] = {
        trait["key"]: dict(trait) for trait in current_traits
    }
    now_iso = _now_iso()
    for operation in operations:
        op_type = str(operation.get("op", "")).strip()
        key = str(operation.get("key", "")).strip()
        if not key:
            continue
        if op_type == "delete":
            traits_by_key.pop(key, None)
        elif op_type in {"add", "replace"}:
            value = str(operation.get("value", "")).strip()
            if not value:
                continue
            if op_type == "add" and key in traits_by_key:
                continue
            traits_by_key[key] = {
                "key": key,
                "value": value,
                "category": _normalize_category(operation.get("category", "general")),
                "importance": _clamp_importance(operation.get("importance", 3)),
                "updated_at": now_iso,
            }
    return list(traits_by_key.values())


def _build_compacted_profile(
    *,
    original_profile: dict[str, Any],
    reduced_traits: list[dict[str, Any]],
    trait_limit: int,
) -> dict[str, Any]:
    """根据原始画像和压缩后的 traits 构建最终画像。

    user_id 和 display_name 始终取自原始画像，确保程序维护字段不被篡改。

    Args:
        original_profile: 压缩前的原始画像
        reduced_traits: 压缩后的 traits 列表
        trait_limit: traits 上限

    Returns:
        完整的压缩后画像
    """
    user_id = str(original_profile.get("user_id", "")).strip()
    display_name = str(original_profile.get("display_name", "")).strip()
    return normalize_profile_for_storage(
        {"traits": reduced_traits},
        fallback_user_id=user_id,
        fallback_display_name=display_name,
        trait_limit=trait_limit,
    )


async def _request_profile_compaction(
    *,
    traits: list[dict[str, Any]],
    config: ProfileCompactionConfig,
    llm_generate_text: GenerateTextCallable,
    trace_id: str,
    source: str,
    stage: str,
    pass_index: int,
    log: LoggerLike,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
) -> list[dict[str, Any]]:
    """请求 LLM 对 traits 进行压缩，返回增量操作应用后的 traits 列表。

    Args:
        traits: 待压缩的 traits 列表
        config: 压缩配置
        llm_generate_text: LLM 调用函数
        trace_id: 追踪 ID
        source: 调用来源
        stage: 压缩阶段（single / final / chunk）
        pass_index: 当前轮次
        log: 日志记录器
        chunk_index: 分批索引（分批时使用）
        chunk_total: 分批总数（分批时使用）

    Returns:
        压缩后的 traits 列表
    """
    prompt = _build_profile_compaction_prompt(
        traits=traits,
        trait_limit=config.profile_trait_limit,
    )
    estimated_prompt_tokens = estimate_text_tokens(prompt)
    log.info(
        f"[KomariMemory] 画像压缩请求: trace_id={trace_id} source={source} "
        f"stage={stage} pass={pass_index} chunk={chunk_index if chunk_index is not None else '-'}"
        f"/{chunk_total if chunk_total is not None else '-'} traits={len(traits)} "
        f"estimated_prompt_tokens={estimated_prompt_tokens} token_limit={config.summary_chunk_token_limit}"
    )

    response = await llm_generate_text(
        prompt=prompt,
        model=config.llm_model_summary,
        temperature=config.llm_temperature_summary,
        max_tokens=config.llm_max_tokens_summary,
        request_trace_id=trace_id,
        request_phase=f"profile_compaction_{stage}",
    )
    parsed = json.loads(_extract_json_from_markdown(response))
    operations = _parse_compaction_operations(parsed)
    return _apply_compaction_operations(traits, operations)


async def compact_profile_with_llm(
    *,
    profile: dict[str, Any],
    config: ProfileCompactionConfig,
    llm_generate_text: GenerateTextCallable,
    trace_id: str,
    source: str,
    log: LoggerLike | None = None,
) -> dict[str, Any]:
    """对超出上限的用户画像执行 LLM 压缩。

    压缩流程：提取当前 traits → LLM 输出增量操作（add/replace/delete） →
    程序侧应用操作 → 构建最终画像。user_id 和 display_name 始终由程序维护。

    Args:
        profile: 待压缩的原始画像
        config: 压缩配置
        llm_generate_text: LLM 调用函数
        trace_id: 追踪 ID
        source: 调用来源
        log: 日志记录器

    Returns:
        压缩后的完整画像

    Raises:
        RuntimeError: 当压缩无法收敛时
    """
    resolved_logger = _resolve_logger(log)
    normalized_profile = normalize_profile_for_storage(
        profile,
        fallback_user_id=str(profile.get("user_id", "")).strip(),
        fallback_display_name=str(profile.get("display_name", "")).strip(),
    )
    user_id = str(normalized_profile.get("user_id", "")).strip()
    display_name = str(normalized_profile.get("display_name", "")).strip()
    current_traits = profile_traits_to_list(normalized_profile)

    resolved_logger.info(
        f"[KomariMemory] 画像压缩开始: trace_id={trace_id} source={source} "
        f"user={user_id or '-'} traits={len(current_traits)} "
        f"chars={profile_json_length(normalized_profile)} limit={config.profile_trait_limit}"
    )

    if len(current_traits) <= config.profile_trait_limit:
        return normalize_profile_for_storage(
            normalized_profile,
            fallback_user_id=user_id,
            fallback_display_name=display_name,
            trait_limit=config.profile_trait_limit,
        )

    for pass_index in range(1, _MAX_COMPACTION_PASSES + 1):
        estimated_prompt_tokens = _estimate_prompt_tokens(
            traits=current_traits,
            trait_limit=config.profile_trait_limit,
        )
        if estimated_prompt_tokens <= config.summary_chunk_token_limit:
            reduced_traits = await _request_profile_compaction(
                traits=current_traits,
                config=config,
                llm_generate_text=llm_generate_text,
                trace_id=trace_id,
                source=source,
                stage="final" if pass_index > 1 else "single",
                pass_index=pass_index,
                log=resolved_logger,
            )
            compacted_profile = _build_compacted_profile(
                original_profile=normalized_profile,
                reduced_traits=reduced_traits,
                trait_limit=config.profile_trait_limit,
            )
            diff = summarize_profile_compaction_diff(
                normalized_profile, compacted_profile
            )
            resolved_logger.info(
                f"[KomariMemory] 画像压缩完成: trace_id={trace_id} source={source} user={user_id or '-'} "
                f"before_traits={diff['before_traits']} after_traits={diff['after_traits']} "
                f"before_chars={diff['before_chars']} after_chars={diff['after_chars']}"
            )
            return compacted_profile

        trait_chunks = _chunk_traits_for_prompt(
            traits=current_traits,
            trait_limit=config.profile_trait_limit,
            token_limit=config.summary_chunk_token_limit,
        )
        resolved_logger.info(
            f"[KomariMemory] 画像压缩触发分批: trace_id={trace_id} source={source} user={user_id or '-'} "
            f"pass={pass_index} chunk_count={len(trait_chunks)} traits={len(current_traits)} "
            f"estimated_prompt_tokens={estimated_prompt_tokens}"
        )

        reduced_traits: list[dict[str, Any]] = []
        for chunk_index, chunk_traits in enumerate(trait_chunks, start=1):
            chunk_reduced = await _request_profile_compaction(
                traits=chunk_traits,
                config=config,
                llm_generate_text=llm_generate_text,
                trace_id=trace_id,
                source=source,
                stage="chunk",
                pass_index=pass_index,
                chunk_index=chunk_index,
                chunk_total=len(trait_chunks),
                log=resolved_logger,
            )
            reduced_traits.extend(chunk_reduced)

        reduced_traits = _dedupe_traits(reduced_traits)
        resolved_logger.info(
            f"[KomariMemory] 画像压缩分批完成: trace_id={trace_id} source={source} user={user_id or '-'} "
            f"pass={pass_index} before_traits={len(current_traits)} after_traits={len(reduced_traits)} "
            f"chunk_count={len(trait_chunks)}"
        )
        if len(reduced_traits) >= len(current_traits):
            msg = "画像压缩未能收敛，无法进一步减少 traits"
            raise RuntimeError(msg)
        current_traits = reduced_traits

    msg = "画像压缩超过最大轮数仍未收敛"
    raise RuntimeError(msg)
