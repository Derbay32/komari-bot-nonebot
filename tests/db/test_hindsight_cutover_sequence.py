"""TSK-198 跨切片验收：旧运行时退役前必须能离线导出正式记忆源表。

本用例在一次性 PostgreSQL 隔离库内按真实发布顺序执行：

1. 升级到历史 revision ``0029``（退役迁移前的数据库结构）；
2. 在三张正式来源表（摘要、非空 traits 画像、互动历史）各写入一条带真实
   provenance/时间戳的金丝雀，并在升级前用**实际** CLI 导出（全新子进程、
   真实 DB 边界、无 exporter mock）；
3. 断言退出码、逐表计数、三条记录、金丝雀字段/provenance 与 manifest
   私有权限，并保存 manifest 字节与全部源行；
4. 升级到 ``0030``/head 并做 ORM 零漂移检查；
5. 用同一 source id 再次导出到另一临时 manifest，要求与升级前导出逐字节
   一致、源行完全不变。

这证明旧运行时/Prompt 退役不会移除最小离线读取器，且导出确实发生在
升级之前，而不是只在退役后补做导出。导入语义由已接受的 import
receipt/completion/source-equality 用例覆盖，本文件不重复实现。

隔离纪律：用例在门控 DSN 派生的一次性隔离库（唯一后缀）内执行，结束即
DROP；共享门控库始终保持 head。无 ``KOMARI_TEST_POSTGRES_URL`` 时 skip，
不硬编码凭据，也不打印 DSN。
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import asyncpg
import pytest

from tests.db.tsk197_gate_support import (
    HEAD_REVISION,
    PROJECT_ROOT,
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [SKIP_NO_POSTGRES, pytest.mark.asyncio]

HISTORICAL_REVISION = "0029"
SOURCE_ID = "tsk198-cutover-canary"
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "migrate_memory_to_hindsight.py"

CONVERSATIONS = "komari_memory_conversations"
PROFILES = "komari_memory_user_profile"
INTERACTIONS = "komari_memory_interaction_history"

GROUP_ID = "tsk198-canary-group"
USER_ID = "tsk198-user"
CONVERSATION_ID = 198001
INTERACTION_ID = 198002

SUMMARY = "TSK-198 离线导出金丝雀：升级前摘要"
EVENT_SUMMARY = "TSK-198 离线导出金丝雀：升级前互动"
TRAITS: dict[str, Any] = {
    "identity": "长期身份",
    "preferences": ["咖啡", "旧书"],
    "confidence": 0.9,
}

CONVERSATION_START = datetime.fromisoformat("2026-10-26 01:02:03.456789")
CONVERSATION_END = datetime.fromisoformat("2026-10-26 01:32:03.456789")
CONVERSATION_LAST_ACCESSED = datetime.fromisoformat("2026-10-27 04:05:06.789012")
CONVERSATION_CREATED_AT = datetime.fromisoformat("2026-10-26 01:33:03.456789")
PROFILE_UPDATED_AT = datetime(2026, 10, 26, 2, 3, 4, 567890, tzinfo=UTC)
PROFILE_LAST_ACCESSED = datetime.fromisoformat("2026-10-27 05:06:07.890123")
INTERACTION_FIRST_SEEN_AT = datetime(2026, 10, 26, 3, 4, 5, 678901, tzinfo=UTC)
INTERACTION_LAST_SEEN_AT = datetime(2026, 10, 26, 3, 5, 5, 678901, tzinfo=UTC)
INTERACTION_LAST_ACCESSED = datetime(2026, 10, 27, 6, 7, 8, 901234, tzinfo=UTC)
INTERACTION_CREATED_AT = datetime(2026, 10, 26, 3, 6, 5, 678901, tzinfo=UTC)

SOURCE_TABLES = (CONVERSATIONS, PROFILES, INTERACTIONS)
SOURCE_ORDER = {
    CONVERSATIONS: "id",
    PROFILES: "user_id, group_id",
    INTERACTIONS: "id",
}


def _manifest_timestamp(value: datetime) -> str:
    """把插入的 TIMESTAMP 值转成 manifest 的 ISO 文本。"""
    return value.isoformat()


async def _alembic_version(connection: asyncpg.Connection) -> str:
    version = await connection.fetchval("SELECT version_num FROM alembic_version")
    assert isinstance(version, str)
    return version


async def _insert_canaries(connection: asyncpg.Connection) -> None:
    """每条正式来源表一条带真实 provenance 与时间戳的金丝雀。"""
    await connection.execute(
        """
        INSERT INTO komari_memory_conversations (
            id, group_id, summary, participants, dedup_key,
            start_time, end_time, importance, importance_initial,
            importance_current, last_accessed, is_fuzzy, created_at
        )
        VALUES (
            $1, $2, $3, ARRAY['canary-user', 'canary-bot']::text[], $4,
            $5::timestamp, $6::timestamp, $7, $8, $9, $10::timestamp,
            TRUE, $11::timestamp
        )
        """,
        CONVERSATION_ID,
        GROUP_ID,
        SUMMARY,
        "tsk198-conv-dedup",
        CONVERSATION_START,
        CONVERSATION_END,
        5,
        2,
        4,
        CONVERSATION_LAST_ACCESSED,
        CONVERSATION_CREATED_AT,
    )
    await connection.execute(
        """
        INSERT INTO komari_memory_user_profile (
            user_id, group_id, version, display_name, traits, updated_at,
            importance, access_count, last_accessed
        )
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9::timestamp)
        """,
        USER_ID,
        GROUP_ID,
        7,
        "TSK-198 用户",
        json.dumps(TRAITS, ensure_ascii=False, sort_keys=True),
        PROFILE_UPDATED_AT,
        5,
        17,
        PROFILE_LAST_ACCESSED,
    )
    await connection.execute(
        """
        INSERT INTO komari_memory_interaction_history (
            id, user_id, display_name, event_summary, source_dedup_key,
            source_message_count, first_seen_at, last_seen_at, importance,
            importance_initial, importance_current, last_accessed, is_fuzzy,
            created_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, TRUE, $13
        )
        """,
        INTERACTION_ID,
        USER_ID,
        "TSK-198 用户",
        EVENT_SUMMARY,
        "tsk198-int-dedup",
        7,
        INTERACTION_FIRST_SEEN_AT,
        INTERACTION_LAST_SEEN_AT,
        5,
        3,
        4,
        INTERACTION_LAST_ACCESSED,
        INTERACTION_CREATED_AT,
    )


async def _fetch_source_rows(
    connection: asyncpg.Connection,
) -> dict[str, list[dict[str, Any]]]:
    """读取三张正式来源表的完整行快照（含未导出的内部列）。"""
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for table in SOURCE_TABLES:
        rows = await connection.fetch(
            f"SELECT * FROM {table} ORDER BY {SOURCE_ORDER[table]}"
        )
        snapshot[table] = [dict(row) for row in rows]
    return snapshot


def _run_export(db_url: str, output_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = db_url
    env["LOG_LEVEL"] = "INFO"
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "export",
            "--source-id",
            SOURCE_ID,
            "--output",
            str(output_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _expected_fields() -> dict[str, dict[str, Any]]:
    return {
        CONVERSATIONS: {
            "id": CONVERSATION_ID,
            "group_id": GROUP_ID,
            "summary": SUMMARY,
            "participants": ["canary-user", "canary-bot"],
            "dedup_key": "tsk198-conv-dedup",
            "start_time": _manifest_timestamp(CONVERSATION_START),
            "end_time": _manifest_timestamp(CONVERSATION_END),
            "importance": 5,
            "importance_initial": 2,
            "importance_current": 4,
            "last_accessed": _manifest_timestamp(CONVERSATION_LAST_ACCESSED),
            "is_fuzzy": True,
            "created_at": _manifest_timestamp(CONVERSATION_CREATED_AT),
        },
        PROFILES: {
            "user_id": USER_ID,
            "group_id": GROUP_ID,
            "version": 7,
            "display_name": "TSK-198 用户",
            "traits": TRAITS,
            "updated_at": PROFILE_UPDATED_AT.isoformat(),
            "importance": 5,
            "access_count": 17,
            "last_accessed": _manifest_timestamp(PROFILE_LAST_ACCESSED),
        },
        INTERACTIONS: {
            "id": INTERACTION_ID,
            "user_id": USER_ID,
            "display_name": "TSK-198 用户",
            "event_summary": EVENT_SUMMARY,
            "source_dedup_key": "tsk198-int-dedup",
            "source_message_count": 7,
            "first_seen_at": INTERACTION_FIRST_SEEN_AT.isoformat(),
            "last_seen_at": INTERACTION_LAST_SEEN_AT.isoformat(),
            "importance": 5,
            "importance_initial": 3,
            "importance_current": 4,
            "last_accessed": INTERACTION_LAST_ACCESSED.isoformat(),
            "is_fuzzy": True,
            "created_at": INTERACTION_CREATED_AT.isoformat(),
        },
    }


def _assert_canary_manifest(manifest: dict[str, Any]) -> None:
    assert manifest["kind"] == "komari-memory-export"
    assert manifest["schema_version"] == 1
    assert manifest["source_id"] == SOURCE_ID
    assert manifest["counts"] == {
        CONVERSATIONS: 1,
        PROFILES: 1,
        INTERACTIONS: 1,
    }
    records = manifest["records"]
    assert [record["table"] for record in records] == list(SOURCE_TABLES)
    assert len(records) == 3

    by_table = {record["table"]: record for record in records}
    expected_fields = _expected_fields()
    expected_kinds = {
        CONVERSATIONS: "conversation",
        PROFILES: "user_profile",
        INTERACTIONS: "interaction_history",
    }
    expected_provenance = {
        CONVERSATIONS: {"group_id": GROUP_ID},
        PROFILES: {"group_id": GROUP_ID},
        INTERACTIONS: {},
    }
    expected_primary_keys = {
        CONVERSATIONS: {"id": CONVERSATION_ID},
        PROFILES: {"user_id": USER_ID, "group_id": GROUP_ID},
        INTERACTIONS: {"id": INTERACTION_ID},
    }
    for table in SOURCE_TABLES:
        record = by_table[table]
        assert record["kind"] == expected_kinds[table]
        assert record["primary_key"] == expected_primary_keys[table]
        assert record["provenance"] == expected_provenance[table]
        assert record["fields"] == expected_fields[table]
        assert record["source_text"].startswith(f"record_kind: {record['kind']}")


async def test_export_before_retirement_then_head_export_is_byte_identical(
    tmp_path: Path,
) -> None:
    scratch = await recreate_scratch_database(f"_tsk198_{uuid4().hex[:10]}")
    db_url = scratch_url(str(scratch["database"]))
    first_manifest_path = tmp_path / "before-retirement.json"
    second_manifest_path = tmp_path / "after-retirement.json"
    try:
        # 1. 历史 0029：退役迁移前的数据库结构，导出必须先于退役发生。
        upgrade = run_bootstrap(db_url, "upgrade", HISTORICAL_REVISION)
        assert upgrade.returncode == 0, upgrade.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            assert await _alembic_version(connection) == HISTORICAL_REVISION
            await _insert_canaries(connection)
            source_rows_before = await _fetch_source_rows(connection)
        finally:
            await connection.close()

        # 2. 升级前用实际 CLI 子进程导出到私有 manifest。
        first_result = _run_export(db_url, first_manifest_path)
        assert first_result.returncode == 0, first_result.stderr
        first_bytes = first_manifest_path.read_bytes()
        first_manifest = json.loads(first_bytes)
        _assert_canary_manifest(first_manifest)
        assert stat.S_IMODE(first_manifest_path.stat().st_mode) == 0o600

        # 3. 升级到 0030/head 并做 ORM 零漂移检查。
        upgrade = run_bootstrap(db_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            assert await _alembic_version(connection) == HEAD_REVISION
            source_rows_after = await _fetch_source_rows(connection)
        finally:
            await connection.close()
        assert source_rows_after == source_rows_before, (
            "0030 退役不得改动或删除三张正式来源表的任何一行"
        )

        # 4. 同一 source id 再次导出：读取器仍可用且结果逐字节一致。
        second_result = _run_export(db_url, second_manifest_path)
        assert second_result.returncode == 0, second_result.stderr
        second_bytes = second_manifest_path.read_bytes()
        assert stat.S_IMODE(second_manifest_path.stat().st_mode) == 0o600
        assert second_bytes == first_bytes, (
            "升级前后同一 source id 的 manifest 必须逐字节一致"
        )
    finally:
        await drop_scratch_database(str(scratch["database"]))
