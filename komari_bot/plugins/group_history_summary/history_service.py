"""NapCat 群历史消息拉取服务。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nonebot import logger

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Bot


@dataclass(slots=True)
class HistoryMessage:
    """群历史消息结构。"""

    user_id: str
    nickname: str
    content: str
    timestamp: int
    message_seq: int
    # 当前消息自身 ID（用于被 reply 关联）
    message_id: str | None
    # 当前消息 reply 的目标 message_id（用于识别“机器人回复某条命令”）
    reply_to_message_id: str | None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_supported_actions(payload: Any) -> list[str]:
    """从 get_supported_actions 响应中提取动作列表。

    不同 OneBot 实现返回结构并不完全一致，这里统一兼容：
    1. 直接返回 list[str]
    2. 返回 {"actions": [...]}
    3. 返回 {"supported_actions": [...]}
    4. 返回 {"data": [...]} 或 {"data": {"actions"/"supported_actions": [...]}}
    """
    actions: list[str] = []

    if isinstance(payload, list):
        # 结构1：直接是数组
        actions = [str(item) for item in payload]
    elif isinstance(payload, dict):
        if isinstance(payload.get("actions"), list):
            # 结构2：顶层 actions
            actions = [str(item) for item in payload["actions"]]
        elif isinstance(payload.get("supported_actions"), list):
            # 结构3：顶层 supported_actions
            actions = [str(item) for item in payload["supported_actions"]]
        elif isinstance(payload.get("data"), list):
            # 结构4-1：data 是数组
            actions = [str(item) for item in payload["data"]]
        elif isinstance(payload.get("data"), dict):
            # 结构4-2：data 是对象，动作列表在 data 下
            data = payload["data"]
            if isinstance(data.get("actions"), list):
                actions = [str(item) for item in data["actions"]]
            elif isinstance(data.get("supported_actions"), list):
                actions = [str(item) for item in data["supported_actions"]]

    return actions


async def check_group_history_supported(bot: Bot) -> bool:
    """检查当前 OneBot 实现是否支持 get_group_msg_history。"""
    try:
        result = await bot.call_api("get_supported_actions")
    except Exception as exc:
        logger.warning(f"[GroupHistorySummary] get_supported_actions 调用失败: {exc}")
        return True

    actions = _extract_supported_actions(result)
    if not actions:
        return True
    return "get_group_msg_history" in set(actions)


def _extract_message_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("messages"), list):
            return [
                item for item in payload["messages"] if isinstance(item, dict)
            ]

        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return [item for item in data["messages"] if isinstance(item, dict)]

    return []


CQ_CODE_PATTERN = re.compile(r"\[CQ:[^\]]+\]")
# 兼容从 raw_message 的 CQ 码中提取 reply 目标 ID
CQ_REPLY_PATTERN = re.compile(r"\[CQ:reply,[^\]]*id=([^,\]]+)")


def _normalize_message_id(value: Any) -> str | None:
    """统一 message_id/reply_id 为可比较的字符串。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _extract_reply_to_message_id(item: dict[str, Any]) -> str | None:
    """提取当前消息 reply 的目标 message_id。

    提取顺序：
    1. message 段中的 reply.data.id（结构化字段，优先）
    2. raw_message 中的 CQ reply 码（兼容兜底）
    """
    message_segments = item.get("message")
    if isinstance(message_segments, list):
        for seg in message_segments:
            if not isinstance(seg, dict):
                continue
            if str(seg.get("type", "")) != "reply":
                continue

            seg_data = seg.get("data", {})
            if isinstance(seg_data, dict):
                reply_id = _normalize_message_id(seg_data.get("id"))
                if reply_id:
                    return reply_id

    raw_message = item.get("raw_message")
    if isinstance(raw_message, str):
        match = CQ_REPLY_PATTERN.search(raw_message)
        if match:
            return _normalize_message_id(match.group(1))

    return None


def _extract_content(item: dict[str, Any]) -> str | None:
    """仅提取文本内容，非文本消息返回 None。"""
    message_segments = item.get("message")
    if isinstance(message_segments, list):
        parts: list[str] = []
        for seg in message_segments:
            if not isinstance(seg, dict):
                continue
            if str(seg.get("type", "")) != "text":
                continue

            seg_data = seg.get("data", {})
            text = ""
            if isinstance(seg_data, dict):
                text = str(seg_data.get("text", "")).strip()
            if not text:
                text = str(seg.get("content", "")).strip()
            if text:
                parts.append(text)

        merged = "".join(parts).strip()
        if merged:
            return merged

    raw_message = item.get("raw_message")
    if isinstance(raw_message, str):
        plain_text = CQ_CODE_PATTERN.sub("", raw_message).strip()
        if plain_text:
            return plain_text

    return None


async def fetch_group_history_messages(
    bot: "Bot",
    group_id: str,
    count: int,
    batch_size: int,
    name_resolver: Any,
) -> list[HistoryMessage]:
    """拉取群最近 N 条历史消息。"""
    target_count = max(1, count)
    current_seq = 0
    seen_keys: set[tuple[int, str, int]] = set()
    collected: list[HistoryMessage] = []

    max_rounds = (target_count // max(1, batch_size)) + 8

    for _ in range(max_rounds):
        try:
            result = await bot.call_api(
                "get_group_msg_history",
                group_id=int(group_id),
                message_seq=current_seq,
                count=min(batch_size, 200),
                reverseOrder=True,
            )
        except Exception as exc:
            logger.warning(f"[GroupHistorySummary] 拉取群历史失败: group={group_id}, {exc}")
            break

        items = _extract_message_items(result)
        if not items:
            break

        min_seq: int | None = None
        fetched_item_count = 0

        for item in items:
            user_id = str(item.get("user_id", "unknown"))
            timestamp = _to_int(item.get("time"))
            message_seq = _to_int(item.get("message_seq"))
            min_seq = message_seq if min_seq is None else min(min_seq, message_seq)
            # 为后续“命令消息 + 机器人 reply 命令”的精准过滤保留关联字段
            message_id = _normalize_message_id(item.get("message_id"))
            reply_to_message_id = _extract_reply_to_message_id(item)

            unique_key = (message_seq, user_id, timestamp)
            if unique_key in seen_keys:
                continue
            seen_keys.add(unique_key)
            fetched_item_count += 1

            fallback_nickname = user_id
            sender = item.get("sender")
            if isinstance(sender, dict):
                fallback_nickname = (
                    str(sender.get("nickname", "")).strip() or fallback_nickname
                )
            nickname = name_resolver(user_id, fallback_nickname)

            content = _extract_content(item)
            if not content:
                continue

            collected.append(
                HistoryMessage(
                    user_id=user_id,
                    nickname=nickname,
                    content=content,
                    timestamp=timestamp,
                    message_seq=message_seq,
                    message_id=message_id,
                    reply_to_message_id=reply_to_message_id,
                )
            )

        if len(collected) >= target_count:
            break

        if fetched_item_count == 0 or min_seq is None or min_seq <= 1:
            break

        next_seq = min_seq - 1
        if next_seq == current_seq:
            break
        current_seq = next_seq

    ordered = sorted(collected, key=lambda msg: (msg.timestamp, msg.message_seq))
    return ordered[-target_count:]


def format_message_for_prompt(message: HistoryMessage) -> str:
    """将历史消息格式化为 LLM 输入文本。"""
    dt = datetime.fromtimestamp(message.timestamp, tz=UTC).astimezone().strftime(
        "%m-%d %H:%M"
    )
    return f"[{dt}] {message.nickname}({message.user_id}): {message.content}"
