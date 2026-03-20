"""Komari Memory LLM 调用服务，封装 llm_provider 插件。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nonebot import logger
from nonebot.plugin import require

from ..config_schema import KomariMemoryConfigSchema  # noqa: TC001
from ..core.retry import retry_async
from .token_counter import estimate_text_tokens

if TYPE_CHECKING:
    from .redis_manager import MessageSchema

# 依赖 llm_provider 插件
llm_provider = require("llm_provider")

_JSON_RESPONSE_EXAMPLE = (
    '{"summary": "...", "user_profiles": '
    '[{"user_id": "12345", "display_name": "阿明", "traits": '
    '[{"key": "喜欢的食物", "value": "拉面", "category": "preference", "importance": 4}]}], '
    '"user_interactions": [{"user_id": "12345", "file_type": "用户的近期对鞠行为备忘录", '
    '"description": "这是我在心里对这个用户近期行为的悄悄记录。用来提醒自己这个人平时是怎么对我的，下次和他说话时应该保持什么态度。", '
    '"records": [{"event": "用好吃的诱惑我", "result": "咽了口水，稍微凑近了过去", "emotion": "有点警惕但很想吃"}], '
    '"summary": "是个经常用食物钓我的骗子先生……但也不是坏人。"}], '
    '"importance": 3}'
)
_MESSAGE_SPLIT_MARKER = "[分片0000] "
_MAX_EXISTING_TRAITS_PER_USER = 5
_MAX_EXISTING_INTERACTION_RECORDS = 2
_TRUNCATED_CONTEXT_MARKER = "【提示】其余已有记录已按 token 上限省略，请仅基于当前提供的已知信息做去重与覆盖。\n\n"


@dataclass(frozen=True)
class _ConversationChunk:
    lines: list[str]
    estimated_tokens: int


@dataclass(frozen=True)
class _ExistingContextBuildResult:
    text: str
    estimated_tokens: int
    included_profiles: int
    included_interactions: int
    truncated: bool


def _extract_json_from_markdown(text: str) -> str:
    """从 markdown 代码块中提取 JSON（保留作为降级方案）。"""
    text = text.strip()

    if not text.startswith("```"):
        return text

    pattern = r"```(?:json)?\s*\n([\s\S]*?)\n```"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    if text.startswith("```"):
        lines = text.split("\n", 1)
        if len(lines) > 1:
            text = lines[1]
        text = text.removesuffix("```")

    return text.strip()


def _extract_tag_content(text: str, tag: str) -> str:
    """从 LLM 回复中提取指定 XML 标签内的内容。"""
    pattern = rf"<{tag}>([\s\S]*)</{tag}>"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    logger.warning("[KomariMemory] 未找到 <{}> 标签，使用原始回复", tag)
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def _build_existing_context(
    existing_profiles: list[dict] | None = None,
    existing_interactions: list[dict] | None = None,
) -> _ExistingContextBuildResult:
    return _build_existing_context_with_budget(
        existing_profiles=existing_profiles,
        existing_interactions=existing_interactions,
        token_budget=None,
    )


def _compact_profile_line(profile: dict[str, Any]) -> str | None:
    """压缩单个用户画像，避免把历史冗余字段整包塞进 prompt。"""
    user_id = str(profile.get("user_id", "")).strip()
    if not user_id:
        return None

    display_name = str(profile.get("display_name", "")).strip()
    traits_raw = profile.get("traits")
    compact_traits: list[dict[str, Any]] = []
    if isinstance(traits_raw, dict):
        sortable_traits: list[tuple[str, dict[str, Any]]] = []
        for key, raw in traits_raw.items():
            if isinstance(raw, dict):
                sortable_traits.append((str(key), raw))
        sortable_traits.sort(
            key=lambda item: (
                str(item[1].get("updated_at", "")),
                int(item[1].get("importance", 0) or 0),
            ),
            reverse=True,
        )
        for key, raw in sortable_traits[:_MAX_EXISTING_TRAITS_PER_USER]:
            value = str(raw.get("value", "")).strip()
            if not value:
                continue
            compact_traits.append(
                {
                    "key": key,
                    "value": value,
                    "category": str(raw.get("category", "general")).strip() or "general",
                    "importance": int(raw.get("importance", 3) or 3),
                }
            )
    elif isinstance(traits_raw, list):
        for raw in traits_raw[:_MAX_EXISTING_TRAITS_PER_USER]:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key", "")).strip()
            value = str(raw.get("value", "")).strip()
            if not key or not value:
                continue
            compact_traits.append(
                {
                    "key": key,
                    "value": value,
                    "category": str(raw.get("category", "general")).strip() or "general",
                    "importance": int(raw.get("importance", 3) or 3),
                }
            )

    return (
        f"- [user_id:{user_id}] display_name={display_name} "
        f"traits={json.dumps(compact_traits, ensure_ascii=False)}"
    )


def _compact_interaction_line(interaction: dict[str, Any]) -> str | None:
    """压缩单个互动历史，保留近期摘要与最近几条 records。"""
    user_id = str(interaction.get("user_id", "")).strip()
    if not user_id:
        return None

    records_raw = interaction.get("records")
    compact_records: list[dict[str, str]] = []
    if isinstance(records_raw, list):
        for record in records_raw[-_MAX_EXISTING_INTERACTION_RECORDS:]:
            if not isinstance(record, dict):
                continue
            compact_records.append(
                {
                    "event": str(record.get("event", "")).strip(),
                    "result": str(record.get("result", "")).strip(),
                    "emotion": str(record.get("emotion", "")).strip(),
                }
            )

    compact_summary = str(interaction.get("summary", "")).strip()
    return (
        f"- [user_id:{user_id}] interaction_summary={compact_summary} "
        f"recent_records={json.dumps(compact_records, ensure_ascii=False)}"
    )


def _build_existing_context_with_budget(
    *,
    existing_profiles: list[dict] | None = None,
    existing_interactions: list[dict] | None = None,
    token_budget: int | None,
) -> _ExistingContextBuildResult:
    """构建可控大小的已有画像与互动历史提示。"""
    instruction_block = (
        "【重要指示】\n"
        "- 你输出的是 user_profiles（按用户聚合），不要输出扁平 entities 列表\n"
        "- 如果对话中发现与已有画像矛盾的新信息，请用新信息覆盖旧值（同 key 覆盖）\n"
        "- 如果对话中没有提到某个旧特征，不要重复输出它\n"
        "- 只输出需要新增或更新的画像特征\n"
        "- 对于互动历史，请在已有记录的基础上追加新的 records（注意：如果 records 总数超过6条，请只保留最近的6条记录）\n\n"
    )
    instruction_tokens = estimate_text_tokens(instruction_block)
    if token_budget is not None and token_budget <= instruction_tokens:
        return _ExistingContextBuildResult(
            text="",
            estimated_tokens=0,
            included_profiles=0,
            included_interactions=0,
            truncated=bool(existing_profiles or existing_interactions),
        )

    data_budget = None
    if token_budget is not None:
        data_budget = max(0, token_budget - instruction_tokens)

    blocks: list[str] = []
    included_profiles = 0
    included_interactions = 0
    truncated = False

    def _current_tokens() -> int:
        return estimate_text_tokens("".join(blocks))

    def _append_block(block: str) -> bool:
        if data_budget is None:
            blocks.append(block)
            return True
        candidate = "".join(blocks) + block
        if estimate_text_tokens(candidate) > data_budget:
            return False
        blocks.append(block)
        return True

    profile_lines = [
        line
        for profile in (existing_profiles or [])
        if (line := _compact_profile_line(profile)) is not None
    ]
    if profile_lines:
        profile_header = "【已知用户画像（数据库中已有记录）】\n以下是目前已存储的用户画像：\n"
        header_added = False
        for line in profile_lines:
            block = f"{profile_header}{line}\n" if not header_added else f"{line}\n"
            if _append_block(block):
                header_added = True
                included_profiles += 1
            else:
                truncated = True
                break
        if header_added:
            _append_block("\n")

    interaction_lines = [
        line
        for interaction in (existing_interactions or [])
        if (line := _compact_interaction_line(interaction)) is not None
    ]
    if interaction_lines:
        interaction_header = "以下是目前已存储的用户互动历史：\n"
        header_added = False
        for line in interaction_lines:
            block = (
                f"{interaction_header}{line}\n" if not header_added else f"{line}\n"
            )
            if _append_block(block):
                header_added = True
                included_interactions += 1
            else:
                truncated = True
                break
        if header_added:
            _append_block("\n")

    if truncated:
        _append_block(_TRUNCATED_CONTEXT_MARKER)

    data_text = "".join(blocks)
    if not data_text:
        return _ExistingContextBuildResult(
            text="",
            estimated_tokens=0,
            included_profiles=0,
            included_interactions=0,
            truncated=truncated,
        )

    full_text = data_text + instruction_block
    return _ExistingContextBuildResult(
        text=full_text,
        estimated_tokens=estimate_text_tokens(full_text),
        included_profiles=included_profiles,
        included_interactions=included_interactions,
        truncated=truncated,
    )


def _build_summary_prompt(
    conversation_text: str,
    *,
    existing_context: str = "",
) -> str:
    """构建原始对话总结提示词。"""
    return f"""请总结以下群聊或私聊对话，提取每个用户的画像信息，并评估对话的重要性。输出必须使用简体中文。

