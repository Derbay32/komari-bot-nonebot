"""群总结历史读取业务操作（无模型/提示词依赖）。

本模块从原规划器中提取平台历史读取、计数/扫描钳制、命令与机器人回复过滤、
用户/主题匹配以及完整度校验。DSH 总结预设通过可信回调转接这些函数；模型
参数不决定群身份、来源标签、时间范围或覆盖元数据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nonebot import logger

from komari_bot.plugins import character_binding

from .admission_gate import ensure_effect_admitted
from .history_service import (
    HistoryFetchMetadata,
    HistoryFetchResult,
    HistoryIncompleteError,
    HistoryMessage,
    fetch_group_history_messages,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nonebot.adapters.onebot.v11 import Bot

RECENT_SOURCE = "recent_group_messages"
USER_SOURCE = "messages_by_user"
TOPIC_SOURCE = "messages_by_topic"

SUMMARY_TRIGGER_PATTERN = re.compile(r"(?=.*总结)(?=.*\d).+")


@dataclass(slots=True)
class SummaryToolResult:
    """一次成功历史选择的业务结果。"""

    source: str
    matched_count: int
    messages: list[HistoryMessage]
    filters: dict[str, Any] = field(default_factory=dict)
    history_fetch: HistoryFetchMetadata | None = None


def clamp_count(value: object, minimum: int, maximum: int, default: int) -> int:
    """仅对正确类型的整数做既有上下界钳制。"""
    parsed = value if isinstance(value, int) and not isinstance(value, bool) else default
    return max(minimum, min(maximum, parsed))


def clamp_scan_limit(
    value: object,
    *,
    count: int,
    hard_limit: int,
) -> int:
    """按既有规则把扫描窗口限制在 [count, hard_limit]。"""
    default = min(hard_limit, max(count * 3, count))
    scan_limit = clamp_count(value, count, hard_limit, default)
    return max(count, min(scan_limit, hard_limit))


def normalize_keyword_list(raw_keywords: object) -> list[str]:
    """保留既有去重、5 个上限与 32 字符截断规则。"""
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


def normalize_display_name(value: object) -> str | None:
    """保留既有显示名 32 字符截断规则。"""
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized[:32]


def match_display_name(message: HistoryMessage, display_name: str) -> bool:
    """既有昵称/用户 ID 大小写不敏感包含匹配。"""
    target = display_name.casefold()
    if not target:
        return False
    return target in message.nickname.casefold() or target in message.user_id.casefold()


def filter_messages_for_summary(
    messages: list[HistoryMessage],
    bot_self_id: str,
    *,
    include_bot_replies: bool,
) -> list[HistoryMessage]:
    """过滤命令消息，以及机器人对这些命令的回复。"""
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


def _is_command_message(text: str) -> bool:
    normalized = "".join(text.split())
    if not normalized:
        return False
    if SUMMARY_TRIGGER_PATTERN.search(normalized):
        return True
    return normalized.startswith(("。", ".", "/"))


async def fetch_history_window(
    *,
    bot: Bot,
    group_id: str,
    count: int,
    batch_size: int,
) -> HistoryFetchResult:
    """按目标群准入读取平台群历史。"""
    # TSK-229：具体群平台读取为不可分瞬时效果；调用 get_group_msg_history
    # 前按目标群 BUSINESS 裁决，受限即抛 SummaryAdmissionDeniedError 终止。
    ensure_effect_admitted(group_id=group_id)
    return await fetch_group_history_messages(
        bot=bot,
        group_id=group_id,
        count=count,
        batch_size=batch_size,
        name_resolver=character_binding.get_character_name,
    )


def require_sufficient_history(
    result: HistoryFetchResult,
    *,
    minimum_coverage_ratio: float,
) -> None:
    """拒绝完整度不足的部分历史，避免生成貌似完整的总结。"""
    metadata = result.metadata
    if metadata.status == "partial" and metadata.coverage_ratio < minimum_coverage_ratio:
        raise HistoryIncompleteError(metadata)

    if metadata.status == "partial":
        logger.warning(
            "[GroupHistorySummary] 使用部分群历史继续总结: "
            "coverage={:.2%} completed_batches={} failed_batch={}",
            metadata.coverage_ratio,
            metadata.completed_batches,
            metadata.failed_batch,
        )


async def execute_recent_tool(
    *,
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    batch_size: int,
    min_summary_count: int,
    max_summary_count: int,
    history_min_coverage_ratio: float,
    arguments: Mapping[str, object],
) -> SummaryToolResult:
    count = clamp_count(
        arguments.get("count"),
        min_summary_count,
        max_summary_count,
        min_summary_count,
    )
    include_bot_replies = bool(arguments.get("include_bot_replies", False))
    fetch_result = await fetch_history_window(
        bot=bot,
        group_id=group_id,
        count=count,
        batch_size=batch_size,
    )
    require_sufficient_history(
        fetch_result,
        minimum_coverage_ratio=history_min_coverage_ratio,
    )
    filtered_messages = filter_messages_for_summary(
        fetch_result.messages,
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
        history_fetch=fetch_result.metadata,
    )


async def execute_user_tool(
    *,
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    batch_size: int,
    min_summary_count: int,
    max_summary_count: int,
    summary_tool_scan_limit: int,
    history_min_coverage_ratio: float,
    arguments: Mapping[str, object],
) -> SummaryToolResult:
    count = clamp_count(
        arguments.get("count"),
        min_summary_count,
        max_summary_count,
        min_summary_count,
    )
    scan_limit = clamp_scan_limit(
        arguments.get("scan_limit"),
        count=count,
        hard_limit=summary_tool_scan_limit,
    )
    user_id = normalize_display_name(arguments.get("user_id"))
    display_name = normalize_display_name(arguments.get("display_name"))
    fetch_result = await fetch_history_window(
        bot=bot,
        group_id=group_id,
        count=scan_limit,
        batch_size=batch_size,
    )
    require_sufficient_history(
        fetch_result,
        minimum_coverage_ratio=history_min_coverage_ratio,
    )
    filtered_messages = filter_messages_for_summary(
        fetch_result.messages,
        bot_self_id,
        include_bot_replies=False,
    )
    matched = [
        message
        for message in filtered_messages
        if (user_id and message.user_id == user_id)
        or (display_name and match_display_name(message, display_name))
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
        history_fetch=fetch_result.metadata,
    )


async def execute_topic_tool(
    *,
    bot: Bot,
    group_id: str,
    bot_self_id: str,
    batch_size: int,
    min_summary_count: int,
    max_summary_count: int,
    summary_tool_scan_limit: int,
    history_min_coverage_ratio: float,
    arguments: Mapping[str, object],
) -> SummaryToolResult:
    count = clamp_count(
        arguments.get("count"),
        min_summary_count,
        max_summary_count,
        min_summary_count,
    )
    scan_limit = clamp_scan_limit(
        arguments.get("scan_limit"),
        count=count,
        hard_limit=summary_tool_scan_limit,
    )
    keywords = normalize_keyword_list(arguments.get("keywords"))
    fetch_result = await fetch_history_window(
        bot=bot,
        group_id=group_id,
        count=scan_limit,
        batch_size=batch_size,
    )
    require_sufficient_history(
        fetch_result,
        minimum_coverage_ratio=history_min_coverage_ratio,
    )
    filtered_messages = filter_messages_for_summary(
        fetch_result.messages,
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
        history_fetch=fetch_result.metadata,
    )


__all__ = [
    "RECENT_SOURCE",
    "TOPIC_SOURCE",
    "USER_SOURCE",
    "SummaryToolResult",
    "clamp_count",
    "clamp_scan_limit",
    "execute_recent_tool",
    "execute_topic_tool",
    "execute_user_tool",
    "fetch_history_window",
    "filter_messages_for_summary",
    "match_display_name",
    "normalize_display_name",
    "normalize_keyword_list",
    "require_sufficient_history",
]
