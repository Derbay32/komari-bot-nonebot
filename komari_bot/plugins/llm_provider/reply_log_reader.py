"""LLM Provider reply 日志读取器。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable  # noqa: TC003
from datetime import datetime, timedelta
from pathlib import Path  # noqa: TC003
from typing import Any

from nonebot import logger

from komari_bot.common.llm_log_safety import sanitize_persisted_log_record

from .llm_logger import _LOG_DIR, flush_llm_logs
from .reply_log_index import LogReference, ReplyLogIndex


class ReplyLogReader:
    """读取 reply 日志的只读服务。"""

    def __init__(
        self,
        *,
        log_dir: Path | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._log_dir = log_dir or _LOG_DIR
        self._now_factory = now_factory or (lambda: datetime.now().astimezone())
        self._index = ReplyLogIndex(self._log_dir)

    async def list_logs(
        self,
        *,
        date: str | None = None,
        days: int = 7,
        trace_id: str | None = None,
        model: str | None = None,
        method: str | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """通过轻量索引分页读取 reply 日志摘要。"""
        await flush_llm_logs()
        return await asyncio.to_thread(
            self._list_logs_sync,
            date=date,
            days=days,
            trace_id=trace_id,
            model=model,
            method=method,
            status=status,
            limit=limit,
            offset=offset,
        )

    async def get_log(
        self,
        *,
        date: str,
        line_number: int,
    ) -> dict[str, Any] | None:
        """按日期与行号读取脱敏日志详情。"""
        await flush_llm_logs()
        return await asyncio.to_thread(
            self._get_log_sync,
            date=date,
            line_number=line_number,
        )

    def _list_logs_sync(
        self,
        *,
        date: str | None,
        days: int,
        trace_id: str | None,
        model: str | None,
        method: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        candidates = self._resolve_candidate_files(date=date, days=days)
        existing = [item for item in candidates if item[1].exists()]

        def _query_index() -> tuple[list[LogReference], int]:
            self._index.sync_files(existing)
            return self._index.list_references(
                dates=[log_date for log_date, _ in existing],
                trace_id=trace_id,
                model=model,
                method=method,
                status=status,
                limit=limit,
                offset=offset,
            )

        references, total = self._with_index_recovery(_query_index)
        items = [
            entry
            for reference in references
            if (entry := self._read_reference(reference)) is not None
        ]
        return items, total

    def _get_log_sync(
        self,
        *,
        date: str,
        line_number: int,
    ) -> dict[str, Any] | None:
        log_file = self._resolve_log_file(date)
        if not log_file.exists():
            return None

        def _query_index() -> LogReference | None:
            self._index.sync_files([(date, log_file)])
            return self._index.get_reference(date=date, line_number=line_number)

        reference = self._with_index_recovery(_query_index)
        return self._read_reference(reference) if reference is not None else None

    def _with_index_recovery[T](self, operation: Callable[[], T]) -> T:
        try:
            return operation()
        except sqlite3.DatabaseError:
            logger.warning("[LLM Provider] 回复日志索引损坏，正在自动重建")
            self._index.reset()
            return operation()

    def _resolve_candidate_files(
        self,
        *,
        date: str | None,
        days: int,
    ) -> list[tuple[str, Path]]:
        if date is not None:
            parsed = self._parse_date(date)
            return [(parsed.strftime("%Y-%m-%d"), self._resolve_log_file(date))]

        if not self._log_dir.exists():
            return []

        cutoff = (self._now_factory() - timedelta(days=days - 1)).date()
        candidates: list[tuple[str, Path]] = []
        for log_file in sorted(self._log_dir.glob("*.jsonl"), reverse=True):
            try:
                parsed = self._parse_date(log_file.stem)
            except ValueError:
                continue
            if parsed.date() < cutoff:
                continue
            candidates.append((log_file.stem, log_file))
        return candidates

    def _resolve_log_file(self, date: str) -> Path:
        parsed = self._parse_date(date)
        return self._log_dir / f"{parsed.strftime('%Y-%m-%d')}.jsonl"

    def _parse_date(self, value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007

    def _read_reference(self, reference: LogReference) -> dict[str, Any] | None:
        log_file = self._resolve_log_file(reference.date)
        try:
            with log_file.open("rb") as handle:
                handle.seek(reference.byte_offset)
                raw_line = handle.read(reference.byte_length)
            line = raw_line.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning(
                "[LLM Provider] 回复日志定位读取失败: date={} line={}",
                reference.date,
                reference.line_number,
            )
            return None
        record = self._parse_json_line(
            date=reference.date,
            line_number=reference.line_number,
            line=line,
        )
        if record is None:
            return None
        return self._build_summary_entry(
            date=reference.date,
            line_number=reference.line_number,
            record=record,
        )

    def _parse_json_line(
        self,
        *,
        date: str,
        line_number: int,
        line: str,
    ) -> dict[str, Any] | None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "[LLM Provider] 回复日志解析失败: date={} line={}",
                date,
                line_number,
            )
            return None

        if not isinstance(record, dict):
            logger.warning(
                "[LLM Provider] 回复日志格式非法: date={} line={}",
                date,
                line_number,
            )
            return None
        return sanitize_persisted_log_record(record)

    def _build_summary_entry(
        self,
        *,
        date: str,
        line_number: int,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "date": date,
            "line_number": line_number,
            "schema_version": int(record.get("schema_version", 2)),
            "timestamp": str(record.get("timestamp", "")).strip(),
            "method": str(record.get("method", "")).strip(),
            "model": str(record.get("model", "")).strip(),
            "trace_id": str(record.get("trace_id", "")).strip(),
            "phase": str(record.get("phase", "")).strip(),
            "duration_ms": record.get("duration_ms"),
            "status": record.get("status", "success"),
            "finish_reason": record.get("finish_reason"),
            "tool_calls_count": record.get("tool_calls_count"),
            "reasoning_chars": int(record.get("reasoning_chars", 0)),
            "input_summary": record.get("input_summary", {}),
            "output_summary": record.get("output_summary"),
            "error_summary": record.get("error_summary"),
            "usage": record.get("usage"),
        }
