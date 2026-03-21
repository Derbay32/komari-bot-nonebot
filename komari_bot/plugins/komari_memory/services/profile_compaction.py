"""Komari Memory 用户画像压缩服务。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from nonebot import logger

from .token_counter import estimate_text_tokens

if TYPE_CHECKING:
    from ..config_schema import KomariMemoryConfigSchema

_ALLOWED_CATEGORIES = {"preference", "fact", "relation", "general"}
_PROFILE_COMPACTION_JSON_EXAMPLE = (
    '{"user_id": "12345", "display_name": "阿明", "traits": '
    '[{"key": "长期兴趣", "value": "偏爱策略和养成类游戏", "category": "preference", "importance": 5}]}'
)
_MAX_COMPACTION_PASSES = 6


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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _extract_json_from_markdown(text: str) -> str:
    """从 markdown 代码块中提取 JSON。"""
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
    """把存储画像统一展开成 traits 列表。"""
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
    """统计画像里的 traits 数量。"""
    return len(profile_traits_to_list(profile))


def profile_json_length(profile: dict[str, Any]) -> int:
    """估算画像 JSON 长度。"""
    return len(json.dumps(profile, ensure_ascii=False))


def normalize_profile_for_storage(
    profile: dict[str, Any],
    *,
    fallback_user_id: str = "",
    fallback_display_name: str = "",
    trait_limit: int | None = None,
) -> dict[str, Any]:
    """把画像规范化为仓库存储格式。"""
    user_id = str(profile.get("user_id", "")).strip() or fallback_user_id
    display_name = (
        str(profile.get("display_name", "")).strip() or fallback_display_name
    )
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
    """生成压缩前后差异摘要，方便日志与 dry-run 输出。"""
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


def _build_prompt_profile_payload(
    *,
    user_id: str,
    display_name: str,
    traits: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "display_name": display_name,
        "traits": [
            {
                "key": trait["key"],
                "value": trait["value"],
                "category": trait["category"],
                "importance": trait["importance"],
            }
            for trait in traits
        ],
    }


def _build_profile_compaction_prompt(
    *,
    user_id: str,
    display_name: str,
    traits: list[dict[str, Any]],
    trait_limit: int,
) -> str:
    payload = _build_prompt_profile_payload(
        user_id=user_id,
        display_name=display_name,
        traits=traits,
    )
    return f"""请把下面这份用户画像压缩成最多 {trait_limit} 条长期稳定 traits，输出必须使用简体中文。

压缩规则：
- 只保留身份、长期偏好、稳定习惯、关系认知、长期事实这类可复用的长期信息
- 删除短期状态、一次性事件、瞬时情绪、临时计划、当天小事
- 合并语义相近或重复的 traits，改写成更稳定、不易过时的表达
- trait 的 key 要简短、稳定、可长期复用，不要使用“当前状态”“最近想法”这种时效性强的键名
- 不要编造新信息，不要输出解释，不要输出 Markdown

当前画像 JSON：
{json.dumps(payload, ensure_ascii=False)}

