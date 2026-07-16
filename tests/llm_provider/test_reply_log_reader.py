"""ReplyLogReader 测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import Any

import pytest

from komari_bot.plugins.llm_provider.reply_log_reader import ReplyLogReader


def _write_jsonl(path: Path, records: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(records) + "\n", encoding="utf-8")


def test_list_logs_defaults_to_recent_seven_days_and_skips_bad_lines(
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
    full_input = {
        "trace_id": "chat-1",
        "phase": "reply",
        "prompt": "完整 prompt",
        "messages": [{"role": "user", "content": "完整消息"}],
    }
    _write_jsonl(
        tmp_path / "2026-04-10.jsonl",
        [
            json.dumps(
                {
                    "timestamp": "2026-04-10T11:00:00+00:00",
                    "method": "generate_text_with_messages",
                    "model": "deepseek-chat",
                    "input": full_input,
                    "output": "<content>你好</content>",
                    "reasoning_content": "完整思考内容",
                },
                ensure_ascii=False,
            )
        ],
    )
    tmp_path.chmod(0o700)
    reader = ReplyLogReader(log_dir=tmp_path)

    detail = asyncio.run(reader.get_log(date="2026-04-10", line_number=1))
    missing = asyncio.run(reader.get_log(date="2026-04-10", line_number=99))

    assert detail is not None
    serialized_detail = json.dumps(detail, ensure_ascii=False)
    assert detail["schema_version"] == 2
    assert detail["input_summary"]["payload_fingerprint"]["chars"] > 0
    assert detail["output_summary"]["chars"] == len("<content>你好</content>")
    assert detail["reasoning_chars"] == len("完整思考内容")
    assert "完整 prompt" not in serialized_detail
    assert "完整消息" not in serialized_detail
    assert "<content>你好</content>" not in serialized_detail
    assert "完整思考内容" not in serialized_detail
    assert "reasoning_content" not in detail
    assert missing is None
    with pytest.raises(ValueError):
        asyncio.run(reader.list_logs(date="20260410"))


def test_list_logs_uses_index_to_parse_only_requested_page(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    records = [
        json.dumps(
            {
                "timestamp": f"2026-04-10T11:{minute:02d}:00+00:00",
                "method": "generate_text",
                "model": "deepseek-chat",
                "input": {"trace_id": f"trace-{minute}"},
                "output": "回复",
            },
            ensure_ascii=False,
        )
        for minute in range(60)
    ]
    _write_jsonl(tmp_path / "2026-04-10.jsonl", records)
    reader = ReplyLogReader(log_dir=tmp_path)
    parse_calls = 0
    original_parse = reader._parse_json_line

    def _count_parse(**kwargs: Any) -> dict[str, Any] | None:
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(**kwargs)

    monkeypatch.setattr(reader, "_parse_json_line", _count_parse)

    items, total = asyncio.run(reader.list_logs(date="2026-04-10", limit=3, offset=10))

    assert total == 60
    assert len(items) == 3
    assert parse_calls == 3
    assert (tmp_path / ".reply-log-index.sqlite3").exists()


def test_log_index_incrementally_discovers_appended_records(tmp_path: Path) -> None:
    log_file = tmp_path / "2026-04-10.jsonl"
    _write_jsonl(
        log_file,
        [
            json.dumps(
                {
                    "timestamp": "2026-04-10T10:00:00+00:00",
                    "method": "generate_text",
                    "model": "deepseek-chat",
                    "input": {"trace_id": "first"},
                    "output": "第一条",
                },
                ensure_ascii=False,
            )
        ],
    )
    reader = ReplyLogReader(log_dir=tmp_path)
    first_items, first_total = asyncio.run(reader.list_logs(date="2026-04-10"))

    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-04-10T11:00:00+00:00",
                    "method": "generate_text",
                    "model": "deepseek-chat",
                    "input": {"trace_id": "second"},
                    "output": "第二条",
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    second_items, second_total = asyncio.run(reader.list_logs(date="2026-04-10"))

    assert first_total == 1
    assert first_items[0]["trace_id"] == "first"
    assert second_total == 2
    assert [item["trace_id"] for item in second_items] == ["second", "first"]


def test_reader_rebuilds_corrupted_index_from_jsonl(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "2026-04-10.jsonl",
        [
            json.dumps(
                {
                    "timestamp": "2026-04-10T11:00:00+00:00",
                    "method": "generate_text",
                    "model": "deepseek-chat",
                    "input": {"trace_id": "recovered"},
                    "output": "回复",
                },
                ensure_ascii=False,
            )
        ],
    )
    (tmp_path / ".reply-log-index.sqlite3").write_bytes(b"not-a-sqlite-database")
    reader = ReplyLogReader(log_dir=tmp_path)

    items, total = asyncio.run(reader.list_logs(date="2026-04-10"))

    assert total == 1
    assert items[0]["trace_id"] == "recovered"
