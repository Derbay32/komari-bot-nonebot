"""Agent Run JSONL 写入、保留期清理与 PostgreSQL 对账。"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nonebot import logger

from .repository import AgentRunIndexEntry, repository

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

LOG_DIR = Path("logs") / "agent_run_logger"
LEGACY_LOG_DIR = Path("logs") / "llm_provider"
_QUEUE_MAX_SIZE = 1024
_WRITE_BATCH_SIZE = 32


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _record_to_index_entry(
    record: dict[str, Any],
    *,
    file_name: str,
    byte_offset: int,
    byte_length: int,
) -> AgentRunIndexEntry:
    started_at = _parse_datetime(record["started_at"])
    finished_at = _parse_datetime(record["finished_at"])
    usage = record.get("usage")
    usage_data = usage if isinstance(usage, dict) else {}
    completeness_fields = (
        "input_tokens_complete",
        "cached_input_tokens_complete",
        "cache_miss_input_tokens_complete",
        "output_tokens_complete",
        "reasoning_output_tokens_complete",
        "total_tokens_complete",
    )
    return AgentRunIndexEntry(
        run_id=str(record["run_id"]),
        trace_id=str(record["trace_id"]),
        run_type=str(record["run_type"]),
        task_kind=str(record["task_kind"]),
        origin=str(record["origin"]),
        status=str(record["status"]),
        started_at=started_at,
        finished_at=finished_at,
        log_date=started_at.astimezone().date(),
        file_name=file_name,
        byte_offset=byte_offset,
        byte_length=byte_length,
        models=[str(item) for item in record.get("models", [])],
        methods=[str(item) for item in record.get("methods", [])],
        round_count=len(record.get("rounds", [])),
        tool_count=len(record.get("tool_executions", [])),
        input_tokens=int(usage_data.get("input_tokens", 0)),
        cached_input_tokens=int(usage_data.get("cached_input_tokens", 0)),
        cache_miss_input_tokens=int(
            usage_data.get("cache_miss_input_tokens", 0)
        ),
        output_tokens=int(usage_data.get("output_tokens", 0)),
        reasoning_output_tokens=int(
            usage_data.get("reasoning_output_tokens", 0)
        ),
        total_tokens=int(usage_data.get("total_tokens", 0)),
        usage_complete=all(
            bool(usage_data.get(field, False)) for field in completeness_fields
        ),
    )


def _ensure_log_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _lock_file_path(log_file: Path) -> Path:
    return log_file.with_name(f".{log_file.name}.lock")


@contextmanager
def _locked_log_file(log_file: Path, operation: int) -> Iterator[None]:
    lock_descriptor = os.open(
        _lock_file_path(log_file),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, operation)
        yield
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def _append_file_records(
    log_file: Path,
    records: list[dict[str, Any]],
) -> list[AgentRunIndexEntry]:
    _ensure_log_directory(log_file.parent)
    entries: list[AgentRunIndexEntry] = []
    with _locked_log_file(log_file, fcntl.LOCK_EX):
        descriptor = os.open(
            log_file,
            os.O_RDWR | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a+b", closefd=False) as handle:
                handle.seek(0, os.SEEK_END)
                for record in records:
                    line = (
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=repr,
                        ).encode("utf-8")
                        + b"\n"
                    )
                    byte_offset = handle.tell()
                    handle.write(line)
                    entries.append(
                        _record_to_index_entry(
                            record,
                            file_name=log_file.name,
                            byte_offset=byte_offset,
                            byte_length=len(line),
                        )
                    )
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
    return entries


def _append_batch_sync(
    log_dir: Path,
    records: list[dict[str, Any]],
) -> list[AgentRunIndexEntry]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        log_date = _parse_datetime(record["started_at"]).astimezone().date()
        grouped[log_date.isoformat()].append(record)

    entries: list[AgentRunIndexEntry] = []
    for date_text, dated_records in grouped.items():
        entries.extend(
            _append_file_records(log_dir / f"{date_text}.jsonl", dated_records)
        )
    return entries


def _parse_log_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None


def scan_log_files_sync(
    log_dir: Path,
    *,
    retained_from: date | None = None,
) -> list[AgentRunIndexEntry]:
    """扫描 JSONL 并重建全部物理定位信息。"""
    if not log_dir.exists():
        return []
    entries: list[AgentRunIndexEntry] = []
    for log_file in sorted(log_dir.glob("*.jsonl")):
        log_date = _parse_log_date(log_file)
        if log_date is None or (
            retained_from is not None and log_date < retained_from
        ):
            continue
        try:
            with (
                _locked_log_file(log_file, fcntl.LOCK_SH),
                log_file.open("rb") as handle,
            ):
                while line := handle.readline():
                    byte_offset = handle.tell() - len(line)
                    try:
                        value = json.loads(line)
                        if not isinstance(value, dict):
                            logger.warning(
                                "[AgentRunLogger] 跳过非对象日志行: {}:{}",
                                log_file,
                                byte_offset,
                            )
                            continue
                        entries.append(
                            _record_to_index_entry(
                                value,
                                file_name=log_file.name,
                                byte_offset=byte_offset,
                                byte_length=len(line),
                            )
                        )
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        logger.warning(
                            "[AgentRunLogger] 跳过损坏日志行: {}:{}",
                            log_file,
                            byte_offset,
                        )
        except OSError:
            logger.opt(exception=True).warning(
                "[AgentRunLogger] 扫描日志文件失败: {}", log_file
            )
    return entries


def read_indexed_record_sync(
    log_dir: Path,
    entry: AgentRunIndexEntry,
) -> dict[str, Any] | None:
    """按索引字节区间读取单条 JSONL，不扫描无关正文。"""
    log_file = log_dir / Path(entry.file_name).name
    try:
        with (
            _locked_log_file(log_file, fcntl.LOCK_SH),
            log_file.open("rb") as handle,
        ):
            handle.seek(entry.byte_offset)
            raw = handle.read(entry.byte_length)
        value = json.loads(raw)
        if isinstance(value, dict) and value.get("run_id") == entry.run_id:
            return value
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _cleanup_files_sync(
    log_dir: Path,
    retained_from: date,
) -> int:
    if not log_dir.exists():
        return 0
    removed = 0
    for log_file in log_dir.glob("*.jsonl"):
        log_date = _parse_log_date(log_file)
        if log_date is not None and log_date < retained_from:
            with _locked_log_file(log_file, fcntl.LOCK_EX):
                if log_file.exists():
                    log_file.unlink(missing_ok=True)
                    removed += 1
    return removed


def _remove_legacy_sqlite_sync() -> int:
    removed = 0
    for name in (
        ".reply-log-index.sqlite3",
        ".reply-log-index.sqlite3-wal",
        ".reply-log-index.sqlite3-shm",
    ):
        path = LEGACY_LOG_DIR / name
        if path.exists():
            path.unlink(missing_ok=True)
            removed += 1
    return removed


@dataclass(frozen=True, slots=True)
class _WriteItem:
    record: dict[str, Any]


class AgentRunLogStorage:
    """有界异步 writer 及周期维护服务。"""

    def __init__(self, log_dir: Path = LOG_DIR) -> None:
        self.log_dir = log_dir
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_WriteItem | None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._retention_getter: Callable[[], int] = lambda: 1
        self._dropped_count = 0
        self._closing = False

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def configure(self, retention_getter: Callable[[], int]) -> None:
        self._retention_getter = retention_getter

    async def initialize(self) -> None:
        self._closing = False
        await asyncio.to_thread(_ensure_log_directory, self.log_dir)
        removed = await asyncio.to_thread(_remove_legacy_sqlite_sync)
        if removed:
            logger.info(
                "[AgentRunLogger] 已删除 {} 个废弃 SQLite 索引文件", removed
            )
        await repository.initialize()

    def enqueue(self, record: dict[str, Any]) -> bool:
        if self._closing:
            self._dropped_count += 1
            logger.warning("[AgentRunLogger] 服务正在关闭，丢弃一条完整任务日志")
            return False
        queue = self._ensure_worker()
        try:
            queue.put_nowait(_WriteItem(record=record))
        except asyncio.QueueFull:
            self._dropped_count += 1
            if (
                self._dropped_count == 1
                or self._dropped_count & (self._dropped_count - 1) == 0
            ):
                logger.warning(
                    "[AgentRunLogger] 写入队列已满，累计丢弃 {} 条完整任务日志",
                    self._dropped_count,
                )
            return False
        return True

    def _ensure_worker(self) -> asyncio.Queue[_WriteItem | None]:
        loop = asyncio.get_running_loop()
        if self._loop is loop and self._queue is not None:
            return self._queue
        if self._queue is not None and not self._queue.empty():
            abandoned = self._queue.qsize()
            self._dropped_count += abandoned
            logger.warning(
                "[AgentRunLogger] 事件循环已替换，丢弃 {} 条未落盘任务日志",
                abandoned,
            )
        self._loop = loop
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)
        self._worker = loop.create_task(
            self._run_writer(self._queue),
            name="agent-run-log-writer",
        )
        return self._queue

    async def _run_writer(
        self,
        queue: asyncio.Queue[_WriteItem | None],
    ) -> None:
        while True:
            first = await queue.get()
            if first is None:
                queue.task_done()
                return
            batch = [first]
            stop_after_batch = False
            while len(batch) < _WRITE_BATCH_SIZE:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    queue.task_done()
                    stop_after_batch = True
                    break
                batch.append(item)
            try:
                async with self._write_lock:
                    entries = await asyncio.to_thread(
                        _append_batch_sync,
                        self.log_dir,
                        [item.record for item in batch],
                    )
                await repository.upsert_many(entries)
            except Exception:
                logger.opt(exception=True).warning(
                    "[AgentRunLogger] 完整任务日志批量写入失败"
                )
            finally:
                for _ in batch:
                    queue.task_done()
            if stop_after_batch:
                return

    async def flush(self) -> None:
        if self._queue is None or self._loop is not asyncio.get_running_loop():
            return
        await self._queue.join()

    def retained_from(self, *, now: datetime | None = None) -> date:
        local_now = now or datetime.now().astimezone()
        retention_days = max(1, min(90, int(self._retention_getter())))
        return local_now.astimezone().date() - timedelta(days=retention_days - 1)

    async def cleanup(self, *, now: datetime | None = None) -> int:
        """删除保留窗口之外的新旧 JSONL，并同步轻索引。"""
        await self.flush()
        retained_from = self.retained_from(now=now)
        async with self._write_lock:
            current_removed, legacy_removed = await asyncio.gather(
                asyncio.to_thread(
                    _cleanup_files_sync,
                    self.log_dir,
                    retained_from,
                ),
                asyncio.to_thread(
                    _cleanup_files_sync,
                    LEGACY_LOG_DIR,
                    retained_from,
                ),
            )
        await repository.delete_before(retained_from)
        removed = current_removed + legacy_removed
        if removed:
            logger.info("[AgentRunLogger] 已清理 {} 个过期 JSONL 文件", removed)
        return removed

    async def reconcile(self, *, now: datetime | None = None) -> bool:
        """扫描保留期文件，用 advisory lock 单飞修复 PG 索引。"""
        await self.flush()
        retained_from = self.retained_from(now=now)
        async with self._write_lock:
            entries = await asyncio.to_thread(
                scan_log_files_sync,
                self.log_dir,
                retained_from=retained_from,
            )
        await repository.cleanup_legacy_provider_config()
        return await repository.reconcile(entries, retained_from=retained_from)

    async def maintain(self) -> None:
        await repository.initialize()
        await repository.cleanup_legacy_provider_config()
        await self.cleanup()
        await self.reconcile()

    async def shutdown(self) -> None:
        self._closing = True
        if self._queue is not None and self._loop is asyncio.get_running_loop():
            await self.flush()
            self._queue.put_nowait(None)
            if self._worker is not None:
                await self._worker
        self._queue = None
        self._worker = None
        self._loop = None
        await repository.close()


storage = AgentRunLogStorage()


__all__ = [
    "LEGACY_LOG_DIR",
    "LOG_DIR",
    "AgentRunLogStorage",
    "read_indexed_record_sync",
    "scan_log_files_sync",
    "storage",
]