每条消息格式为 [user_id:xxx] 昵称: 内容。请你在提取时将 user_id 准确关联。

{conversation_text}

{existing_context}【任务一：用户画像提取（按用户聚合）】
- 提取对话的核心内容，形成 summary（简短总结）。
- 输出 `user_profiles` 数组，每个元素对应一个用户，字段包含：
  - user_id
  - display_name（可为空字符串）
  - traits（数组），每个 trait 包含 key/value/category/importance
- category 仅可取：preference/fact/relation/general

【任务二：主观互动备忘录提取】
- 你必须基于《败犬女主太多了！》中"小鞠知花"的人设视角，为有明显互动行为的用户，提取出在互动期间该用户的行为记录。这将被作为"小鞠在心里对近期互动过的用户的悄悄记录"。
- 数据格式要求如下：必须包含 user_id, file_type, description, records(包括 event[行为], result[反应], emotion[感受]), summary。

【任务三：评估重要性】
请按以下标准评估重要性（1-5分）：
- 1分：无意义的闲聊、表情包测试、简短问候
- 2分：简单的日常对话
- 3分：一般的讨论交流
- 4分：有意义的话题讨论或较深的互动
- 5分：重要的决定、约定、深度的设定或情感交流

请严格返回以下 JSON 格式：
{_JSON_RESPONSE_EXAMPLE}"""


def _build_merge_prompt(
    chunk_summaries_text: str,
    *,
    existing_context: str = "",
) -> str:
    """构建分段总结后的二次汇总提示词。"""
    return f"""请将以下按时间顺序排列的分段总结整合成一份最终总结。输出必须使用简体中文。

