"""TSK-206 总结结果的纯渲染投影（无模型、无 I/O）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .history_service import HistoryFetchMetadata

DEFAULT_SUMMARY_TEXT = "本次聊天记录信息较少，暂无可提炼的有效总结。"


def summary_text_to_lines(summary_text: str) -> list[str]:
    """将总结正文转换为图片渲染行（保留既有空行折叠规则）。"""
    lines = [line.strip() for line in summary_text.splitlines()]
    normalized = [line for line in lines if line]
    return normalized or [DEFAULT_SUMMARY_TEXT]


def format_time_range(start_ts: int, end_ts: int) -> str:
    """按既有本地时区格式投影历史时间范围。"""
    start_str = (
        datetime.fromtimestamp(start_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    )
    end_str = datetime.fromtimestamp(end_ts, tz=UTC).astimezone().strftime("%m-%d %H:%M")
    return f"{start_str} - {end_str}"


def format_partial_history_notice(metadata: HistoryFetchMetadata | None) -> str:
    """保留允许的部分历史缺页提示。"""
    if metadata is None or metadata.status != "partial":
        return ""
    failed_batch = metadata.failed_batch or metadata.completed_batches + 1
    missing_suffix = (
        f"，约缺 {metadata.missing_count} 条记录" if metadata.missing_count > 0 else ""
    )
    return (
        f"⚠ 历史第 {failed_batch} 批读取失败{missing_suffix}；"
        f"以下基于已取回的 {metadata.retrieved_item_count} 条记录。"
    )


__all__ = [
    "DEFAULT_SUMMARY_TEXT",
    "format_partial_history_notice",
    "format_time_range",
    "summary_text_to_lines",
]
