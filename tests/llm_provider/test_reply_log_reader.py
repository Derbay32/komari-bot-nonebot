"""ReplyLogReader 测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003

import pytest

from komari_bot.plugins.llm_provider.reply_log_reader import ReplyLogReader


def _write_jsonl(path: Path, records: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")


def test_list_logs_defaults_to_recent_seven_days_and_skips_bad_lines(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "2026-04-10.jsonl",
        [
            json.dumps(
                {
                    "timestamp": "2026-04-10T11:00:00+00:00",
                    "method": "generate_text_with_messages",
                    "model": "deepseek-chat",
                    "input": {"trace_id": "chat-1", "phase": "reply"},
                    "output": "<content>你好</content>",
                    "duration_ms": 123.4,
                },
                ensure_ascii=False,
            ),
            "{bad json",
        ],
    )
    _write_jsonl(
        tmp_path / "2026-04-09.jsonl",
        [
            json.dumps(
                {
                    "timestamp": "2026-04-09T09:00:00+00:00",
                    "method": "generate_text",
                    "model": "deepseek-reasoner",
                    "input": {"trace_id": "chat-2", "phase": "reply"},
                    "error": "boom",
                    "duration_ms": 88.0,
                },
                ensure_ascii=False,
            )
        ],
    )
    _write_jsonl(
        tmp_path / "2026-04-01.jsonl",
        [
            json.dumps(
                {
                    "timestamp": "2026-04-01T09:00:00+00:00",
                    "method": "generate_text",
                    "model": "deepseek-chat",
                    "input": {"trace_id": "old"},
                    "output": "旧日志",
                },
                ensure_ascii=False,
            )
        ],
    )

    reader = ReplyLogReader(
        log_dir=tmp_path,
        now_factory=lambda: datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
    )

    items, total = asyncio.run(reader.list_logs())

    assert total == 2
    assert [item["trace_id"] for item in items] == ["chat-1", "chat-2"]
    assert items[0]["status"] == "success"
    assert items[1]["status"] == "error"


def test_list_logs_supports_filters_and_empty_directory(tmp_path: Path) -> None:
    reader = ReplyLogReader(
        log_dir=tmp_path,
        now_factory=lambda: datetime(2026, 4, 10, 12, 0, tzinfo=UTC),
    )

    empty_items, empty_total = asyncio.run(reader.list_logs())
    assert empty_items == []
    assert empty_total == 0

    _write_jsonl(
        tmp_path / "2026-04-10.jsonl",
        [
            json.dumps(
                {
                    "timestamp": "2026-04-10T11:00:00+00:00",
                    "method": "generate_text_with_messages",
                    "model": "deepseek-chat",
                    "input": {"trace_id": "chat-1", "phase": "reply"},
                    "output": "<content>你好</content>",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timestamp": "2026-04-10T10:00:00+00:00",
                    "method": "generate_text",
                    "model": "deepseek-reasoner",
                    "input": {"trace_id": "chat-2", "phase": "summary"},
                    "error": "boom",
                },
                ensure_ascii=False,
            ),
        ],
    )

    items, total = asyncio.run(
        reader.list_logs(
            date="2026-04-10",
            trace_id="chat-2",
            model="deepseek-reasoner",
            method="generate_text",
            status="error",
        )
    )

    assert total == 1
    assert items[0]["trace_id"] == "chat-2"
    assert items[0]["status"] == "error"


def test_get_log_returns_detail_and_handles_invalid_date_and_missing_line(
    tmp_path: Path,
) -> None:
    _write_jsonl(
        tmp_path / "2026-04-10.jsonl",
        [
            json.dumps(
                {
                    "timestamp": "2026-04-10T11:00:00+00:00",
                    "method": "generate_text_with_messages",
                    "model": "deepseek-chat",
                    "input": {"trace_id": "chat-1", "phase": "reply"},
                    "output": "<content>你好</content>",
                },
                ensure_ascii=False,
            )
        ],
    )
    reader = ReplyLogReader(log_dir=tmp_path)

    detail = asyncio.run(reader.get_log(date="2026-04-10", line_number=1))
    missing = asyncio.run(reader.get_log(date="2026-04-10", line_number=99))

    assert detail is not None
    assert detail["output"] == "<content>你好</content>"
    assert missing is None
    with pytest.raises(ValueError):
        asyncio.run(reader.list_logs(date="20260410"))