每个分段总结都已经是结构化结果，包含 summary、user_profiles、user_interactions、importance。
请你基于这些分段结果，输出一份全局统一的最终 JSON：
- 合并重复 user_id 的画像信息，只保留新增或更新的 traits
- 合并互动历史，records 总数最多保留最近6条
- 产出一份整体 summary 和整体 importance
- 不要按分段分别输出，不要解释推理过程

{chunk_summaries_text}

{existing_context}请严格返回以下 JSON 格式：
{_JSON_RESPONSE_EXAMPLE}"""


def _format_message_prefix(
    message: MessageSchema,
    config: KomariMemoryConfigSchema,
) -> str:
    if message.is_bot:
        return f"[bot] {config.bot_nickname}: "
    return f"[user_id:{message.user_id}] {message.user_nickname}: "


def _format_message_line(
    message: MessageSchema,
    config: KomariMemoryConfigSchema,
) -> str:
    return f"{_format_message_prefix(message, config)}{message.content}"


def _estimate_payload_limit(config: KomariMemoryConfigSchema) -> int:
    """估算单个原文分段可承载的消息正文大小。"""
    prompt_base_tokens = estimate_text_tokens(_build_summary_prompt(""))
    return max(1, config.summary_chunk_token_limit - prompt_base_tokens)


def _split_oversized_message(
    *,
    prefix: str,
    content: str,
    payload_limit: int,
) -> list[str]:
    """把单条超长消息拆成多个分片，避免单条消息打爆输入上限。"""
    max_content_tokens = max(
        1,
        payload_limit - estimate_text_tokens(prefix) - estimate_text_tokens(_MESSAGE_SPLIT_MARKER),
    )

    parts: list[str] = []
    cursor = 0
    part_index = 1
    while cursor < len(content):
        piece = content[cursor : cursor + max_content_tokens]
        line = f"{prefix}[分片{part_index}] {piece}"
        while piece and estimate_text_tokens(line) > payload_limit:
            piece = piece[:-1]
            line = f"{prefix}[分片{part_index}] {piece}"

        if not piece:
            piece = content[cursor : cursor + 1]
            line = f"{prefix}[分片{part_index}] {piece}"

        parts.append(line)
        cursor += len(piece)
        part_index += 1

    return parts


def _chunk_formatted_messages(
    messages: list[MessageSchema],
    config: KomariMemoryConfigSchema,
) -> tuple[list[_ConversationChunk], bool]:
    """按当前近似 token 口径把原始消息切成多个分段。"""
    payload_limit = _estimate_payload_limit(config)
    prepared_lines: list[str] = []
    oversized_message_split = False

    for message in messages:
        formatted_line = _format_message_line(message, config)
        if estimate_text_tokens(formatted_line) <= payload_limit:
            prepared_lines.append(formatted_line)
            continue

        oversized_message_split = True
        prepared_lines.extend(
            _split_oversized_message(
                prefix=_format_message_prefix(message, config),
                content=message.content,
                payload_limit=payload_limit,
            )
        )

    chunks: list[_ConversationChunk] = []
    current_lines: list[str] = []
    current_tokens = 0
    for line in prepared_lines:
        line_tokens = estimate_text_tokens(line)
        separator_tokens = 1 if current_lines else 0
        if current_lines and current_tokens + separator_tokens + line_tokens > payload_limit:
            chunks.append(
                _ConversationChunk(
                    lines=list(current_lines),
                    estimated_tokens=current_tokens,
                )
            )
            current_lines = [line]
            current_tokens = line_tokens
            continue

        if current_lines:
            current_tokens += 1
        current_lines.append(line)
        current_tokens += line_tokens

    if current_lines:
        chunks.append(
            _ConversationChunk(
                lines=list(current_lines),
                estimated_tokens=current_tokens,
            )
        )

    return chunks, oversized_message_split


def _normalize_summary_result(result: dict[str, Any]) -> dict[str, Any]:
    """规范化总结结果结构。"""
    normalized_result = dict(result)
    normalized_result["summary"] = str(normalized_result.get("summary", "")).strip()

    if not isinstance(normalized_result.get("user_profiles"), list):
        normalized_result["user_profiles"] = []
    normalized_profiles: list[dict[str, Any]] = []
    for profile in normalized_result["user_profiles"]:
        if not isinstance(profile, dict):
            continue
        user_id = str(profile.get("user_id", "")).strip()
        if not user_id:
            continue
        traits = profile.get("traits")
        normalized_traits: list[dict[str, Any]] = []
        if isinstance(traits, list):
            for trait in traits:
                if not isinstance(trait, dict):
                    continue
                key = str(trait.get("key", "")).strip()
                value = str(trait.get("value", "")).strip()
                if not key or not value:
                    continue
                category = str(trait.get("category", "general")).strip() or "general"
                if category not in {"preference", "fact", "relation", "general"}:
                    category = "general"
                try:
                    importance = int(trait.get("importance", 3))
                except (TypeError, ValueError):
                    importance = 3
                normalized_traits.append(
                    {
                        "key": key,
                        "value": value,
                        "category": category,
                        "importance": max(1, min(5, importance)),
                    }
                )

        normalized_profiles.append(
            {
                "user_id": user_id,
                "display_name": str(profile.get("display_name", "")).strip(),
                "traits": normalized_traits,
            }
        )
    normalized_result["user_profiles"] = normalized_profiles

    if not isinstance(normalized_result.get("user_interactions"), list):
        normalized_result["user_interactions"] = []
    normalized_interactions: list[dict[str, Any]] = []
    for interaction in normalized_result["user_interactions"]:
        if not isinstance(interaction, dict):
            continue
        user_id = str(interaction.get("user_id", "")).strip()
        if not user_id:
            continue
        records = interaction.get("records")
        normalized_records = records if isinstance(records, list) else []
        if len(normalized_records) > 6:
            normalized_records = normalized_records[-6:]
        normalized_interactions.append(
            {
                "user_id": user_id,
                "file_type": str(interaction.get("file_type", "")).strip(),
                "description": str(interaction.get("description", "")).strip(),
                "records": normalized_records,
                "summary": str(interaction.get("summary", "")).strip(),
            }
        )
    normalized_result["user_interactions"] = normalized_interactions

    try:
        importance = int(normalized_result.get("importance", 3))
        normalized_result["importance"] = max(1, min(5, importance))
    except (TypeError, ValueError):
        normalized_result["importance"] = 3

    return normalized_result


async def _request_structured_summary(
    *,
    prompt: str,
    config: KomariMemoryConfigSchema,
    trace_id: str,
    stage: str,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
    estimated_input_tokens: int | None = None,
) -> dict[str, Any]:
    """调用总结模型并解析结构化 JSON。"""
    estimated_prompt_tokens = estimate_text_tokens(prompt)
    logger.info(
        "[KomariMemory] 总结请求追踪: trace_id={} stage={} chunk={}/{} estimated_input_tokens={} estimated_prompt_tokens={}",
        trace_id,
        stage,
        chunk_index if chunk_index is not None else "-",
        chunk_total if chunk_total is not None else "-",
        estimated_input_tokens if estimated_input_tokens is not None else "-",
        estimated_prompt_tokens,
    )
    if estimated_prompt_tokens > config.summary_chunk_token_limit:
        logger.warning(
            "[KomariMemory] 总结请求估算 token 超过分段上限: trace_id={} stage={} estimated={} limit={}",
            trace_id,
            stage,
            estimated_prompt_tokens,
            config.summary_chunk_token_limit,
        )

    response = await llm_provider.generate_text(
        prompt=prompt,
        model=config.llm_model_summary,
        temperature=config.llm_temperature_summary,
        max_tokens=config.llm_max_tokens_summary,
        request_trace_id=trace_id,
        request_phase=stage,
        request_chunk_index=chunk_index,
        request_chunk_total=chunk_total,
    )

    json_text = _extract_json_from_markdown(response)
    parsed = json.loads(json_text)
    if not isinstance(parsed, dict):
        msg = "总结模型返回的 JSON 不是对象"
        raise TypeError(msg)
    return _normalize_summary_result(parsed)


def _serialize_chunk_results_for_merge(
    chunk_results: list[dict[str, Any]],
    chunk_tokens: list[int],
) -> str:
    """将分段总结结果序列化为二次汇总输入。"""
    blocks = []
    total = len(chunk_results)
    for index, (result, estimated_tokens) in enumerate(
        zip(chunk_results, chunk_tokens, strict=True),
        start=1,
    ):
        blocks.append(
            f"【分段{index}/{total}】\n"
            f"estimated_input_tokens={estimated_tokens}\n"
            f"{json.dumps(result, ensure_ascii=False)}"
        )
    return "\n\n".join(blocks)


@retry_async(max_attempts=3, base_delay=1.0)
async def generate_reply(
    config: KomariMemoryConfigSchema,
    messages: list[dict] | None = None,
    user_message: str = "",
    system_prompt: str = "",
) -> str:
    """生成回复（使用 OpenAI messages 格式，带重试机制，支持多模态）。"""
    if messages is not None:
        raw_response = await llm_provider.generate_text_with_messages(
            messages=messages,
            model=config.llm_model_chat,
            temperature=config.llm_temperature_chat,
            max_tokens=config.llm_max_tokens_chat,
        )
    else:
        raw_response = await llm_provider.generate_text(
            prompt=user_message,
            model=config.llm_model_chat,
            system_instruction=system_prompt,
            temperature=config.llm_temperature_chat,
            max_tokens=config.llm_max_tokens_chat,
        )

    return _extract_tag_content(raw_response, config.response_tag)


@retry_async(max_attempts=3, base_delay=1.0)
async def summarize_conversation(
    messages: list[MessageSchema],
    config: KomariMemoryConfigSchema,
    existing_profiles: list[dict] | None = None,
    existing_interactions: list[dict] | None = None,
) -> dict:
    """总结对话，提取用户画像，并评估重要性（带重试机制）。"""
    trace_id = f"memsum-{uuid4().hex[:8]}"
    group_id = messages[0].group_id if messages else "-"
    chunks, oversized_message_split = _chunk_formatted_messages(messages, config)
    logger.info(
        "[KomariMemory] 总结追踪开始: trace_id={} group={} messages={} chunk_limit={} existing_profiles={} existing_interactions={}",
        trace_id,
        group_id,
        len(messages),
        config.summary_chunk_token_limit,
        len(existing_profiles or []),
        len(existing_interactions or []),
    )
    if not chunks:
        return _normalize_summary_result({})

    if len(chunks) == 1:
        single_message_text = "\n".join(chunks[0].lines)
        single_base_prompt = _build_summary_prompt(single_message_text)
        single_context_budget = max(
            0,
            config.summary_chunk_token_limit - estimate_text_tokens(single_base_prompt),
        )
        existing_context_result = _build_existing_context_with_budget(
            existing_profiles=existing_profiles,
            existing_interactions=existing_interactions,
            token_budget=single_context_budget,
        )
        single_prompt = _build_summary_prompt(
            single_message_text,
            existing_context=existing_context_result.text,
        )
        logger.debug(
            "[KomariMemory] 总结输入未触发分段: trace_id={} estimated_input_tokens={} context_tokens={} included_profiles={} included_interactions={} context_truncated={}",
            trace_id,
            chunks[0].estimated_tokens,
            existing_context_result.estimated_tokens,
            existing_context_result.included_profiles,
            existing_context_result.included_interactions,
            existing_context_result.truncated,
        )
        return await _request_structured_summary(
            prompt=single_prompt,
            config=config,
            trace_id=trace_id,
            stage="single",
            chunk_index=1,
            chunk_total=1,
            estimated_input_tokens=chunks[0].estimated_tokens,
        )

    chunk_tokens = [chunk.estimated_tokens for chunk in chunks]
    logger.info(
        "[KomariMemory] 总结输入触发分段: trace_id={} chunks={} estimated_input_tokens={} oversized_message_split={}",
        trace_id,
        len(chunks),
        chunk_tokens,
        oversized_message_split,
    )

    chunk_results: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_prompt = _build_summary_prompt("\n".join(chunk.lines))
        chunk_results.append(
            await _request_structured_summary(
                prompt=chunk_prompt,
                config=config,
                trace_id=trace_id,
                stage="chunk",
                chunk_index=index,
                chunk_total=len(chunks),
                estimated_input_tokens=chunk.estimated_tokens,
            )
        )

    merge_source = _serialize_chunk_results_for_merge(chunk_results, chunk_tokens)
    merge_base_prompt = _build_merge_prompt(merge_source)
    merge_context_budget = max(
        0,
        config.summary_chunk_token_limit - estimate_text_tokens(merge_base_prompt),
    )
    existing_context_result = _build_existing_context_with_budget(
        existing_profiles=existing_profiles,
        existing_interactions=existing_interactions,
        token_budget=merge_context_budget,
    )
    merge_prompt = _build_merge_prompt(
        merge_source,
        existing_context=existing_context_result.text,
    )
    logger.info(
        "[KomariMemory] 开始执行总结二次汇总: trace_id={} chunk_summaries={} context_tokens={} included_profiles={} included_interactions={} context_truncated={}",
        trace_id,
        len(chunk_results),
        existing_context_result.estimated_tokens,
        existing_context_result.included_profiles,
        existing_context_result.included_interactions,
        existing_context_result.truncated,
    )
    return await _request_structured_summary(
        prompt=merge_prompt,
        config=config,
        trace_id=trace_id,
        stage="merge",
        estimated_input_tokens=estimate_text_tokens(merge_source),
    )
