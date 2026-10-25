"""TSK-190：0025 退役判定场景四表与 38 个判定配置字段（隔离库验收）。

覆盖契约要求的数据库侧证据：

1. 全新库 ``upgrade head`` 停在 0026（0025 退役后），四张场景表不存在，判定配置表只剩
   3 个存储字段 + 13 个保留字段，且 ``orm_bootstrap check`` 零漂移；
2. 0024 存量库（四表按链接关系写入真实数据 + 两行判定配置：显式关闭与
   显式开启）升级后**只有**四表与 38 个字段消失，其他 schema 对象、其他
   配置表与其行、保留字段值、``revision`` 与 ``updated_at`` 原样保留；
3. 升级不伪造配置行，也不覆盖运维值；新初始化的配置默认启用（并在默认
   值状态下没有任何可用 Jev 连接）；
4. 0025 的 downgrade 明确不可逆：拒绝回退、不重建表、不回填数据。

全部用例在门控库派生的一次性隔离库内执行（唯一后缀），共享门控库始终停留
head，绝不 downgrade/重建门控库；不访问真实模型或平台服务。
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.config.typed_config import ensure_typed_config_model
from tests.db.tsk190_legacy_config_support import (
    RETAINED_DECISION_CONFIG_FIELDS,
    RETIRED_DECISION_CONFIG_FIELDS,
    STORAGE_COLUMNS,
    historical_decision_config_row,
)
from tests.db.tsk197_gate_support import (
    HEAD_REVISION,
    POSTGRES_URL,
    drop_scratch_database,
    insert_row_without_budget_columns,
    recreate_scratch_database,
    run_bootstrap,
    same_database,
    scratch_url,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")
DECISION_CONFIG_TABLE = "komari_decision_config"
CHAT_CONFIG_TABLE = "komari_chat_config"

#: 退役的四张场景表（依赖顺序：条目 → 运行时 → 场景 → 构建集）。
RETIRED_TABLES: tuple[str, ...] = (
    "komari_memory_scene_item",
    "komari_memory_scene_runtime",
    "komari_decision_scenes",
    "komari_memory_scene_set",
)

#: 0025 之后判定配置表应有的列（顺序无关，用集合比较）。
EXPECTED_CONFIG_COLUMNS: frozenset[str] = frozenset(
    (*STORAGE_COLUMNS, *RETAINED_DECISION_CONFIG_FIELDS)
)

SKIP_NO_POSTGRES = pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过迁移集成测试"
)


def _scratch_name(tag: str) -> str:
    return f"_{tag}_{uuid4().hex[:10]}"


async def _connect(scratch: Mapping[str, object]) -> asyncpg.Connection:
    return await asyncpg.connect(**scratch)  # type: ignore[arg-type]


async def _public_tables(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return {str(row["table_name"]) for row in rows}


async def _columns(connection: asyncpg.Connection, table: str) -> set[str]:
    rows = await connection.fetch(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = $1",
        table,
    )
    return {str(row["column_name"]) for row in rows}


async def _schema_snapshot(
    connection: asyncpg.Connection,
) -> dict[str, tuple[tuple[str, str, str, str | None], ...]]:
    """抓取全库 public schema 形状：表 → 有序列定义（不含行数据）。"""
    rows = await connection.fetch(
        "SELECT table_name, column_name, data_type, is_nullable, column_default"
        " FROM information_schema.columns"
        " WHERE table_schema = 'public'"
        " ORDER BY table_name, ordinal_position"
    )
    snapshot: dict[str, list[tuple[str, str, str, str | None]]] = {}
    for row in rows:
        snapshot.setdefault(str(row["table_name"]), []).append(
            (
                str(row["column_name"]),
                str(row["data_type"]),
                str(row["is_nullable"]),
                None if row["column_default"] is None else str(row["column_default"]),
            )
        )
    return {table: tuple(columns) for table, columns in snapshot.items()}


async def _insert_decision_config_row(
    connection: asyncpg.Connection,
    **overrides: object,
) -> dict[str, object]:
    """按 0024 时代表结构写入一行判定配置，返回写入后的实际行。"""
    values = historical_decision_config_row(**overrides)
    await connection.execute(
        "INSERT INTO komari_decision_config"
        " SELECT * FROM json_populate_record("
        "NULL::komari_decision_config, $1::json)",
        json.dumps(values),
    )
    row_id = values["id"]
    record = await connection.fetchrow(
        "SELECT * FROM komari_decision_config WHERE id = $1", row_id
    )
    assert record is not None
    return dict(record)


async def _populate_scene_storage(connection: asyncpg.Connection) -> None:
    """按真实外键关系写入四张退役表的数据（升级前必须存在）。"""
    await connection.execute(
        "INSERT INTO komari_decision_scenes"
        " (id, scene_key, scene_type, content_text, content_hash, enabled,"
        "  order_index, created_at, updated_at)"
        " VALUES"
        " (1, 'NOISE', 'fixed', '闲聊噪声', 'hash-noise', true, 0, now(), now()),"
        " (2, 'CUSTOM_SCENE', 'general', '自定义场景', 'hash-custom', true, 1,"
        "  now(), now())"
    )
    await connection.execute(
        "INSERT INTO komari_memory_scene_set"
        " (id, source_path, source_hash, embedding_model,"
        "  embedding_instruction_hash, status, item_total, item_ready,"
        "  item_failed, created_at, ready_at)"
        " VALUES (1, 'legacy/scenes.yaml', 'source-hash', 'tsk190-embedding',"
        " 'instruction-hash', 'READY', 2, 2, 0, now(), now())"
    )
    await connection.execute(
        "INSERT INTO komari_memory_scene_item"
        " (id, set_id, scene_id, scene_key_snapshot, scene_type_snapshot,"
        "  content_text_snapshot, enabled_snapshot, order_index_snapshot,"
        "  content_hash, status, attempt_count)"
        " VALUES"
        " (1, 1, 1, 'NOISE', 'fixed', '闲聊噪声', true, 0, 'hash-noise',"
        "  'READY', 0),"
        " (2, 1, 2, 'CUSTOM_SCENE', 'general', '自定义场景', true, 1,"
        "  'hash-custom', 'READY', 0)"
    )
    # 0002 已播种运行时单例行（active_set_id 为 NULL）；这里把它指向
    # 刚写入的构建集，证明升级前存在真实的链接关系。
    await connection.execute(
        "UPDATE komari_memory_scene_runtime SET active_set_id = 1 WHERE id = 1"
    )


async def _scene_storage_counts(
    connection: asyncpg.Connection,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in RETIRED_TABLES:
        counts[table] = int(
            await connection.fetchval(f"SELECT count(*) FROM {table}") or 0
        )
    return counts


async def _config_rows(
    connection: asyncpg.Connection,
) -> dict[int, dict[str, object]]:
    rows = await connection.fetch("SELECT * FROM komari_decision_config ORDER BY id")
    return {int(row["id"]): dict(row) for row in rows}


async def _first_chat_row(connection: asyncpg.Connection) -> dict[str, object]:
    row = await connection.fetchrow("SELECT * FROM komari_chat_config WHERE id = 1")
    assert row is not None
    return dict(row)


def _project(row: Mapping[str, object], columns: Iterable[str]) -> dict[str, object]:
    return {column: row[column] for column in columns}


@SKIP_NO_POSTGRES
@pytest.mark.asyncio
async def test_fresh_install_reaches_head_without_scene_storage() -> None:
    """全新库升到 head：四表与 38 字段不存在，模型元数据零漂移。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")
    scratch = await recreate_scratch_database(_scratch_name("tsk190fresh"))
    database_url = scratch_url(str(scratch["database"]))
    try:
        upgraded = run_bootstrap(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        connection = await _connect(scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            )
            tables = await _public_tables(connection)
            assert not set(RETIRED_TABLES) & tables
            assert await _columns(connection, DECISION_CONFIG_TABLE) == (
                set(EXPECTED_CONFIG_COLUMNS)
            )
            # 空库升级不得伪造配置行；首次读取才由配置管理器按 Schema 默认值初始化
            assert (
                await connection.fetchval("SELECT count(*) FROM komari_decision_config")
                == 0
            )
            # 当前共享 schema 约定：配置列没有 server default
            assert (
                await connection.fetchval(
                    "SELECT column_default FROM information_schema.columns"
                    " WHERE table_schema = 'public'"
                    " AND table_name = 'komari_decision_config'"
                    " AND column_name = 'plugin_enable'"
                )
                is None
            )
        finally:
            await connection.close()
        checked = run_bootstrap(database_url, "check")
        assert checked.returncode == 0, checked.stdout + checked.stderr
    finally:
        await drop_scratch_database(str(scratch["database"]))


@SKIP_NO_POSTGRES
@pytest.mark.asyncio
async def test_populated_0024_upgrade_retires_only_scene_storage() -> None:
    """0024 有数据时升级只删四表与 38 字段，其他对象与运维值全部保留。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")
    scratch = await recreate_scratch_database(_scratch_name("tsk190upgrade"))
    database_url = scratch_url(str(scratch["database"]))
    try:
        upgraded = run_bootstrap(database_url, "upgrade", "0024")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        # 两行判定配置：id=1 显式关闭、id=2 显式开启，各自带运维改写的
        # 别名 / Jev 值 / revision；同时写一行兄弟配置表，验证无关数据不动。
        connection = await _connect(scratch)
        try:
            disabled_row = await _insert_decision_config_row(
                connection,
                id=1,
                revision=42,
                plugin_enable=False,
                bot_aliases=["运维关闭", "komari"],
                jev_base_url="",
                jev_api_key="",
                jev_model="jev-1.14.0",
                decision_log_retention_days=9,
            )
            enabled_row = await _insert_decision_config_row(
                connection,
                id=2,
                revision=7,
                plugin_enable=True,
                bot_aliases=["运维开启"],
                jev_base_url="https://jev.example.invalid/system-one",
                jev_api_key="tsk190-dummy-key",
                jev_model="jev-1.15.0",
                jev_http_timeout_seconds=4.5,
                jev_request_timeout_seconds=5.5,
                max_concurrent_decisions=2,
                optional_reply_probability=0.25,
                decision_log_retention_days=14,
            )
            await insert_row_without_budget_columns(connection, revision=9)
            await _populate_scene_storage(connection)

            # 前提确认：四表确实有链接数据（否则“表消失”是空证据）
            assert await _scene_storage_counts(connection) == dict(
                zip(RETIRED_TABLES, (2, 1, 2, 1), strict=True)
            )
            assert (
                await connection.fetchval(
                    "SELECT active_set_id FROM komari_memory_scene_runtime WHERE id = 1"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM komari_memory_scene_item"
                    " WHERE set_id = 1 AND scene_id = 2"
                )
                == 1
            )

            before_schema = await _schema_snapshot(connection)
            before_tables = await _public_tables(connection)
            before_config = await _config_rows(connection)
            before_chat = await _first_chat_row(connection)
            before_config_columns = await _columns(connection, DECISION_CONFIG_TABLE)
        finally:
            await connection.close()

        # 0024 时代列集合 = 3 存储 + 13 保留 + 38 退役
        assert before_config_columns == set(EXPECTED_CONFIG_COLUMNS) | set(
            RETIRED_DECISION_CONFIG_FIELDS
        )
        assert before_config[1] == disabled_row
        assert before_config[2] == enabled_row

        upgraded = run_bootstrap(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        connection = await _connect(scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            )
            # 1. 恰好那四张表消失，其余表一个不少
            after_tables = await _public_tables(connection)
            assert after_tables == before_tables - set(RETIRED_TABLES)
            # 2. 其余 schema 对象逐列不变（列名/类型/可空/默认值）
            after_schema = await _schema_snapshot(connection)
            expected_schema = {
                table: definition
                for table, definition in before_schema.items()
                if table not in RETIRED_TABLES
            }
            expected_schema[DECISION_CONFIG_TABLE] = tuple(
                definition
                for definition in before_schema[DECISION_CONFIG_TABLE]
                if definition[0] not in set(RETIRED_DECISION_CONFIG_FIELDS)
            )
            assert after_schema == expected_schema
            # 3. 判定配置表恰好少 38 列，剩下的就是存储 + 保留字段
            after_config_columns = await _columns(connection, DECISION_CONFIG_TABLE)
            assert before_config_columns - after_config_columns == set(
                RETIRED_DECISION_CONFIG_FIELDS
            )
            assert after_config_columns == set(EXPECTED_CONFIG_COLUMNS)
            # 4. 运维值、revision、updated_at 原样保留；行数不改写
            after_config = await _config_rows(connection)
            assert set(after_config) == set(before_config)
            surviving = sorted(EXPECTED_CONFIG_COLUMNS)
            for row_id, before_row in before_config.items():
                assert _project(after_config[row_id], surviving) == _project(
                    before_row, surviving
                )
                assert after_config[row_id]["updated_at"] == before_row["updated_at"]
            assert after_config[1]["plugin_enable"] is False
            assert after_config[2]["plugin_enable"] is True
            # 5. 兄弟配置表数据不变
            assert await _first_chat_row(connection) == before_chat
            assert await connection.fetchval(
                "SELECT count(*) FROM komari_decision_config"
            ) == len(before_config)
        finally:
            await connection.close()

        checked = run_bootstrap(database_url, "check")
        assert checked.returncode == 0, checked.stdout + checked.stderr
    finally:
        await drop_scratch_database(str(scratch["database"]))


@SKIP_NO_POSTGRES
@pytest.mark.asyncio
async def test_new_configuration_is_enabled_by_default_but_unconfigured() -> None:
    """无行库升级后仍无行；新初始化配置启用但没有任何可用 Jev 连接。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")
    scratch = await recreate_scratch_database(_scratch_name("tsk190empty"))
    database_url = scratch_url(str(scratch["database"]))
    try:
        upgraded = run_bootstrap(database_url, "upgrade", "0024")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        upgraded = run_bootstrap(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        connection = await _connect(scratch)
        try:
            assert (
                await connection.fetchval("SELECT count(*) FROM komari_decision_config")
                == 0
            )
            # 列无 server default：init 由配置管理器按 Pydantic 默认值执行，
            # 因此升级不会把插件开关写进任何存量行
            defaults = {
                str(row["column_name"]): row["column_default"]
                for row in await connection.fetch(
                    "SELECT column_name, column_default"
                    " FROM information_schema.columns"
                    " WHERE table_schema = 'public'"
                    " AND table_name = 'komari_decision_config'"
                )
            }
            assert set(defaults) == set(EXPECTED_CONFIG_COLUMNS)
            assert {name: value for name, value in defaults.items() if value} == {}
        finally:
            await connection.close()

        schema = ensure_typed_config_model("komari_decision")
        assert schema is not None
        config = cast("type[Any]", schema)()
        # 新初始化的配置默认启用，但仍然没有可用连接（enabled-but-unconfigured）
        assert config.plugin_enable is True
        assert schema.model_fields["plugin_enable"].default is True
        assert config.jev_base_url == ""
        assert config.jev_api_key == ""
        assert config.startup_required is False
    finally:
        await drop_scratch_database(str(scratch["database"]))


@SKIP_NO_POSTGRES
@pytest.mark.asyncio
async def test_downgrade_from_0025_is_rejected_without_restoring_storage() -> None:
    """0025 不可逆：downgrade 直接失败，不重建表、不回填任何数据。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")
    scratch = await recreate_scratch_database(_scratch_name("tsk190down"))
    database_url = scratch_url(str(scratch["database"]))
    try:
        upgraded = run_bootstrap(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        downgraded = run_bootstrap(database_url, "downgrade", "0024")
        assert downgraded.returncode != 0, (
            "0025 必须拒绝回退，实际成功:\n" + downgraded.stdout
        )
        output = downgraded.stdout + downgraded.stderr
        assert "0025_SCENE_RETIREMENT_IS_IRREVERSIBLE" in output

        connection = await _connect(scratch)
        try:
            # 拒绝回退后仍停在 0025，且没有表/数据被“恢复”
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            )
            tables = await _public_tables(connection)
            assert not set(RETIRED_TABLES) & tables
            assert await _columns(connection, DECISION_CONFIG_TABLE) == (
                set(EXPECTED_CONFIG_COLUMNS)
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
