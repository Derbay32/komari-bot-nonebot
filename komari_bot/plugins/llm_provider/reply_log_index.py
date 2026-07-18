"""LLM JSONL 日志的轻量 SQLite 分页索引。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from komari_bot.common.llm_log_safety import sanitize_persisted_log_record

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class LogReference:
    """一条 JSONL 记录在文件中的定位信息。"""

    date: str
    line_number: int
    byte_offset: int
    byte_length: int


class ReplyLogIndex:
    """仅保存查询字段与字节偏移，不复制日志正文或完整记录。"""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._database_path = log_dir / ".reply-log-index.sqlite3"

    def sync_files(self, candidates: list[tuple[str, Path]]) -> None:
        """按文件增量同步索引；首次遇到历史日志时只扫描一次。"""
        if not candidates:
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            self._ensure_schema(connection)
            for date, log_file in candidates:
                if log_file.exists():
                    self._sync_file(connection, date=date, log_file=log_file)

    def reset(self) -> None:
        """删除可重建的索引文件，供损坏恢复使用。"""
        for suffix in ("", "-wal", "-shm"):
            index_file = self._database_path.with_name(
                f"{self._database_path.name}{suffix}"
            )
            index_file.unlink(missing_ok=True)

    def list_references(
        self,
        *,
        dates: list[str],
        trace_id: str | None,
        model: str | None,
        method: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[LogReference], int]:
        """直接在索引中筛选、排序和分页。"""
        if not dates or not self._database_path.exists():
            return [], 0

        placeholders = ", ".join("?" for _ in dates)
        clauses = [f"date IN ({placeholders})"]
        parameters: list[str | int] = [*dates]
        for column, value in (
            ("trace_id", trace_id),
            ("model", model),
            ("method", method),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where_clause = " AND ".join(clauses)

        with self._connection() as connection:
            self._ensure_schema(connection)
            total_row = connection.execute(
                f"SELECT COUNT(*) FROM log_entries WHERE {where_clause}",
                parameters,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT date, line_number, byte_offset, byte_length
                FROM log_entries
                WHERE {where_clause}
                ORDER BY timestamp DESC, date DESC, line_number DESC
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()

        references = [
            LogReference(
                date=str(row[0]),
                line_number=int(row[1]),
                byte_offset=int(row[2]),
                byte_length=int(row[3]),
            )
            for row in rows
        ]
        return references, int(total_row[0]) if total_row else 0

    def get_reference(self, *, date: str, line_number: int) -> LogReference | None:
        """按兼容的日期与行号主键定位记录。"""
        if not self._database_path.exists():
            return None
        with self._connection() as connection:
            self._ensure_schema(connection)
            row = connection.execute(
                """
                SELECT byte_offset, byte_length
                FROM log_entries
                WHERE date = ? AND line_number = ?
                """,
                (date, line_number),
            ).fetchone()
        if row is None:
            return None
        return LogReference(
            date=date,
            line_number=line_number,
            byte_offset=int(row[0]),
            byte_length=int(row[1]),
        )

    def delete_dates(self, dates: list[str]) -> None:
        """日志轮转删除文件时同步清理索引元数据。"""
        if not dates or not self._database_path.exists():
            return
        placeholders = ", ".join("?" for _ in dates)
        with self._connection() as connection:
            self._ensure_schema(connection)
            connection.execute(
                f"DELETE FROM log_entries WHERE date IN ({placeholders})",
                dates,
            )
            connection.execute(
                f"DELETE FROM log_files WHERE date IN ({placeholders})",
                dates,
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            if self._database_path.exists():
                self._database_path.chmod(0o600)
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS log_files (
                date TEXT PRIMARY KEY,
                inode INTEGER NOT NULL,
                file_size INTEGER NOT NULL,
                indexed_offset INTEGER NOT NULL,
                line_count INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS log_entries (
                date TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                byte_length INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                method TEXT NOT NULL,
                model TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (date, line_number)
            );

            CREATE INDEX IF NOT EXISTS idx_log_entries_page
            ON log_entries (date, timestamp DESC, line_number DESC);
            CREATE INDEX IF NOT EXISTS idx_log_entries_trace
            ON log_entries (trace_id, date);
            CREATE INDEX IF NOT EXISTS idx_log_entries_model
            ON log_entries (model, date);
            CREATE INDEX IF NOT EXISTS idx_log_entries_method
            ON log_entries (method, date);
            CREATE INDEX IF NOT EXISTS idx_log_entries_status
            ON log_entries (status, date);
            """
        )

    def _sync_file(
        self,
        connection: sqlite3.Connection,
        *,
        date: str,
        log_file: Path,
    ) -> None:
        stat = log_file.stat()
        state = connection.execute(
            """
            SELECT inode, file_size, indexed_offset, line_count, mtime_ns
            FROM log_files
            WHERE date = ?
            """,
            (date,),
        ).fetchone()

        if state is not None and self._is_fully_indexed(state, stat):
            return

        if state is None or self._requires_rebuild(state, stat):
            connection.execute("DELETE FROM log_entries WHERE date = ?", (date,))
            indexed_offset = 0
            line_count = 0
        else:
            indexed_offset = int(state[2])
            line_count = int(state[3])

        with log_file.open("rb") as handle:
            handle.seek(indexed_offset)
            while raw_line := handle.readline():
                if not raw_line.endswith(b"\n"):
                    break
                byte_offset = indexed_offset
                byte_length = len(raw_line)
                indexed_offset += byte_length
                line_count += 1
                record = self._parse_record(raw_line)
                if record is None:
                    continue
                connection.execute(
                    """
                    INSERT OR REPLACE INTO log_entries (
                        date, line_number, byte_offset, byte_length,
                        timestamp, method, model, trace_id, phase, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date,
                        line_count,
                        byte_offset,
                        byte_length,
                        str(record.get("timestamp", "")),
                        str(record.get("method", "")),
                        str(record.get("model", "")),
                        str(record.get("trace_id", "")),
                        str(record.get("phase", "")),
                        str(record.get("status", "success")),
                    ),
                )

        final_stat = log_file.stat()
        connection.execute(
            """
            INSERT OR REPLACE INTO log_files (
                date, inode, file_size, indexed_offset, line_count, mtime_ns
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                date,
                final_stat.st_ino,
                final_stat.st_size,
                indexed_offset,
                line_count,
                final_stat.st_mtime_ns,
            ),
        )

    @staticmethod
    def _is_fully_indexed(state: tuple[Any, ...], stat: Any) -> bool:
        return (
            int(state[0]) == stat.st_ino
            and int(state[1]) == stat.st_size
            and int(state[2]) == stat.st_size
            and int(state[4]) == stat.st_mtime_ns
        )

    @staticmethod
    def _requires_rebuild(state: tuple[Any, ...], stat: Any) -> bool:
        inode = int(state[0])
        file_size = int(state[1])
        indexed_offset = int(state[2])
        mtime_ns = int(state[4])
        return (
            inode != stat.st_ino
            or stat.st_size < indexed_offset
            or (stat.st_size == file_size and mtime_ns != stat.st_mtime_ns)
        )

    @staticmethod
    def _parse_record(raw_line: bytes) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        return sanitize_persisted_log_record(parsed)
