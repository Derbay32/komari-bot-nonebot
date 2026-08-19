"""旧版 JSONB 配置 → 强类型表离线迁移脚本测试。

测试脚本独立加载（不 import komari_bot 运行时代码），与脚本自身的
独立性约束保持一致。集成测试走真实 PostgreSQL，环境变量门控：
未设置 ``KOMARI_TEST_POSTGRES_URL`` 时跳过。

隔离纪律：集成用例在从门控 DSN 派生的一次性隔离库（库名后缀
``_legacycfg``）内先 ``upgrade head`` 再执行脚本验收，用例结束即
DROP；共享门控库的版本与数据不受搬移影响，重复执行与执行顺序
互不影响。门控用户需要 CREATEDB 权限。

TSK-190 语义：legacy JSONB 夹具仍可携带 ``output_instruction`` 作
迁移输入，但 head 强类型表的查询/断言不得引用已删除列；脚本按新
契约丢弃 ``output_instruction``（不并入任何新字段），保留其余自定义
Prompt 字段，新增行为列在 legacy 迁移阶段保持空值（待统一 seed 补齐，
补齐路径由 ``test_prompt_seed_bootstrap_integration.py`` 覆盖）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/migrate_legacy_config_to_typed_tables.py"

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")

#: TSK-190 被删除列。本地镜像 ``tests/config/chat_prompt_field_contract.py``
#: 的 ``REMOVED_FIELD``；本文件刻意不 import komari_bot 运行时代码（与脚本
#: 自身独立性约束一致），因此就地声明并在 docstring 中指向 oracle。
REMOVED_PROMPT_FIELD = "output_instruction"

#: 0003 旧 Prompt 表保留列（TSK-190 迁移所删除字段之外的既有正文列）。
#: 同上镜像 oracle 的 ``LEGACY_KEPT_CHAT_COLUMNS``。
LEGACY_KEPT_CHAT_PROMPT_COLUMNS: frozenset[str] = frozenset(
    {
        "system_prompt",
        "memory_ack",
        "memory_ack_role",
        "cot_prefix",
        "cot_prefix_role",
    }
)


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.migrate_legacy_config_to_typed_tables",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestParseDsn:
    """DSN 解析：postgresql:// 与 postgresql+asyncpg:// 均须可解析。"""

    @staticmethod
    def test_plain_postgresql_scheme() -> None:
        module = _load_script_module()
        parsed = module.parse_dsn(
            "postgresql://komari_bot:komari_bot_ci@db.example:55433/komari"
        )
        assert parsed["host"] == "db.example"
        assert parsed["port"] == 55433
        assert parsed["database"] == "komari"
        assert parsed["user"] == "komari_bot"
        assert parsed["password"] == "komari_bot_ci"

    @staticmethod
    def test_asyncpg_driver_scheme() -> None:
        module = _load_script_module()
        parsed = module.parse_dsn(
            "postgresql+asyncpg://komari_bot:pw@localhost:5432/komari_bot"
        )
        assert parsed["host"] == "localhost"
        assert parsed["port"] == 5432
        assert parsed["database"] == "komari_bot"
        assert parsed["user"] == "komari_bot"
        assert parsed["password"] == "pw"

    @staticmethod
    def test_url_encoded_credentials_are_decoded() -> None:
        module = _load_script_module()
        parsed = module.parse_dsn("postgresql://user:p%40ss%2Fword@localhost:5432/db")
        assert parsed["user"] == "user"
        assert parsed["password"] == "p@ss/word"

    @staticmethod
    def test_default_port_is_5432() -> None:
        module = _load_script_module()
        parsed = module.parse_dsn("postgresql://u:p@localhost/db")
        assert parsed["port"] == 5432

    @staticmethod
    def test_rejects_unsupported_scheme() -> None:
        module = _load_script_module()
        with pytest.raises(ValueError, match="只支持 postgresql"):
            module.parse_dsn("mysql://u:p@localhost/db")

    @staticmethod
    def test_rejects_empty_dsn() -> None:
        module = _load_script_module()
        with pytest.raises(ValueError, match="DSN"):
            module.parse_dsn("")


class TestConvertValue:
    """JSONB 值 → 目标列类型的转换。"""

    @staticmethod
    def test_direct_scalar_types() -> None:
        module = _load_script_module()
        assert (
            module.convert_value(raw=True, data_type="BOOLEAN", column_name="flag")
            is True
        )
        assert module.convert_value(42, "INTEGER", "count") == 42
        assert module.convert_value(3.5, "DOUBLE PRECISION", "ratio") == 3.5
        assert module.convert_value(7, "DOUBLE PRECISION", "ratio") == 7.0
        assert module.convert_value("abc", "CHARACTER VARYING", "name") == "abc"
        assert module.convert_value("chat", "TEXT", "body") == "chat"

    @staticmethod
    def test_jsonb_list_and_dict_pass_through() -> None:
        module = _load_script_module()
        assert module.convert_value(["a", "b"], "JSONB", "wl") == ["a", "b"]
        assert module.convert_value({"k": 1}, "JSONB", "extra") == {"k": 1}

    @staticmethod
    def test_type_mismatch_raises() -> None:
        module = _load_script_module()
        with pytest.raises(ValueError, match="count"):
            module.convert_value("3", "INTEGER", "count")
        with pytest.raises(ValueError, match="flag"):
            module.convert_value("yes", "BOOLEAN", "flag")
        with pytest.raises(ValueError, match="name"):
            module.convert_value(42, "CHARACTER VARYING", "name")


