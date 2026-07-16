"""LLM Provider reply 日志读取器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable  # noqa: TC003
from datetime import datetime, timedelta
from pathlib import Path  # noqa: TC003
from typing import Any

from nonebot import logger

from komari_bot.common.llm_log_safety import sanitize_persisted_log_record

from .llm_logger import _LOG_DIR


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
        """分页扫描 reply 日志摘要。"""
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
        items: list[dict[str, Any]] = []
        for log_date, log_file in candidates:
            if not log_file.exists():
                continue
            with log_file.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    entry = self._parse_log_line(
                        date=log_date,
                        line_number=line_number,
                        line=line,
                    )
                    if entry is None:
                        continue
                    if trace_id and entry.get("trace_id") != trace_id:
                        continue
                    if model and entry.get("model") != model:
                        continue
                    if method and entry.get("method") != method:
                        continue
                    if status and entry.get("status") != status:
                        continue
                    items.append(entry)

        items.sort(
            key=lambda item: (
                str(item.get("timestamp", "")),
                str(item.get("date", "")),
                int(item.get("line_number", 0)),
            ),
            reverse=True,
        )
        total = len(items)
        return items[offset : offset + limit], total

    def _get_log_sync(
        self,
        *,
        date: str,
        line_number: int,
    ) -> dict[str, Any] | None:
        log_file = self._resolve_log_file(date)
        if not log_file.exists():
            return None

        with log_file.open(encoding="utf-8") as handle:
            for current_line_number, line in enumerate(handle, start=1):
                if current_line_number != line_number:
                    continue
                raw_record = self._parse_json_line(
                    date=date,
                    line_number=current_line_number,
                    line=line,
                )
                if raw_record is None:
                    return None
                return self._build_summary_entry(
                    date=date,
                    line_number=current_line_number,
                    record=raw_record,
                )
        return None

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

    def _parse_log_line(
        self,
        *,
        date: str,
        line_number: int,
        line: str,
    ) -> dict[str, Any] | None:
        record = self._parse_json_line(date=date, line_number=line_number, line=line)
        if record is None:
            return None
        return self._build_summary_entry(
            date=date,
            line_number=line_number,
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
