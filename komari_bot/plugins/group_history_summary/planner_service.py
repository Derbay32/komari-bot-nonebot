"""群聊历史总结规划服务。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from nonebot import logger
from nonebot.plugin import require

from .prompt_template import get_template

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nonebot.adapters.onebot.v11 import Bot

    from komari_bot.plugins.llm_provider.base_client import LLMCompletionResultSchema

    from .history_service import HistoryMessage

llm_provider = require("llm_provider")
character_binding = require("character_binding")

RECENT_SOURCE = "recent_group_messages"
USER_SOURCE = "messages_by_user"
TOPIC_SOURCE = "messages_by_topic"


@dataclass(slots=True)
class SummaryToolResult:
    """规划工具执行结果。"""

    source: str
    matched_count: int
    messages: list[HistoryMessage]
    filters: dict[str, Any]


@dataclass(slots=True)
class SummaryPlanResult:
    """总结规划结果。"""

    messages: list[HistoryMessage]
    tool_result: SummaryToolResult | None
    planner_note: str
    rounds_used: int


def build_summary_tools() -> list[dict[str, Any]]:
    """构建总结规划工具定义。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "fetch_recent_group_messages",
                "description": "拉取群最近若干条可用于总结的消息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "count": {
                            "type": "integer",
                            "description": "希望总结的消息条数",
                        },
                        "include_bot_replies": {
                            "type": "boolean",
                            "description": "是否包含机器人回复",
                            "default": False,
                        },
                    },
                    "required": ["count"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_messages_by_user",
                "description": "拉取最近历史中某个用户的消息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {
                            "type": "string",
                            "description": "用户 ID，优先精确匹配",
                        },
                        "display_name": {
                            "type": "string",
                            "description": "用户昵称或角色名",
                        },
                        "count": {
                            "type": "integer",
                            "description": "希望保留的命中条数",
                        },
                        "scan_limit": {
                            "type": "integer",
                            "description": "本地扫描的历史窗口大小",
                        },
                    },
                    "required": ["count"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_messages_by_topic",
                "description": "拉取最近历史中与主题关键词相关的消息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "主题关键词列表",
                        },
                        "count": {
                            "type": "integer",
                            "description": "希望保留的命中条数",
                        },
                        "scan_limit": {
                            "type": "integer",
                            "description": "本地扫描的历史窗口大小",
                        },
                    },
                    "required": ["keywords", "count"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _clamp_count(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _clamp_scan_limit(
    value: Any,
    *,
    count: int,
    hard_limit: int,
) -> int:
    default = min(hard_limit, max(count * 3, count))
    scan_limit = _clamp_count(value, count, hard_limit, default)
    return max(count, min(scan_limit, hard_limit))


def _normalize_keyword_list(raw_keywords: Any) -> list[str]:
    if not isinstance(raw_keywords, list):
        return []
    keywords: list[str] = []
    for item in raw_keywords:
        keyword = str(item).strip()
        if keyword and keyword not in keywords:
            keywords.append(keyword[:32])
        if len(keywords) >= 5:
            break
    return keywords


def _normalize_display_name(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized[:32]


def _match_display_name(message: HistoryMessage, display_name: str) -> bool:
    target = display_name.casefold()
    if not target:
        return False
    return target in message.nickname.casefold() or target in message.user_id.casefold()


def _filter_messages_for_summary(
    messages: list[HistoryMessage],
    bot_self_id: str,
    *,
    include_bot_replies: bool,
) -> list[HistoryMessage]:
    """过滤命令消息，以及机器人对这些命令的回复。"""
    import re

    summary_trigger_pattern = r"(?=.*总结)(?=.*\d).+"

    def _is_command_message(text: str) -> bool:
        normalized = "".join(text.split())
        if not normalized:
            return False
        if re.search(summary_trigger_pattern, normalized):
            return True
        return normalized.startswith(("。", ".", "/"))

    command_indexes = {
        idx
        for idx, message in enumerate(messages)
        if _is_command_message(message.content)
    }
    command_message_ids = {
        message.message_id
        for idx, message in enumerate(messages)
        if idx in command_indexes and message.message_id
    }

    bot_reply_indexes: set[int] = set()
    if not include_bot_replies and command_message_ids:
        for idx, message in enumerate(messages):
            if message.user_id != bot_self_id:
                continue
            if not message.reply_to_message_id:
                continue
            if message.reply_to_message_id in command_message_ids:
                bot_reply_indexes.add(idx)

    filtered: list[HistoryMessage] = []
    for idx, message in enumerate(messages):
        if idx in command_indexes:
            continue
        if not include_bot_replies and idx in bot_reply_indexes:
            continue
        if not include_bot_replies and message.user_id == bot_self_id:
            continue
        filtered.append(message)
    return filtered


async def _fetch_history_window(
    *,
    bot: Bot,
    group_id: str,
    count: int,
    batch_size: int,
) -> list[HistoryMessage]:
    from .history_service import fetch_group_history_messages

    return await fetch_group_history_messages(
        bot=bot,
        group_id=group_id,
        count=count,
        batch_size=batch_size,
        name_resolver=character_binding.get_character_name,
    )


def _serialize_tool_result(result: SummaryToolResult) -> str:
    payload = {
        "source": result.source,
        "matched_count": result.matched_count,
        "filters": result.filters,
        "messages": [
            {
                "timestamp": message.timestamp,
                "user_id": message.user_id,
                "nickname": message.nickname,
                "content": message.content,
            }
            for message in result.messages
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_planning_messages(user_request: str) -> list[dict[str, Any]]:
    template = get_template()
    return [
        {"role": "system", "content": template["planning_system_prompt"]},
        {"role": "user", "content": user_request},
    ]


async def _execute_recent_tool(
    *,
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    batch_size: int,
    min_summary_count: int,
    max_summary_count: int,
    arguments: dict[str, Any],
) -> SummaryToolResult:
    count = _clamp_count(
        arguments.get("count"),
        min_summary_count,
        max_summary_count,
        min_summary_count,
    )
    include_bot_replies = bool(arguments.get("include_bot_replies", False))
    messages = await _fetch_history_window(
        bot=bot,
        group_id=group_id,
        count=count,
        batch_size=batch_size,
    )
    filtered_messages = _filter_messages_for_summary(
        messages,
        bot_self_id,
        include_bot_replies=include_bot_replies,
    )
    return SummaryToolResult(
        source=RECENT_SOURCE,
        matched_count=len(filtered_messages),
        messages=filtered_messages[-count:],
        filters={
            "count": count,
            "include_bot_replies": include_bot_replies,
        },
    )


async def _execute_user_tool(
    *,
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    batch_size: int,
    min_summary_count: int,
    max_summary_count: int,
    summary_tool_scan_limit: int,
    arguments: dict[str, Any],
) -> SummaryToolResult:
    count = _clamp_count(
        arguments.get("count"),
        min_summary_count,
        max_summary_count,
        min_summary_count,
    )
    scan_limit = _clamp_scan_limit(
        arguments.get("scan_limit"),
        count=count,
        hard_limit=summary_tool_scan_limit,
    )
    user_id = _normalize_display_name(arguments.get("user_id"))
    display_name = _normalize_display_name(arguments.get("display_name"))
    messages = await _fetch_history_window(
        bot=bot,
        group_id=group_id,
        count=scan_limit,
        batch_size=batch_size,
    )
    filtered_messages = _filter_messages_for_summary(
        messages,
        bot_self_id,
        include_bot_replies=False,
    )
    matched = [
        message
        for message in filtered_messages
        if (user_id and message.user_id == user_id)
        or (display_name and _match_display_name(message, display_name))
    ]
    return SummaryToolResult(
        source=USER_SOURCE,
        matched_count=len(matched),
        messages=matched[-count:],
        filters={
            "count": count,
            "scan_limit": scan_limit,
            "user_id": user_id,
            "display_name": display_name,
        },
    )


async def _execute_topic_tool(
    *,
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    batch_size: int,
    min_summary_count: int,
    max_summary_count: int,
    summary_tool_scan_limit: int,
    arguments: dict[str, Any],
) -> SummaryToolResult:
    count = _clamp_count(
        arguments.get("count"),
        min_summary_count,
        max_summary_count,
        min_summary_count,
    )
    scan_limit = _clamp_scan_limit(
        arguments.get("scan_limit"),
        count=count,
        hard_limit=summary_tool_scan_limit,
    )
    keywords = _normalize_keyword_list(arguments.get("keywords"))
    messages = await _fetch_history_window(
        bot=bot,
        group_id=group_id,
        count=scan_limit,
        batch_size=batch_size,
    )
    filtered_messages = _filter_messages_for_summary(
        messages,
        bot_self_id,
        include_bot_replies=False,
    )
    matched = [
        message
        for message in filtered_messages
        if keywords
        and any(
            keyword.casefold() in message.content.casefold() for keyword in keywords
        )
    ]
    return SummaryToolResult(
        source=TOPIC_SOURCE,
        matched_count=len(matched),
        messages=matched[-count:],
        filters={
            "count": count,
            "scan_limit": scan_limit,
            "keywords": keywords,
        },
    )


async def plan_summary_request(
    *,
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    user_request: str,
    planning_model: str,
    planning_max_tokens: int,
    planning_round_limit: int,
    summary_default_count: int,
    min_summary_count: int,
    max_summary_count: int,
    summary_tool_scan_limit: int,
    fetch_batch_size: int,
) -> SummaryPlanResult:
    """使用工具调用规划总结所需的历史记录。"""
    messages = _build_planning_messages(user_request)
    tools = build_summary_tools()
    tool_result: SummaryToolResult | None = None
    rounds_used = 0

    tool_executor_map: dict[
        str, "Callable[[dict[str, Any]], Awaitable[SummaryToolResult]]"
    ] = {
        "fetch_recent_group_messages": lambda arguments: _execute_recent_tool(
            bot=bot,
            group_id=group_id,
            bot_self_id=bot_self_id,
            batch_size=fetch_batch_size,
            min_summary_count=min_summary_count,
            max_summary_count=max_summary_count,
            arguments=arguments,
        ),
        "fetch_messages_by_user": lambda arguments: _execute_user_tool(
            bot=bot,
            group_id=group_id,
            bot_self_id=bot_self_id,
            batch_size=fetch_batch_size,
            min_summary_count=min_summary_count,
            max_summary_count=max_summary_count,
            summary_tool_scan_limit=summary_tool_scan_limit,
            arguments=arguments,
        ),
        "fetch_messages_by_topic": lambda arguments: _execute_topic_tool(
            bot=bot,
            group_id=group_id,
            bot_self_id=bot_self_id,
            batch_size=fetch_batch_size,
            min_summary_count=min_summary_count,
            max_summary_count=max_summary_count,
            summary_tool_scan_limit=summary_tool_scan_limit,
            arguments=arguments,
        ),
    }

    for round_index in range(1, planning_round_limit + 1):
        rounds_used = round_index
        completion = cast(
            "LLMCompletionResultSchema",
            await llm_provider.generate_messages_completion(
                messages=messages,
                model=planning_model,
                temperature=0.1,
                max_tokens=planning_max_tokens,
                tools=tools,
                tool_choice="auto",
                parallel_tool_calls=False,
            ),
        )

        if not completion.tool_calls:
            return SummaryPlanResult(
                messages=(tool_result.messages if tool_result is not None else []),
                tool_result=tool_result,
                planner_note=completion.content.strip(),
                rounds_used=rounds_used,
            )

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": completion.content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": tool_call.type,
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.raw_arguments,
                    },
                }
                for tool_call in completion.tool_calls
            ],
        }
        messages.append(assistant_message)

        for tool_call in completion.tool_calls[:1]:
            executor = tool_executor_map.get(tool_call.function.name)
            if executor is None:
                logger.warning(
                    "[GroupHistorySummary] 收到未知工具调用: {}",
                    tool_call.function.name,
                )
                continue
            arguments = tool_call.parsed_arguments or {}
            tool_result = await executor(arguments)
            serialized_tool_result = _serialize_tool_result(tool_result)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id or tool_call.function.name,
                    "content": serialized_tool_result,
                }
            )

    if tool_result is not None:
        return SummaryPlanResult(
            messages=tool_result.messages,
            tool_result=tool_result,
            planner_note="规划轮数已达到上限，按当前取回的消息继续总结。",
            rounds_used=rounds_used,
        )

    fallback_result = await _execute_recent_tool(
        bot=bot,
        group_id=group_id,
        bot_self_id=bot_self_id,
        batch_size=fetch_batch_size,
        min_summary_count=min_summary_count,
        max_summary_count=max_summary_count,
        arguments={"count": summary_default_count, "include_bot_replies": False},
    )
    return SummaryPlanResult(
        messages=fallback_result.messages,
        tool_result=fallback_result,
        planner_note="规划未拿到有效工具结果，已回退为默认最近消息总结。",
        rounds_used=rounds_used,
    )
