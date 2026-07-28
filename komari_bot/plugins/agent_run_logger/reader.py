"""Agent Run 日志查询服务。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from .repository import AgentRunIndexEntry, IndexUnavailableError, repository
from .sanitizer import build_preview
from .storage import (
    LOG_DIR,
    read_indexed_record_sync,
    scan_log_files_sync,
    storage,
)


def _entry_matches(
    entry: AgentRunIndexEntry,
    *,
    date_from: date | None,
    date_to: date | None,
    run_type: str | None,
    task_kind: str | None,
    origin: str | None,
    trace_id: str | None,
    status: str | None,
    model: str | None,
    method: str | None,
) -> bool:
    return not (
        (date_from is not None and entry.log_date < date_from)
        or (date_to is not None and entry.log_date > date_to)
        or (run_type is not None and entry.run_type != run_type)
        or (task_kind is not None and entry.task_kind != task_kind)
        or (origin is not None and entry.origin != origin)
        or (trace_id is not None and entry.trace_id != trace_id)
        or (status is not None and entry.status != status)
        or (model is not None and model not in entry.models)
        or (method is not None and method not in entry.methods)
    )


class AgentRunLogReader:
    """优先使用 PG 定位，故障时自动降级扫描 JSONL。"""

    def __init__(
        self,
        *,
        log_dir: Path = LOG_DIR,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._log_dir = log_dir
        self._now_factory = now_factory or (lambda: datetime.now().astimezone())

    @staticmethod
    def _parse_date(value: str) -> date:
        return date.fromisoformat(value)

    def _resolve_dates(
        self,
        *,
        log_date: str | None,
        days: int,
    ) -> tuple[date, date]:
        if log_date is not None:
            parsed = self._parse_date(log_date)
            return parsed, parsed
        today = self._now_factory().astimezone().date()
        return today - timedelta(days=max(1, days) - 1), today

    async def _fallback_entries(
        self,
        *,
        date_from: date,
        date_to: date,
        run_type: str | None,
        task_kind: str | None,
        origin: str | None,
        trace_id: str | None,
        status: str | None,
        model: str | None,
        method: str | None,
    ) -> list[AgentRunIndexEntry]:
        entries = await asyncio.to_thread(
            scan_log_files_sync,
            self._log_dir,
            retained_from=date_from,
        )
        filtered = [
            entry
            for entry in entries
            if _entry_matches(
                entry,
                date_from=date_from,
                date_to=date_to,
                run_type=run_type,
                task_kind=task_kind,
                origin=origin,
                trace_id=trace_id,
                status=status,
                model=model,
                method=method,
            )
        ]
        filtered.sort(key=lambda item: (item.started_at, item.run_id), reverse=True)
        return filtered

    async def list_runs(
        self,
        *,
        log_date: str | None = None,
        days: int = 7,
        run_type: str | None = None,
        task_kind: str | None = None,
        origin: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        model: str | None = None,
        method: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页列出任务；仅为当前页读取 JSONL 以生成正文预览。"""
        await storage.flush()
        date_from, date_to = self._resolve_dates(log_date=log_date, days=days)
        try:
            entries, total = await repository.list_entries(
                date_from=date_from,
                date_to=date_to,
                run_type=run_type,
                task_kind=task_kind,
                origin=origin,
                trace_id=trace_id,
                status=status,
                model=model,
                method=method,
                limit=limit,
                offset=offset,
            )
        except IndexUnavailableError:
            matched = await self._fallback_entries(
                date_from=date_from,
                date_to=date_to,
                run_type=run_type,
                task_kind=task_kind,
                origin=origin,
                trace_id=trace_id,
                status=status,
                model=model,
                method=method,
            )
            total = len(matched)
            entries = matched[offset : offset + limit]

        records = await asyncio.gather(
            *(
                asyncio.to_thread(read_indexed_record_sync, self._log_dir, entry)
                for entry in entries
            )
        )
        items = [
            self._build_list_item(entry, record)
            for entry, record in zip(entries, records, strict=True)
            if record is not None
        ]
        return items, total

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """按 run ID 返回完整 JSONL v3 记录。"""
        await storage.flush()
        entry: AgentRunIndexEntry | None = None
        with suppress(IndexUnavailableError):
            entry = await repository.get(run_id)
        if entry is not None:
            record = await asyncio.to_thread(
                read_indexed_record_sync,
                self._log_dir,
                entry,
            )
            if record is not None:
                return record

        entries = await asyncio.to_thread(scan_log_files_sync, self._log_dir)
        fallback = next((item for item in entries if item.run_id == run_id), None)
        if fallback is None:
            return None
        return await asyncio.to_thread(
            read_indexed_record_sync,
            self._log_dir,
            fallback,
        )

    async def get_legacy_line(
        self,
        *,
        log_date: str,
        line_number: int,
    ) -> dict[str, Any] | None:
        """旧 URL 的日期/物理行号兼容定位。"""
        parsed = self._parse_date(log_date)
        entries = await asyncio.to_thread(
            scan_log_files_sync,
            self._log_dir,
            retained_from=parsed,
        )
        dated = sorted(
            (entry for entry in entries if entry.log_date == parsed),
            key=lambda item: item.byte_offset,
        )
        if line_number > len(dated):
            return None
        return await asyncio.to_thread(
            read_indexed_record_sync,
            self._log_dir,
            dated[line_number - 1],
        )

    @staticmethod
    def _build_list_item(
        entry: AgentRunIndexEntry,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "run_id": entry.run_id,
            "trace_id": entry.trace_id,
            "date": entry.log_date.isoformat(),
            "run_type": entry.run_type,
            "task_kind": entry.task_kind,
            "origin": entry.origin,
            "status": entry.status,
            "started_at": entry.started_at.isoformat(),
            "finished_at": entry.finished_at.isoformat(),
            "duration_ms": record.get("duration_ms"),
            "models": entry.models,
            "methods": entry.methods,
            "round_count": entry.round_count,
            "tool_count": entry.tool_count,
            "usage": record.get("usage", {}),
            "input_preview": build_preview(record.get("input")),
            "output_preview": build_preview(record.get("output")),
            "error_preview": build_preview(record.get("error")),
        }


reader = AgentRunLogReader()


__all__ = ["AgentRunLogReader", "reader"]