class TestPlanRowValues:
    """旧 JSONB 行 → 写入值 + 决算元数据（键→列静态映射的运行时行为）。"""

    @staticmethod
    def _spec(module: Any) -> Any:
        spec = next(s for s in module._RESOURCE_SPECS if s.key_value == "user_data")
        assert spec.columns == (
            "plugin_enable",
            "initial_favorability",
            "max_favorability_delta_per_reply",
        )
        return spec

    @staticmethod
    def _column_info(
        columns: Sequence[str] = ("plugin_enable",),
    ) -> dict[str, tuple[str, bool]]:
        info: dict[str, tuple[str, bool]] = {}
        for column in columns:
            if column in ("plugin_enable",):
                info[column] = ("BOOLEAN", False)
            elif column == "initial_favorability":
                info[column] = ("INTEGER", False)
            elif column == "max_favorability_delta_per_reply":
                info[column] = ("INTEGER", True)
            else:
                raise AssertionError(column)
        return info

    @staticmethod
    def test_full_row_migrates_all_keys() -> None:
        module = _load_script_module()
        spec = TestPlanRowValues._spec(module)
        info = TestPlanRowValues._column_info(spec.columns)
        planned = module.plan_row_values(
            spec,
            {
                "plugin_enable": True,
                "initial_favorability": 30,
                "max_favorability_delta_per_reply": 5,
            },
            info,
        )
        assert planned.values == {
            "plugin_enable": True,
            "initial_favorability": 30,
            "max_favorability_delta_per_reply": 5,
        }
        assert planned.migrated_keys == [
            "plugin_enable",
            "initial_favorability",
            "max_favorability_delta_per_reply",
        ]
        assert planned.dropped_keys == []
        assert planned.defaulted_keys == []

    @staticmethod
    def test_deprecated_and_unknown_keys_are_dropped() -> None:
        module = _load_script_module()
        spec = TestPlanRowValues._spec(module)
        info = TestPlanRowValues._column_info(spec.columns)
        planned = module.plan_row_values(
            spec,
            {
                "plugin_enable": True,
                "initial_favorability": 30,
                "version": "1.0",
                "last_updated": "2026-01-01",
                "schema_name": "LegacyJsonConfig",
                "unknown_key": 1,
            },
            info,
        )
        assert set(planned.dropped_keys) == {
            "version",
            "last_updated",
            "schema_name",
            "unknown_key",
        }
        assert planned.values["plugin_enable"] is True

    @staticmethod
    def test_missing_not_null_columns_fall_back_to_defaults() -> None:
        module = _load_script_module()
        spec = TestPlanRowValues._spec(module)
        info = TestPlanRowValues._column_info(spec.columns)
        planned = module.plan_row_values(
            spec,
            {"plugin_enable": False},
            info,
        )
        assert planned.values == {"plugin_enable": False, "initial_favorability": 0}
        assert planned.defaulted_keys == [
            "initial_favorability",
            "max_favorability_delta_per_reply",
        ]

    @staticmethod
    def test_missing_nullable_column_is_not_written() -> None:
        module = _load_script_module()
        spec = TestPlanRowValues._spec(module)
        info = TestPlanRowValues._column_info(spec.columns)
        planned = module.plan_row_values(
            spec,
            {"plugin_enable": True, "initial_favorability": 3},
            info,
        )
        assert planned.values == {"plugin_enable": True, "initial_favorability": 3}
        assert planned.defaulted_keys == ["max_favorability_delta_per_reply"]

    @staticmethod
    def test_none_value_treated_as_missing() -> None:
        module = _load_script_module()
        spec = TestPlanRowValues._spec(module)
        info = TestPlanRowValues._column_info(spec.columns)
        planned = module.plan_row_values(
            spec,
            {"plugin_enable": True, "initial_favorability": None},
            info,
        )
        assert planned.values == {"plugin_enable": True, "initial_favorability": 0}
        assert planned.defaulted_keys == [
            "initial_favorability",
            "max_favorability_delta_per_reply",
        ]

    @staticmethod
    def test_jsonb_list_default_uses_static_declaration() -> None:
        module = _load_script_module()
        spec = next(s for s in module._RESOURCE_SPECS if s.key_value == "sr")
        info: dict[str, tuple[str, bool]] = {}
        for column in spec.columns:
            info[column] = (
                ("JSONB", False)
                if column
                in (
                    "user_whitelist",
                    "group_whitelist",
                    "sr_list",
                )
                else ("BOOLEAN", False)
            )
        planned = module.plan_row_values(
            spec,
            {"user_whitelist": ["1"]},
            info,
        )
        assert planned.values["user_whitelist"] == ["1"]
        assert planned.values["group_whitelist"] == []
        assert planned.values["sr_list"] == []
        assert planned.defaulted_keys == [
            "plugin_enable",
            "group_whitelist",
            "sr_list",
            "list_chunk_size",
            "redis_db",
        ]

    @staticmethod
    def test_type_mismatch_raises() -> None:
        module = _load_script_module()
        spec = TestPlanRowValues._spec(module)
        info = TestPlanRowValues._column_info(spec.columns)
        with pytest.raises(ValueError, match="initial_favorability"):
            module.plan_row_values(
                spec,
                {"plugin_enable": True, "initial_favorability": "高"},
                info,
            )

    @staticmethod
    def test_declared_column_missing_from_database_raises() -> None:
        module = _load_script_module()
        spec = TestPlanRowValues._spec(module)
        info = TestPlanRowValues._column_info(("plugin_enable", "initial_favorability"))
        with pytest.raises(RuntimeError, match="max_favorability_delta_per_reply"):
            module.plan_row_values(
                spec,
                {"plugin_enable": True, "initial_favorability": 1},
                info,
            )

    @staticmethod
    def test_chat_legacy_keys_map_to_fulfillment_columns() -> None:
        """离线脚本继续读取旧 JSONB 键，但只写新履约配置列。"""
        module = _load_script_module()
        spec = next(
            item
            for item in module._RESOURCE_SPECS
            if item.target_table == "komari_chat_config"
        )
        expected_mapping = {
            "reply_commit_worker_interval_seconds": (
                "reply_fulfillment_worker_interval_seconds"
            ),
            "reply_commit_batch_size": "reply_fulfillment_batch_size",
            "reply_commit_lease_seconds": "reply_fulfillment_lease_seconds",
            "reply_commit_max_attempts": "reply_fulfillment_max_attempts",
            "reply_commit_retry_base_seconds": ("reply_fulfillment_retry_base_seconds"),
            "reply_commit_tombstone_retention_days": (
                "reply_fulfillment_tombstone_retention_days"
            ),
            # TSK-194：旧图片开关读入，写入模式枚举列（值经变换器转换）
            "vision_tool_enabled": "image_understanding_mode",
        }
        info: dict[str, tuple[str, bool]] = dict.fromkeys(
            spec.columns, ("INTEGER", False)
        )
        info.update(
            {
                "proactive_enabled": ("BOOLEAN", False),
                "proactive_cooldown": ("INTEGER", False),
                "proactive_max_per_hour": ("INTEGER", False),
                "proactive_reservation_ttl_seconds": ("INTEGER", False),
                "reply_fulfillment_retry_max_seconds": ("INTEGER", False),
                "reply_fulfillment_freshness_seconds": ("INTEGER", False),
                "image_understanding_mode": ("CHARACTER VARYING", False),
            }
        )
        source = {old_name: index for index, old_name in enumerate(expected_mapping, 1)}
        source["vision_tool_enabled"] = True  # 变换器只接受布尔

        planned = module.plan_row_values(spec, source, info)

        assert spec.legacy_key_map == expected_mapping
        # 值级变换：vision_tool_enabled 只接受布尔，缺键回退列默认 delegated
        assert (
            spec.column_transformers["image_understanding_mode"](raw=True) == "delegated"
        )
        assert (
            spec.column_transformers["image_understanding_mode"](raw=False) == "native"
        )
        assert spec.default_value_overrides["image_understanding_mode"] == "delegated"
        assert planned.values["image_understanding_mode"] == "delegated"
        expected_non_vision = {  # 排包图片模式列的期望映射（保持与 source 同源）
            new_name: index
            for index, (old_name, new_name) in enumerate(
                (kv for kv in expected_mapping.items() if kv[0] != "vision_tool_enabled"),
                1,
            )
        }
        assert {
            new_name: planned.values[new_name]
            for new_name in set(expected_mapping.values())
            - {"image_understanding_mode"}
        } == expected_non_vision
        assert not set(expected_mapping).intersection(spec.columns)

    @staticmethod
    def test_chat_vision_legacy_values_map_to_mode_enum() -> None:
        """TSK-194：旧布尔开关双分支 → 模式枚举，预算同名直写。"""
        module = _load_script_module()
        spec = next(
            item
            for item in module._RESOURCE_SPECS
            if item.target_table == "komari_chat_config"
        )
        info: dict[str, tuple[str, bool]] = dict.fromkeys(
            spec.columns, ("INTEGER", False)
        )
        info.update(
            {
                "image_understanding_mode": ("CHARACTER VARYING", False),
                "vision_image_download_connect_timeout_seconds": (
                    "DOUBLE PRECISION",
                    False,
                ),
                "vision_image_download_read_timeout_seconds": (
                    "DOUBLE PRECISION",
                    False,
                ),
                "vision_image_download_total_timeout_seconds": (
                    "DOUBLE PRECISION",
                    False,
                ),
            }
        )
        budgets = {
            "vision_image_download_max_count": 6,
            "vision_image_download_max_bytes": 9 * 1024 * 1024,
            "vision_image_download_total_max_bytes": 21 * 1024 * 1024,
            "vision_image_download_max_pixels": 41_000_000,
            "vision_image_download_concurrency": 3,
            "vision_image_download_connect_timeout_seconds": 6.0,
            "vision_image_download_read_timeout_seconds": 31.0,
            "vision_image_download_total_timeout_seconds": 46.0,
        }

        planned_delegated = module.plan_row_values(
            spec,
            {"vision_tool_enabled": True, **budgets},
            info,
        )
        assert planned_delegated.values["image_understanding_mode"] == "delegated"
        for column, expected in budgets.items():
            assert planned_delegated.values[column] == expected

        planned_native = module.plan_row_values(
            spec,
            {"vision_tool_enabled": False, **budgets},
            info,
        )
        assert planned_native.values["image_understanding_mode"] == "native"
        for column, expected in budgets.items():
            assert planned_native.values[column] == expected

        # legacy 缺开关：写入列级默认覆盖 delegated（非类型中性空串）
        planned_missing = module.plan_row_values(spec, {**budgets}, info)
        assert planned_missing.values["image_understanding_mode"] == "delegated"

        # 非布尔值：变换器拒绝，报告失败而非静默降级
        with pytest.raises(ValueError, match="vision_tool_enabled"):
            module.plan_row_values(spec, {"vision_tool_enabled": "yes"}, info)