请严格返回以下 JSON 格式：
{_PROFILE_COMPACTION_JSON_EXAMPLE}"""


def _estimate_prompt_tokens(
    *,
    user_id: str,
    display_name: str,
    traits: list[dict[str, Any]],
    trait_limit: int,
) -> int:
    return estimate_text_tokens(
        _build_profile_compaction_prompt(
            user_id=user_id,
            display_name=display_name,
            traits=traits,
            trait_limit=trait_limit,
        )
    )


def _chunk_traits_for_prompt(
    *,
    user_id: str,
    display_name: str,
    traits: list[dict[str, Any]],
    trait_limit: int,
    token_limit: int,
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current_chunk: list[dict[str, Any]] = []

    for trait in traits:
        candidate = [*current_chunk, trait]
        estimated_tokens = _estimate_prompt_tokens(
            user_id=user_id,
            display_name=display_name,
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


async def _request_profile_compaction(
    *,
    user_id: str,
    display_name: str,
    traits: list[dict[str, Any]],
    config: KomariMemoryConfigSchema,
    llm_generate_text: GenerateTextCallable,
    trace_id: str,
    source: str,
    stage: str,
    pass_index: int,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
) -> dict[str, Any]:
    prompt = _build_profile_compaction_prompt(
        user_id=user_id,
        display_name=display_name,
        traits=traits,
        trait_limit=config.profile_trait_limit,
    )
    estimated_prompt_tokens = estimate_text_tokens(prompt)
    logger.info(
        "[KomariMemory] 画像压缩请求: trace_id={} source={} stage={} pass={} chunk={}/{} traits={} estimated_prompt_tokens={} token_limit={}",
        trace_id,
        source,
        stage,
        pass_index,
        chunk_index if chunk_index is not None else "-",
        chunk_total if chunk_total is not None else "-",
        len(traits),
        estimated_prompt_tokens,
        config.summary_chunk_token_limit,
    )

    response = await llm_generate_text(
        prompt=prompt,
        model=config.llm_model_summary,
        temperature=config.llm_temperature_summary,
        max_tokens=config.llm_max_tokens_summary,
        response_format={"type": "json_object"},
        request_trace_id=trace_id,
        request_phase=f"profile_compaction_{stage}",
    )
    parsed = json.loads(_extract_json_from_markdown(response))
    if not isinstance(parsed, dict):
        msg = "画像压缩返回的 JSON 不是对象"
        raise TypeError(msg)
    return normalize_profile_for_storage(
        parsed,
        fallback_user_id=user_id,
        fallback_display_name=display_name,
        trait_limit=config.profile_trait_limit,
    )


async def compact_profile_with_llm(
    *,
    profile: dict[str, Any],
    config: KomariMemoryConfigSchema,
    llm_generate_text: GenerateTextCallable,
    trace_id: str,
    source: str,
) -> dict[str, Any]:
    """使用 LLM 将用户画像压缩到指定 traits 上限。"""
    normalized_profile = normalize_profile_for_storage(
        profile,
        fallback_user_id=str(profile.get("user_id", "")).strip(),
        fallback_display_name=str(profile.get("display_name", "")).strip(),
    )
    user_id = str(normalized_profile.get("user_id", "")).strip()
    display_name = str(normalized_profile.get("display_name", "")).strip()
    current_traits = profile_traits_to_list(normalized_profile)

    logger.info(
        "[KomariMemory] 画像压缩开始: trace_id={} source={} user={} traits={} chars={} limit={}",
        trace_id,
        source,
        user_id or "-",
        len(current_traits),
        profile_json_length(normalized_profile),
        config.profile_trait_limit,
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
            user_id=user_id,
            display_name=display_name,
            traits=current_traits,
            trait_limit=config.profile_trait_limit,
        )
        if estimated_prompt_tokens <= config.summary_chunk_token_limit:
            compacted_profile = await _request_profile_compaction(
                user_id=user_id,
                display_name=display_name,
                traits=current_traits,
                config=config,
                llm_generate_text=llm_generate_text,
                trace_id=trace_id,
                source=source,
                stage="final" if pass_index > 1 else "single",
                pass_index=pass_index,
            )
            diff = summarize_profile_compaction_diff(normalized_profile, compacted_profile)
            logger.info(
                "[KomariMemory] 画像压缩完成: trace_id={} source={} user={} before_traits={} after_traits={} before_chars={} after_chars={}",
                trace_id,
                source,
                user_id or "-",
                diff["before_traits"],
                diff["after_traits"],
                diff["before_chars"],
                diff["after_chars"],
            )
            return compacted_profile

        trait_chunks = _chunk_traits_for_prompt(
            user_id=user_id,
            display_name=display_name,
            traits=current_traits,
            trait_limit=config.profile_trait_limit,
            token_limit=config.summary_chunk_token_limit,
        )
        logger.info(
            "[KomariMemory] 画像压缩触发分批: trace_id={} source={} user={} pass={} chunk_count={} traits={} estimated_prompt_tokens={}",
            trace_id,
            source,
            user_id or "-",
            pass_index,
            len(trait_chunks),
            len(current_traits),
            estimated_prompt_tokens,
        )

        reduced_traits: list[dict[str, Any]] = []
        for chunk_index, chunk_traits in enumerate(trait_chunks, start=1):
            chunk_profile = await _request_profile_compaction(
                user_id=user_id,
                display_name=display_name,
                traits=chunk_traits,
                config=config,
                llm_generate_text=llm_generate_text,
                trace_id=trace_id,
                source=source,
                stage="chunk",
                pass_index=pass_index,
                chunk_index=chunk_index,
                chunk_total=len(trait_chunks),
            )
            reduced_traits.extend(profile_traits_to_list(chunk_profile))

        reduced_traits = _dedupe_traits(reduced_traits)
        logger.info(
            "[KomariMemory] 画像压缩分批完成: trace_id={} source={} user={} pass={} before_traits={} after_traits={} chunk_count={}",
            trace_id,
            source,
            user_id or "-",
            pass_index,
            len(current_traits),
            len(reduced_traits),
            len(trait_chunks),
        )
        if len(reduced_traits) >= len(current_traits):
            msg = "画像压缩未能收敛，无法进一步减少 traits"
            raise RuntimeError(msg)
        current_traits = reduced_traits

    msg = "画像压缩超过最大轮数仍未收敛"
    raise RuntimeError(msg)
