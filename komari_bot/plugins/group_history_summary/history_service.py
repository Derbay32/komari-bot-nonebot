"""NapCat 群历史消息拉取服务。"""

from __future__ import annotations

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


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_supported_actions(payload: Any) -> list[str]:
    actions: list[str] = []

    if isinstance(payload, list):
        actions = [str(item) for item in payload]
    elif isinstance(payload, dict):
        if isinstance(payload.get("actions"), list):
            actions = [str(item) for item in payload["actions"]]
        elif isinstance(payload.get("supported_actions"), list):
            actions = [str(item) for item in payload["supported_actions"]]
        elif isinstance(payload.get("data"), list):
            actions = [str(item) for item in payload["data"]]
        elif isinstance(payload.get("data"), dict):
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


def _extract_content(item: dict[str, Any]) -> str:
    raw_message = item.get("raw_message")
    if isinstance(raw_message, str) and raw_message.strip():
        return raw_message.strip()

    message_segments = item.get("message")
    if isinstance(message_segments, list):
        parts: list[str] = []
        for seg in message_segments:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type", ""))
            seg_data = seg.get("data", {})
            if seg_type == "text":
                text = ""
                if isinstance(seg_data, dict):
                    text = str(seg_data.get("text", "")).strip()
                if not text:
                    text = str(seg.get("content", "")).strip()
                if text:
                    parts.append(text)
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "face":
                parts.append("[表情]")
            elif seg_type == "at":
                parts.append("[@]")
            elif seg_type:
                parts.append(f"[{seg_type}]")

        merged = "".join(parts).strip()
        if merged:
            return merged

    return "[无法解析的消息]"


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
        new_item_count = 0

        for item in items:
            user_id = str(item.get("user_id", "unknown"))
            timestamp = _to_int(item.get("time"))
            message_seq = _to_int(item.get("message_seq"))

            unique_key = (message_seq, user_id, timestamp)
            if unique_key in seen_keys:
                continue
            seen_keys.add(unique_key)

            fallback_nickname = user_id
            sender = item.get("sender")
            if isinstance(sender, dict):
                fallback_nickname = (
                    str(sender.get("nickname", "")).strip() or fallback_nickname
                )
            nickname = name_resolver(user_id, fallback_nickname)

            content = _extract_content(item)
            collected.append(
                HistoryMessage(
                    user_id=user_id,
                    nickname=nickname,
                    content=content,
                    timestamp=timestamp,
                    message_seq=message_seq,
                )
            )
            new_item_count += 1

            min_seq = message_seq if min_seq is None else min(min_seq, message_seq)

        if len(collected) >= target_count:
            break

        if new_item_count == 0 or min_seq is None or min_seq <= 1:
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