class _FakeConnection:
    """记录 execute 调用、按查询内容返回预设行的假 asyncpg 连接。"""

    def __init__(
        self,
        *,
        info_schema: list[dict[str, Any]],
        plugin_rows: list[dict[str, Any]],
        prompt_rows: list[dict[str, Any]],
    ) -> None:
        self.info_schema = info_schema
        self.plugin_rows = plugin_rows
        self.prompt_rows = prompt_rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.fetched_queries: list[str] = []

    async def fetch(self, query: str, *_args: object) -> list[dict[str, Any]]:
        self.fetched_queries.append(query)
        if "information_schema.columns" in query:
            return self.info_schema
        if "komari_plugin_configs" in query:
            return self.plugin_rows
        if "komari_prompt_configs" in query:
            return self.prompt_rows
        pytest.fail(f"未预期的查询: {query}")
        return []

    async def set_type_codec(self, _type_name: str, **_kwargs: object) -> None:
        return None

    async def execute(self, query: str, *args: object) -> str:
        self.executed.append((query, args))
        return "OK"


class TestBuildUpsertSql:
    """upsert 只写入规划的列；id/revision/updated_at 恒被写入。"""

    @staticmethod
    def test_sql_shape_only_writes_planned_columns() -> None:
        module = _load_script_module()
        spec = next(s for s in module._RESOURCE_SPECS if s.key_value == "user_data")
        sql = module.build_upsert_sql(
            spec,
            {"plugin_enable": True, "initial_favorability": 0},
            {"plugin_enable": "BOOLEAN", "initial_favorability": "INTEGER"},
            ("plugin_enable",),
        )
        assert sql.startswith(
            "INSERT INTO komari_user_data_config (id, revision, updated_at,"
            ' "plugin_enable", "initial_favorability")'
        )
        assert "ON CONFLICT (id) DO UPDATE" in sql
        assert "$3::boolean" in sql
        assert "$4::integer" in sql
        # 缺键列（initial_favorability）不进入 UPDATE 覆盖清单，播种值不被覆盖
        assert 'EXCLUDED."initial_favorability"' not in sql
        assert '"plugin_enable" = EXCLUDED."plugin_enable"' in sql
        assert "max_favorability_delta_per_reply" not in sql
        assert "revision = EXCLUDED.revision" in sql
        assert "updated_at = EXCLUDED.updated_at" in sql

    @staticmethod
    def test_sql_without_update_keys_still_valid() -> None:
        module = _load_script_module()
        spec = next(s for s in module._RESOURCE_SPECS if s.key_value == "user_data")
        sql = module.build_upsert_sql(
            spec,
            {"plugin_enable": False, "initial_favorability": 0},
            {"plugin_enable": "BOOLEAN", "initial_favorability": "INTEGER"},
            (),
        )
        assert sql.endswith("updated_at = EXCLUDED.updated_at")
        assert "DO UPDATE SET revision = EXCLUDED.revision" in sql


