"""TSK-206 纯渲染投影测试。"""

from __future__ import annotations

from komari_bot.plugins.group_history_summary.history_service import (
    HistoryFetchMetadata,
)
from komari_bot.plugins.group_history_summary.rendering import (
    DEFAULT_SUMMARY_TEXT,
    format_partial_history_notice,
    format_time_range,
    summary_text_to_lines,
)


def test_summary_text_to_lines_strips_and_drops_empty_lines() -> None:
    assert summary_text_to_lines("  第一行 \n\n 第二行  \n") == ["第一行", "第二行"]
    assert summary_text_to_lines(" \n\t\n") == [DEFAULT_SUMMARY_TEXT]


def test_format_time_range_uses_local_minute_precision() -> None:
    import re

    formatted = format_time_range(0, 3600)
    assert re.fullmatch(
        r"\d{2}-\d{2} \d{2}:\d{2} - \d{2}-\d{2} \d{2}:\d{2}",
        formatted,
    )


def test_partial_notice_preserves_failed_batch_and_missing_count() -> None:
    metadata = HistoryFetchMetadata(
        status="partial",
        requested_count=50,
        retrieved_item_count=40,
        missing_count=10,
        completed_batches=2,
        failed_batch=3,
        failure_code="history_api_error",
    )

    assert format_partial_history_notice(metadata) == (
        "⚠ 历史第 3 批读取失败，约缺 10 条记录；以下基于已取回的 40 条记录。"
    )
    assert format_partial_history_notice(None) == ""
    assert (
        format_partial_history_notice(
            HistoryFetchMetadata(
                status="complete",
                requested_count=10,
                retrieved_item_count=10,
                missing_count=0,
                completed_batches=1,
            )
        )
        == ""
    )