class TestMigrateLegacyConfigs:
    """fake 连接上的完整迁移流程。"""

    @staticmethod
    def _specs(module: Any) -> list[Any]:
        wanted = {"user_data", "komari_chat"}
        return [s for s in module._RESOURCE_SPECS if s.key_value in wanted]

    @staticmethod
    def _info_schema(module: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for spec in TestMigrateLegacyConfigs._specs(module):
            for column in spec.columns:
                if column in ("plugin_enable",):
                    data_type, nullable = "BOOLEAN", "NO"
                elif column == "initial_favorability":
                    data_type, nullable = "INTEGER", "NO"
                elif column == "max_favorability_delta_per_reply":
                    data_type, nullable = "INTEGER", "YES"
                else:
                    data_type, nullable = "TEXT", "NO"
                rows.append(
                    {
                        "table_name": spec.target_table,
                        "column_name": column,
                        "data_type": data_type,
                        "is_nullable": nullable,
                    }
                )
        return rows

    @staticmethod
    def test_migrates_known_rows_with_inherited_revision(
        monkeypatch: Any,
    ) -> None:
        module = _load_script_module()
        updated_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        connection = _FakeConnection(
            info_schema=TestMigrateLegacyConfigs._info_schema(module),
            plugin_rows=[
                {
                    "key_value": "user_data",
                    "data": {
                        "plugin_enable": True,
                        "initial_favorability": 30,
                        "max_favorability_delta_per_reply": None,
                        "version": "1.0",
                        "last_updated": "2026-01-01",
                    },
                    "revision": 7,
                    "updated_at": updated_at,
                },
            ],
            prompt_rows=[],
        )
        monkeypatch.setattr(module.asyncpg, "connect", lambda **_: connection)

        async def _run() -> Any:
            return await module.migrate_legacy_configs(
                connection,
                specs=TestMigrateLegacyConfigs._specs(module),
            )

        result = asyncio.run(_run())

        assert len(connection.executed) == 1
        sql, args = connection.executed[0]
        assert "komari_user_data_config" in sql
        assert args[0] == 7  # revision 继承 legacy 行
        assert args[1] == updated_at  # updated_at 继承 legacy 行
        assert args[2] is True  # plugin_enable
        assert args[3] == 30  # initial_favorability

        report = result.reports[0]
        assert report.migrated is True
        assert report.spec.key_value == "user_data"
        assert report.revision == 7
        assert set(report.dropped_keys) == {"version", "last_updated"}
        assert report.defaulted_keys == ["max_favorability_delta_per_reply"]

    @staticmethod
    def test_missing_legacy_row_is_reported_as_skipped(
        monkeypatch: Any,
    ) -> None:
        module = _load_script_module()
        connection = _FakeConnection(
            info_schema=TestMigrateLegacyConfigs._info_schema(module),
            plugin_rows=[],
            prompt_rows=[],
        )
        monkeypatch.setattr(module.asyncpg, "connect", lambda **_: connection)

        async def _run() -> Any:
            return await module.migrate_legacy_configs(
                connection,
                specs=TestMigrateLegacyConfigs._specs(module),
            )

        result = asyncio.run(_run())
        assert len(connection.executed) == 0
        assert all(not report.migrated for report in result.reports)
        assert len(result.reports) == 2

    @staticmethod
    def test_unknown_legacy_rows_are_collected(monkeypatch: Any) -> None:
        module = _load_script_module()
        connection = _FakeConnection(
            info_schema=TestMigrateLegacyConfigs._info_schema(module),
            plugin_rows=[
                {
                    "key_value": "some_legacy_plugin",
                    "data": {"plugin_enable": True},
                    "revision": 1,
                    "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
                },
            ],
            prompt_rows=[],
        )
        monkeypatch.setattr(module.asyncpg, "connect", lambda **_: connection)

        async def _run() -> Any:
            return await module.migrate_legacy_configs(
                connection,
                specs=TestMigrateLegacyConfigs._specs(module),
            )

        result = asyncio.run(_run())
        assert result.unknown_keys == ["some_legacy_plugin"]

    @staticmethod
    def test_second_run_is_idempotent() -> None:
        module = _load_script_module()
        updated_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

        def _make_connection() -> _FakeConnection:
            return _FakeConnection(
                info_schema=TestMigrateLegacyConfigs._info_schema(module),
                plugin_rows=[
                    {
                        "key_value": "user_data",
                        "data": {
                            "plugin_enable": True,
                            "initial_favorability": 30,
                        },
                        "revision": 3,
                        "updated_at": updated_at,
                    },
                ],
                prompt_rows=[],
            )

        async def _run(connection: _FakeConnection) -> Any:
            return await module.migrate_legacy_configs(
                connection,
                specs=TestMigrateLegacyConfigs._specs(module),
            )

        first = _make_connection()
        asyncio.run(_run(first))
        second = _make_connection()
        asyncio.run(_run(second))
        assert first.executed == second.executed


class TestRenderReport:
    """决算报告包含三类清单、汇总与可重复执行提示。"""

    @staticmethod
    def _result(module: Any) -> Any:
        spec = next(s for s in module._RESOURCE_SPECS if s.key_value == "user_data")
        reports = [
            module.ResourceReport(
                spec=spec,
                migrated=True,
                revision=7,
                updated_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
                migrated_keys=["plugin_enable", "initial_favorability"],
                dropped_keys=["version", "last_updated", "schema_name"],
                defaulted_keys=["max_favorability_delta_per_reply"],
                error=None,
            ),
            module.ResourceReport(
                spec=next(
                    s for s in module._RESOURCE_SPECS if s.key_value == "komari_chat"
                ),
                migrated=False,
                revision=None,
                updated_at=None,
                migrated_keys=[],
                dropped_keys=[],
                defaulted_keys=[],
                error=None,
            ),
        ]
        return module.MigrationResult(reports=reports, unknown_keys=["mystery_plugin"])

    @staticmethod
    def test_report_contains_three_lists_and_summary() -> None:
        module = _load_script_module()
        text = module.render_report(TestRenderReport._result(module))
        assert "user_data -> komari_user_data_config" in text
        assert "已迁移键 (2)" in text
        assert "plugin_enable" in text
        assert "丢弃弃用键 (3)" in text
        assert "last_updated" in text
        assert "落回默认值列 (1)" in text
        assert "max_favorability_delta_per_reply" in text
        assert "komari_chat -> komari_prompt_komari_chat" in text
        assert "legacy 无行" in text
        assert "mystery_plugin" in text
        assert "资源总数: 2" in text
        assert "已迁移: 1" in text
        assert "失败: 0" in text
        assert "可重复执行" in text
        assert "revision" in text


def _parse_dsn(url: str) -> dict[str, Any]:
    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def _run_bootstrap(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """在仓库根目录对指定数据库 URL 执行 orm_bootstrap 迁移命令。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "komari_bot.db.orm_bootstrap", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _chat_prompt_spec(module: Any) -> Any:
    """聊天 Prompt 资源的静态声明（脚本内唯一真源）。"""
    return next(
        spec
        for spec in module._RESOURCE_SPECS
        if spec.legacy_table == "komari_prompt_configs"
        and spec.key_value == "komari_chat"
    )


def _chat_prompt_declared_columns(module: Any) -> tuple[str, ...]:
    """脚本静态声明的聊天 Prompt 列（随 TSK-190 落地自然剔除旧列/加入新列）。

    不用硬编码新列名：查询列集合跟随脚本声明，head 表查询永远不会引用
    已删除列（脚本更新后即不再声明 ``output_instruction``）。
    """
    return tuple(_chat_prompt_spec(module).columns)


async def _column_names(
    connection: asyncpg.Connection,
    table_name: str,
) -> set[str]:
    """查询一张表当前的全部列名（用于删除验证，不引用已删除列）。"""
    rows = await connection.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = $1
        """,
        table_name,
    )
    return {str(row["column_name"]) for row in rows}


async def _recreate_scratch_database() -> dict[str, Any]:
    """重建本文件的一次性隔离库并返回其 asyncpg 连接参数。

    隔离库名 = 门控库名 + ``_legacycfg``；先 DROP（FORCE 断开残留
    连接）再 CREATE，重复执行幂等。门控用户需要 CREATEDB 权限。
    """
    base = _parse_dsn(POSTGRES_URL)
    scratch = {**base, "database": f"{base['database']}_legacycfg"}
    connection = await asyncpg.connect(**base)
    try:
        name = str(scratch["database"]).replace('"', '""')
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    return scratch


async def _drop_scratch_database(database: str) -> None:
    """删除一次性隔离库（finally 清理，重复删除安全）。"""
    base = _parse_dsn(POSTGRES_URL)
    connection = await asyncpg.connect(**base)
    try:
        name = database.replace('"', '""')
        await connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await connection.close()


@pytest.mark.skipif(
    not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL，跳过集成测试"
)
class TestMigrateLegacyConfigsIntegration:
    """真实 PostgreSQL 集成测试：legacy 行 → 新表内容 + 报告 + 幂等。

    每个用例在一次性隔离库内先 ``upgrade head`` 再执行，结束即删库。
    """

    async def _prepare_head_scratch(self) -> dict[str, Any]:
        scratch = await _recreate_scratch_database()
        url = urlparse(POSTGRES_URL)._replace(path=f"/{scratch['database']}").geturl()
        result = _run_bootstrap(url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        return scratch

    @pytest.mark.asyncio
    async def test_migrates_legacy_rows_and_is_idempotent(self) -> None:
        module = _load_script_module()
        updated_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        #: TSK-190 legacy komari_chat Prompt 迁移输入：JSONB insert 与
        #: “保留旧字段”断言共用同一真源，避免字面量转写不一致。
        legacy_chat_prompt = {
            "system_prompt": "你是小鞠知花。",
            "memory_ack": "好的。",
            "memory_ack_role": "assistant",
            "output_instruction": "输出正文。",
            "cot_prefix": "<think>\n",
            "cot_prefix_role": "assistant",
            "version": "1.0",
        }
        scratch = await self._prepare_head_scratch()
        conn = await asyncpg.connect(**scratch)
        try:
            await conn.execute(
                "INSERT INTO komari_plugin_configs"
                " (plugin_name, schema_name, config_data, version, revision,"
                " updated_at)"
                " VALUES ($1, $2, $3::jsonb, $4, $5, $6)",
                "user_data",
                "DynamicConfigSchema",
                json.dumps(
                    {
                        "plugin_enable": True,
                        "initial_favorability": 30,
                        "max_favorability_delta_per_reply": None,
                        "version": "1.0",
                        "last_updated": "2026-01-01",
                        "schema_name": "LegacyJsonConfig",
                    },
                    ensure_ascii=False,
                ),
                "1.0",
                7,
                updated_at,
            )
            await conn.execute(
                "INSERT INTO komari_plugin_configs"
                " (plugin_name, schema_name, config_data, version, revision,"
                " updated_at)"
                " VALUES ($1, $2, $3::jsonb, $4, $5, $6)",
                "sr",
                "DynamicConfigSchema",
                json.dumps(
                    {
                        "plugin_enable": False,
                        "sr_list": ["条", "串"],
                    },
                    ensure_ascii=False,
                ),
                "1.0",
                3,
                updated_at,
            )
            await conn.execute(
                "INSERT INTO komari_prompt_configs"
                " (resource_id, display_name, prompt_data, version, revision,"
                " updated_at)"
                " VALUES ($1, $2, $3::jsonb, $4, $5, $6)",
                "komari_chat",
                "Komari Chat Prompt",
                json.dumps(legacy_chat_prompt, ensure_ascii=False),
                "1.0",
                2,
                updated_at,
            )

            result = await module.migrate_legacy_configs(conn)
            reports = {r.spec.key_value: r for r in result.reports}

            user_data = reports["user_data"]
            assert user_data.migrated is True
            assert user_data.revision == 7
            assert set(user_data.dropped_keys) == {
                "version",
                "last_updated",
                "schema_name",
            }
            assert user_data.defaulted_keys == ["max_favorability_delta_per_reply"]

            row = await conn.fetchrow(
                "SELECT id, revision, updated_at, plugin_enable,"
                " initial_favorability, max_favorability_delta_per_reply"
                " FROM komari_user_data_config WHERE id = 1"
            )
            assert row["id"] == 1
            assert row["revision"] == 7
            assert row["updated_at"] == updated_at
            assert row["plugin_enable"] is True
            assert row["initial_favorability"] == 30
            # legacy 值为 null（视为缺失）且列 NOT NULL → 回退类型默认值 0
            assert row["max_favorability_delta_per_reply"] == 0

            sr_row = await conn.fetchrow(
                "SELECT revision, plugin_enable, user_whitelist,"
                " group_whitelist, sr_list, list_chunk_size, redis_db"
                " FROM komari_sr_config WHERE id = 1"
            )
            assert sr_row["revision"] == 3
            assert sr_row["plugin_enable"] is False
            assert sr_row["user_whitelist"] == []
            assert sr_row["group_whitelist"] == []
            assert sr_row["sr_list"] == ["条", "串"]
            assert sr_row["list_chunk_size"] == 0
            assert sr_row["redis_db"] == 0

            # ═══ TSK-190：聊天 Prompt 迁移语义 ═══
            # 脚本静态声明不再包含已删除列，并随新 Schema 宣告新行为列
            chat_spec = _chat_prompt_spec(module)
            assert REMOVED_PROMPT_FIELD not in chat_spec.columns, (
                "TSK-190 未实现：脚本不得再声明 output_instruction 列"
            )
            new_chat_columns = [
                column
                for column in chat_spec.columns
                if column not in LEGACY_KEPT_CHAT_PROMPT_COLUMNS
            ]
            assert new_chat_columns, (
                "TSK-190 未实现：脚本未声明任何新行为列"
            )

            chat_report = reports["komari_chat"]
            assert chat_report.migrated is True
            assert chat_report.revision == 2
            assert set(chat_report.dropped_keys) == {
                "version",
                REMOVED_PROMPT_FIELD,
            }, "legacy output_instruction 必须被丢弃，不并入任何新字段"
            assert set(LEGACY_KEPT_CHAT_PROMPT_COLUMNS) <= set(
                chat_report.migrated_keys
            ), "旧的其他自定义 Prompt 字段必须保留"
            assert set(chat_report.defaulted_keys) == set(new_chat_columns), (
                "新增行为列在 legacy 迁移阶段保持空值（待 seed 补齐）"
            )

            # head 表查询列集合跟随脚本声明：永不引用已删除列
            chat_select = ", ".join(
                ["id", "revision", "updated_at", *_chat_prompt_declared_columns(module)]
            )
            prompt_row = await conn.fetchrow(
                f"SELECT {chat_select} FROM komari_prompt_komari_chat WHERE id = 1"
            )
            assert prompt_row["id"] == 1
            assert prompt_row["revision"] == 2
            assert prompt_row["updated_at"] == updated_at
            assert prompt_row["system_prompt"] == legacy_chat_prompt["system_prompt"]
            assert prompt_row["memory_ack"] == legacy_chat_prompt["memory_ack"]
            assert prompt_row["memory_ack_role"] == legacy_chat_prompt["memory_ack_role"]
            assert prompt_row["cot_prefix"] == legacy_chat_prompt["cot_prefix"]
            assert prompt_row["cot_prefix_role"] == legacy_chat_prompt["cot_prefix_role"]
            for column in new_chat_columns:
                assert prompt_row[column] == "", (
                    f"新增行为列 {column} 在 legacy 迁移后必须为空（待 seed 补齐）"
                )
            # 显式删除验证：head 表已不存在旧列（TSK-190 未实现时为可解释 RED）
            head_columns = await _column_names(conn, "komari_prompt_komari_chat")
            assert REMOVED_PROMPT_FIELD not in head_columns, (
                "TSK-190 未实现：head 表仍存在 output_instruction 列"
            )

            text = module.render_report(result)
            assert "已迁移键" in text
            assert "丢弃弃用键" in text
            assert "落回默认值列" in text
            assert "user_data -> komari_user_data_config" in text
            assert "可重复执行" in text

            second = await module.migrate_legacy_configs(
                conn,
                specs=[
                    s
                    for s in module._RESOURCE_SPECS
                    if s.key_value
                    in (
                        "user_data",
                        "sr",
                        "komari_chat",
                    )
                ],
            )
            row_after = await conn.fetchrow(
                "SELECT id, revision, updated_at, plugin_enable,"
                " initial_favorability, max_favorability_delta_per_reply"
                " FROM komari_user_data_config WHERE id = 1"
            )
            sr_after = await conn.fetchrow(
                "SELECT revision, plugin_enable, user_whitelist,"
                " group_whitelist, sr_list, list_chunk_size, redis_db"
                " FROM komari_sr_config WHERE id = 1"
            )
            prompt_after = await conn.fetchrow(
                f"SELECT {chat_select} FROM komari_prompt_komari_chat WHERE id = 1"
            )
            assert dict(row_after) == dict(row)
            assert dict(sr_after) == dict(sr_row)
            assert dict(prompt_after) == dict(prompt_row)
            assert {r.spec.key_value: r.migrated for r in second.reports} == {
                "user_data": True,
                "sr": True,
                "komari_chat": True,
            }
        finally:
            await conn.close()
            await _drop_scratch_database(str(scratch["database"]))

    @pytest.mark.asyncio
    async def test_legacy_memory_vision_config_migrates_to_chat(self) -> None:
        """TSK-194：旧 komari_memory JSONB 视觉配置迁入 komari_chat_config。

        vision_tool_enabled 布尔双分支 → image_understanding_mode 枚举；
        8 项预算原样直写；旧开关与预算键对 memory 表为弃用键（head 无
        对应列）进入丢弃清单；联动断言脚本静态声明不再引用已删除列。
        """
        module = _load_script_module()
        updated_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
        budgets = {
            "vision_image_download_max_count": 6,
            "vision_image_download_max_bytes": 9 * 1024 * 1024,
            "vision_image_download_total_max_bytes": 21 * 1024 * 1024,
            "vision_image_download_max_pixels": 41_000_000,
            "vision_image_download_concurrency": 3,
            "vision_image_download_connect_timeout_seconds": 6.0,
            "vision_image_download_read_timeout_seconds": 31.0,
            "vision_image_download_total_timeout_seconds": 46.0,
        }
        scratch = await self._prepare_head_scratch()
        conn = await asyncpg.connect(**scratch)
        try:
            await conn.execute(
                "INSERT INTO komari_plugin_configs"
                " (plugin_name, schema_name, config_data, version, revision,"
                " updated_at)"
                " VALUES ($1, $2, $3::jsonb, $4, $5, $6)",
                "komari_memory",
                "DynamicConfigSchema",
                json.dumps(
                    {
                        "vision_tool_enabled": True,
                        **budgets,
                    },
                    ensure_ascii=False,
                ),
                "1.0",
                9,
                updated_at,
            )

            result = await module.migrate_legacy_configs(conn)
            reports = {r.spec.key_value: r for r in result.reports}

            memory_report = reports["komari_memory"]
            assert memory_report.migrated is True
            assert set(memory_report.dropped_keys) == {
                "version",
                "last_updated",
                "schema_name",
                "vision_tool_enabled",
                *budgets.keys(),
            }, "旧视觉开关与预算键对 memory 表无对应列，必须全部丢弃"

            chat_row = await conn.fetchrow(
                "SELECT image_understanding_mode,"
                " vision_image_download_max_count,"
                " vision_image_download_max_bytes,"
                " vision_image_download_total_max_bytes,"
                " vision_image_download_max_pixels,"
                " vision_image_download_concurrency,"
                " vision_image_download_connect_timeout_seconds,"
                " vision_image_download_read_timeout_seconds,"
                " vision_image_download_total_timeout_seconds"
                " FROM komari_chat_config WHERE id = 1"
            )
            assert chat_row["image_understanding_mode"] == "delegated"
            for column, expected in budgets.items():
                assert chat_row[column] == expected, f"预算列 {column} 迁移失真"

            # head 表已无 memory 旧视觉列；脚本声明物也须一致
            memory_columns = await _column_names(conn, "komari_memory_config")
            assert "vision_tool_enabled" not in memory_columns
            memory_spec = next(
                s for s in module._RESOURCE_SPECS if s.key_value == "komari_memory"
            )
            assert "vision_tool_enabled" not in memory_spec.columns
            assert not {"vision_image_download_*"} & set(
                memory_spec.columns
            ), "memory 资源不得声明任何旧视觉字段"

            # 双分支：更新同一 legacy 行 vision_tool_enabled=false → native
            await conn.execute(
                "UPDATE komari_plugin_configs SET config_data = $1::jsonb,"
                " revision = 10 WHERE plugin_name = 'komari_memory'",
                json.dumps({"vision_tool_enabled": False}, ensure_ascii=False),
            )
            await module.migrate_legacy_configs(conn)
            native_row = await conn.fetchrow(
                "SELECT image_understanding_mode FROM komari_chat_config WHERE id = 1"
            )
            assert native_row["image_understanding_mode"] == "native"
            # 缺键预算列不覆盖已播种值（UPDATE 路径不写缺键列）
            budget_after = await conn.fetchrow(
                "SELECT vision_image_download_max_count,"
                " vision_image_download_total_timeout_seconds"
                " FROM komari_chat_config WHERE id = 1"
            )
            assert budget_after["vision_image_download_max_count"] == 6
            assert budget_after["vision_image_download_total_timeout_seconds"] == 46.0
        finally:
            await conn.close()
            await _drop_scratch_database(str(scratch["database"]))

    @pytest.mark.asyncio
    async def test_seeded_new_table_preserves_values_for_missing_keys(
        self,
    ) -> None:
        """应用启动已播种的新表：缺键列保持播种值，不被默认值覆盖。"""
        module = _load_script_module()
        scratch = await self._prepare_head_scratch()
        conn = await asyncpg.connect(**scratch)
        try:
            # 模拟应用启动播种（insert_if_absent）
            await conn.execute(
                "INSERT INTO komari_user_data_config"
                " (id, revision, updated_at, plugin_enable,"
                " initial_favorability, max_favorability_delta_per_reply)"
                " VALUES (1, 100, $1, false, 99, 77)",
                datetime(2025, 1, 1, tzinfo=UTC),
            )
            # legacy 行只含 plugin_enable：其余键缺失
            await conn.execute(
                "INSERT INTO komari_plugin_configs"
                " (plugin_name, schema_name, config_data, version, revision,"
                " updated_at) VALUES ($1, $2, $3::jsonb, $4, $5, $6)",
                "user_data",
                "DynamicConfigSchema",
                json.dumps({"plugin_enable": True}),
                "1.0",
                3,
                datetime(2026, 2, 2, tzinfo=UTC),
            )

            result = await module.migrate_legacy_configs(
                conn,
                specs=[s for s in module._RESOURCE_SPECS if s.key_value == "user_data"],
            )
            assert result.reports[0].migrated is True
            assert result.reports[0].defaulted_keys == [
                "initial_favorability",
                "max_favorability_delta_per_reply",
            ]

            row = await conn.fetchrow(
                "SELECT revision, updated_at, plugin_enable,"
                " initial_favorability, max_favorability_delta_per_reply"
                " FROM komari_user_data_config WHERE id = 1"
            )
            assert row["revision"] == 3  # 继承 legacy revision
            assert row["updated_at"] == datetime(2026, 2, 2, tzinfo=UTC)
            assert row["plugin_enable"] is True  # legacy 值覆盖
            assert row["initial_favorability"] == 99  # 播种值保留
            assert row["max_favorability_delta_per_reply"] == 77  # 播种值保留
        finally:
            await conn.close()
            await _drop_scratch_database(str(scratch["database"]))
