"""TSK-182 判定 Prompt 可选初始数据 —— PostgreSQL 集成验收。

依赖已执行 ``alembic upgrade head`` 的迁移管理 schema
（``KOMARI_TEST_POSTGRES_URL`` 门控，且与 ``SQLALCHEMY_DATABASE_URL``
同库，否则按既有约定 skip）。

隔离纪律：用例在门控 DSN 派生的一次性隔离库（库名后缀
``_tsk182_decision_seed*``）内执行，用例结束即 DROP；共享门控库的版本与
数据不受影响。用例之间各自重建唯一的隔离库，重复运行与并行安全。

覆盖清单（对应 TSK-182 可选资源切片）：
- 全新数据库 upgrade head（0027）后运行真实播种命令：三个必需 Prompt
  资源齐全，另加可选判定 Prompt 单行（值等于打包资产）；场景初始化已
  退役，4 张退役场景表不得存在；
- 重复初始化：判定 Prompt 正文/revision/updated_at 不变；
- 部分已有：非空自定义字段保留，只按共享 helper 补齐空白字段；
- 可选资产缺失/畸形：既有自定义判定行原样保留，必需资源照常初始化；
- 全新库 + 可选资产缺失：必需资源仍完整初始化，判定行不写入；
- 可选写入的真实 SQL 失败（临时触发器让写入后的校验失败）：必需资源已
  提交、可选部分回滚、命令成功退出且只输出固定安全诊断，无正文/回溯泄漏；
- 可选租约的真实归还失败（外部注入在共享连接池边界，仅作用于观察到判定
  Prompt 查询的那次租约，且在底层真实 acquire 上下文结束后才报错）：必需
  初始化仍成功、诊断固定、无原始异常/正文泄漏；
- 必需 manifest 缺 Prompt 块：按既有规则失败且不写入任何数据。

期望值一律来自打包资产（必需资源经 ``tsk197_gate_support.asset_values``，
可选资源按契约的显式 ``decision_prompt`` 键读取），不复制生产正文。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml

from komari_bot.config.typed_config import ensure_typed_prompt_model
from komari_bot.db.seed_bootstrap import DEFAULT_DECISION_SEED_FILE, DEFAULT_SEED_FILE
from tests.config.prompt_field_contract import (
    PROMPT_RESOURCE_IDS,
    prompt_resource_field_names,
)
from tests.db.tsk197_gate_support import (
    POSTGRES_URL,
    SKIP_NO_POSTGRES,
    SQLALCHEMY_URL,
    drop_scratch_database,
    fetch_prompt_row,
    recreate_scratch_database,
    run_bootstrap,
    same_database,
    scratch_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI_MODULE = "komari_bot.db.seed_bootstrap"

DECISION_RESOURCE_ID = "komari_decision"

#: 可选资源不可用/未能确认完成时的唯一固定安全诊断（与单元测试同一冻结
#: 文案；提交后发生的归还失败不等同于写入被撤销，因此文案不作回滚断言）。
OPTIONAL_UNAVAILABLE_MESSAGE = "判定 Prompt 初始化未能确认完成。"

#: 放进可选资产正文里的哨兵：诊断与回溯绝不能回显它。
SENTINEL = "TSK182-DECISION-BODY-SENTINEL-91a4"

#: 合成“连接归还失败”异常里的哨兵：失败路径不得将它回显给用户。
RELEASE_SENTINEL = "TSK182-RELEASE-SENTINEL-4c1d"

#: 当前迁移 head（TSK-192 phase B 新增 chat/context 配置所有权后为 0027）。
TSK182_HEAD_REVISION = "0027"

#: 已退役的 4 张场景表（test-owned 字面量，来自 TSK-190 manifest）。
RETIRED_SCENE_TABLES: tuple[str, ...] = (
    "komari_memory_scene_item",
    "komari_memory_scene_runtime",
    "komari_decision_scenes",
    "komari_memory_scene_set",
)

#: 预置判定行的历史时间戳（验证未被改写）。
LEGACY_UPDATED_AT = datetime(2024, 1, 1, tzinfo=UTC)

#: Prompt 强类型表继承自 ``TypedPromptModel`` 的存储专用字段（非正文）。
PROMPT_STORAGE_FIELDS = frozenset({"id", "revision", "updated_at"})

#: 外部故障注入驱动：只包装共享连接池边界，仍然调用真实的
#: ``seed_bootstrap.main``（不重实现 CLI），也不 patch 任何播种/校验/业务
#: 逻辑。代理把每条 SQL 原样转发给真实 asyncpg 连接；仅当租约实际观察到
#: 判定 Prompt 查询、且底层真实 ``acquire`` 上下文已退出（连接已归还真实
#: 池）之后，才抛出带哨兵的合成归还异常。
RELEASE_PROBE_DRIVER = '''
"""外部故障注入驱动（集成测试写到临时目录后以子进程运行）。"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from komari_bot.config.typed_config import ensure_typed_prompt_model
from komari_bot.db import orm_connection

SENTINEL = os.environ["TSK182_RELEASE_SENTINEL"]

DECISION_TABLE = str(ensure_typed_prompt_model("komari_decision").__tablename__)

_real_get_pool = orm_connection.get_shared_orm_connection_pool


class SyntheticReleaseError(RuntimeError):
    """模拟可选租约归还（release）阶段的外部故障。"""


class _ProbeConnection:
    """真实 asyncpg 连接的透明代理：只观察判定 Prompt 查询，其余原样转发。"""

    def __init__(self, raw: Any, observed: list[str]) -> None:
        self._raw = raw
        self._observed = observed

    def _note(self, sql: object) -> None:
        # 只记录**第一条**实际发往判定 Prompt 表的查询（即该租约确实观察到了
        # 可选查询），不改变任何 SQL 语义。
        if DECISION_TABLE in str(sql) and not self._observed:
            self._observed.append(str(sql))

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        return await self._raw.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        return await self._raw.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        return await self._raw.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any) -> Any:
        self._note(sql)
        return await self._raw.execute(sql, *args)

    def transaction(self, *args: Any, **kwargs: Any) -> Any:
        return self._raw.transaction(*args, **kwargs)


class _ProbePool:
    """共享连接池代理：只包装 ``acquire``，其余属性转发真实池。"""

    def __init__(self, real: Any) -> None:
        self._real = real

    @asynccontextmanager
    async def acquire(self):
        observed: list[str] = []
        async with self._real.acquire() as raw:
            yield _ProbeConnection(raw, observed)
        # 底层真实 acquire 上下文已退出（连接已归还真实池）之后才注入故障。
        if observed:
            raise SyntheticReleaseError(f"release failed [{SENTINEL}]")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _probe_pool() -> _ProbePool:
    return _ProbePool(_real_get_pool())


orm_connection.get_shared_orm_connection_pool = _probe_pool

from komari_bot.db import seed_bootstrap  # noqa: E402

sys.exit(seed_bootstrap.main(sys.argv[1:]) or 0)
'''

pytestmark = [
    SKIP_NO_POSTGRES,
    pytest.mark.skipif(
        not same_database(POSTGRES_URL, SQLALCHEMY_URL),
        reason="KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致",
    ),
]


def _decision_schema_fields() -> set[str]:
    """``komari_decision`` Prompt 正文列（真实 Schema，非复制 oracle）。"""
    model = ensure_typed_prompt_model(DECISION_RESOURCE_ID)
    assert model is not None, "无法加载 komari_decision Prompt 强类型 Schema"
    return set(model.model_fields) - PROMPT_STORAGE_FIELDS


def _decision_table_name() -> str:
    """判定 Prompt 强类型表名（来自 Schema，不硬编码）。"""
    model = ensure_typed_prompt_model(DECISION_RESOURCE_ID)
    assert model is not None, "无法加载 komari_decision Prompt 强类型 Schema"
    return str(model.__tablename__)


DECISION_TABLE = _decision_table_name()


def _decision_asset_values() -> dict[str, str]:
    """打包可选资产中的判定正文（契约固定形状的显式 ``decision_prompt`` 键）。"""
    raw = yaml.safe_load(
        DEFAULT_DECISION_SEED_FILE.read_text(encoding="utf-8")
    ) or {}
    section = raw.get("decision_prompt")
    assert isinstance(section, dict), (
        "判定初始数据必须包含 decision_prompt 对象块"
    )
    return {str(field): str(value) for field, value in section.items()}


def _run_seed_cli(
    url: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """以公开 CLI 形式执行播种命令（与 prestart / 本地 / CI 同一命令）。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _run_release_probe_cli(
    url: str,
    driver: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """以子进程运行故障注入驱动（真实 ``seed_bootstrap.main`` + 真实库）。"""
    env = os.environ.copy()
    env["SQLALCHEMY_DATABASE_URL"] = url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["TSK182_RELEASE_SENTINEL"] = RELEASE_SENTINEL
    return subprocess.run(
        [sys.executable, str(driver), *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout}\n{result.stderr}"


async def _prepare_head_scratch(
    suffix: str,
) -> tuple[dict[str, Any], str]:
    """重建唯一后缀的隔离库并 upgrade head；返回 (asyncpg 连接参数, URL)。"""
    scratch = await recreate_scratch_database(suffix)
    url = scratch_url(str(scratch["database"]))
    result = run_bootstrap(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return scratch, url


async def _fetch_decision_row(
    connection: asyncpg.Connection,
) -> dict[str, Any] | None:
    row = await connection.fetchrow(
        f"SELECT * FROM {DECISION_TABLE} WHERE id = 1"
    )
    return None if row is None else dict(row)


async def _insert_decision_row(
    connection: asyncpg.Connection,
    values: dict[str, str],
    *,
    revision: int,
) -> None:
    """按强类型 Schema 当前字段集插入判定单行（列集合运行时推导）。"""
    fields = sorted(values)
    columns_sql = ", ".join(["id", "revision", "updated_at", *fields])
    placeholders = ", ".join(f"${index}" for index in range(1, 4 + len(fields)))
    await connection.execute(
        f"INSERT INTO {DECISION_TABLE} ({columns_sql}) VALUES ({placeholders})",
        1,
        revision,
        LEGACY_UPDATED_AT,
        *[values[field] for field in fields],
    )


async def _public_tables(connection: asyncpg.Connection) -> set[str]:
    rows = await connection.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    return {str(row["tablename"]) for row in rows}


async def _assert_retired_tables_absent(connection: asyncpg.Connection) -> None:
    """场景初始化已退役：4 张退役场景表在播种前后都不得存在。"""
    present = await _public_tables(connection)
    retired = set(RETIRED_SCENE_TABLES) & present
    assert not retired, f"播种不得重建已退役的场景表: {sorted(retired)}"


async def _assert_required_prompt_rows_complete(
    connection: asyncpg.Connection,
) -> None:
    """三个必需 Prompt 行存在且全字段非空（可选资源不得影响其完成度）。"""
    for resource_id in PROMPT_RESOURCE_IDS:
        row = await fetch_prompt_row(connection, resource_id)
        assert row is not None, f"seed 后 {resource_id} Prompt 单行必须存在"
        for field in sorted(prompt_resource_field_names(resource_id)):
            assert str(row[field] or "").strip(), (
                f"{resource_id} Prompt 字段 {field} 不得为空"
            )


def _write_decision_seed_file(
    path: Path,
    values: dict[str, str],
) -> Path:
    """把测试自有的判定正文写成契约固定形状的可选资产文件。"""
    document = {"version": "1", "decision_prompt": values}
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _sentinel_values() -> dict[str, str]:
    """测试自有的完整判定正文 marker（含哨兵，不复制生产正文）。"""
    return {
        field: f"marker-{field}-{SENTINEL}"
        for field in sorted(_decision_schema_fields())
    }


def _remove_key(node: object, key: str) -> bool:
    """递归删除文档中第一个指定键（不锁定 manifest 布局）。"""
    if isinstance(node, dict):
        mapping: dict[str, Any] = node
        if key in mapping:
            del mapping[key]
            return True
        return any(_remove_key(value, key) for value in mapping.values())
    if isinstance(node, list):
        items: list[Any] = node
        return any(_remove_key(value, key) for value in items)
    return False


async def test_fresh_database_seeds_required_and_decision_prompt() -> None:
    """head 新库：真实播种写入必需资源 + 可选判定单行，值等于打包资产。"""
    scratch, url = await _prepare_head_scratch("_tsk182_decision_seed_fresh")
    try:
        result = _run_seed_cli(url)
        output = _combined(result)
        assert result.returncode == 0, output
        assert OPTIONAL_UNAVAILABLE_MESSAGE not in output, (
            "打包的可选判定资产合法，不得产生跳过诊断"
        )

        connection = await asyncpg.connect(**scratch)
        try:
            version = await connection.fetchval(
                "SELECT version_num FROM alembic_version"
            )
            assert version == TSK182_HEAD_REVISION, (
                f"隔离库必须位于迁移 head {TSK182_HEAD_REVISION}，实际 {version}"
            )

            await _assert_required_prompt_rows_complete(connection)

            asset = _decision_asset_values()
            schema_fields = _decision_schema_fields()
            row = await _fetch_decision_row(connection)
            assert row is not None, "seed 后判定 Prompt 单行必须存在"
            assert set(row) - PROMPT_STORAGE_FIELDS == schema_fields
            for field in sorted(schema_fields):
                assert str(row[field] or "").strip(), (
                    f"判定 Prompt 字段 {field} 初始值不得为空"
                )
                assert row[field] == asset[field], (
                    f"判定 Prompt 字段 {field} 必须等于打包资产值"
                )
            assert row["revision"] == 1

            await _assert_retired_tables_absent(connection)
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_repeat_initialization_keeps_decision_prompt_unchanged() -> None:
    """重复初始化：判定 Prompt 正文、revision 与时间戳都不变化。"""
    scratch, url = await _prepare_head_scratch("_tsk182_decision_seed_repeat")
    try:
        result = _run_seed_cli(url)
        assert result.returncode == 0, _combined(result)

        connection = await asyncpg.connect(**scratch)
        try:
            before = await _fetch_decision_row(connection)
            assert before is not None

            result = _run_seed_cli(url)
            assert result.returncode == 0, _combined(result)

            after = await _fetch_decision_row(connection)
            assert after == before, (
                "重复播种不得改写判定 Prompt 正文、revision 或 updated_at"
            )
            assert after is not None
            assert after["revision"] == 1
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_initialization_preserves_custom_field_and_fills_blank_only() -> None:
    """部分已有：非空自定义字段保留，只补齐空/空白的判定字段（共享 helper）。"""
    scratch, url = await _prepare_head_scratch("_tsk182_decision_seed_partial")
    try:
        fields = sorted(_decision_schema_fields())
        custom_field = fields[0]
        blank_field = fields[1]
        empty_fields = fields[2:]
        custom_text = f"custom-{custom_field}-{SENTINEL}"
        values = {
            field: (
                custom_text
                if field == custom_field
                else ("   " if field == blank_field else "")
            )
            for field in fields
        }

        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_decision_row(connection, values, revision=7)

            result = _run_seed_cli(url)
            assert result.returncode == 0, _combined(result)

            row = await _fetch_decision_row(connection)
            assert row is not None
            assert row[custom_field] == custom_text, (
                "种子绝不覆盖非空自定义判定字段"
            )
            asset = _decision_asset_values()
            for field in [blank_field, *empty_fields]:
                assert row[field] == asset[field], (
                    f"种子应按共享 helper 补齐空的判定字段 {field}"
                )
            assert row["revision"] == 8, "补齐空字段应只递增一次 revision"
            assert row["updated_at"] > LEGACY_UPDATED_AT

            before = dict(row)
            result = _run_seed_cli(url)
            assert result.returncode == 0, _combined(result)
            after = await _fetch_decision_row(connection)
            assert after == before, "补齐后重复播种不得产生任何变化"
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_missing_or_invalid_decision_file_keeps_existing_custom_row(
    tmp_path: Path,
) -> None:
    """可选资产缺失/畸形：既有非空自定义判定行原样保留，必需资源照常初始化。"""
    scratch, url = await _prepare_head_scratch("_tsk182_decision_seed_keepcustom")
    try:
        custom_values = {
            field: f"custom-{field}-{SENTINEL}"
            for field in sorted(_decision_schema_fields())
        }
        connection = await asyncpg.connect(**scratch)
        try:
            await _insert_decision_row(connection, custom_values, revision=5)
            before = await _fetch_decision_row(connection)
            assert before is not None

            missing_path = tmp_path / "absent-decision.yaml"
            result = _run_seed_cli(
                url,
                "--decision-seed-file",
                str(missing_path),
            )
            output = _combined(result)
            assert result.returncode == 0, output
            assert output.count(OPTIONAL_UNAVAILABLE_MESSAGE) == 1, output
            assert SENTINEL not in output, "诊断不得回显判定正文"
            assert await _fetch_decision_row(connection) == before, (
                "可选资产缺失不得改动既有自定义判定行"
            )

            malformed_path = tmp_path / "malformed-decision.yaml"
            malformed_path.write_text(
                f'version: "1"\ndecision_prompt: ["{SENTINEL}", unclosed\n',
                encoding="utf-8",
            )
            result = _run_seed_cli(
                url,
                "--decision-seed-file",
                str(malformed_path),
            )
            output = _combined(result)
            assert result.returncode == 0, output
            assert output.count(OPTIONAL_UNAVAILABLE_MESSAGE) == 1, output
            assert SENTINEL not in output, "诊断不得回显畸形可选资产内容"
            assert "Traceback" not in output, output
            assert await _fetch_decision_row(connection) == before, (
                "畸形可选资产不得改动既有自定义判定行"
            )

            await _assert_required_prompt_rows_complete(connection)
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_missing_decision_file_on_fresh_database_still_seeds_required(
    tmp_path: Path,
) -> None:
    """全新库 + 可选资产缺失：必需资源完整初始化，判定行不写入。"""
    scratch, url = await _prepare_head_scratch("_tsk182_decision_seed_missing")
    try:
        missing_path = tmp_path / "absent-decision.yaml"
        result = _run_seed_cli(
            url,
            "--decision-seed-file",
            str(missing_path),
        )
        output = _combined(result)
        assert result.returncode == 0, output
        assert output.count(OPTIONAL_UNAVAILABLE_MESSAGE) == 1, output

        connection = await asyncpg.connect(**scratch)
        try:
            await _assert_required_prompt_rows_complete(connection)
            await _assert_retired_tables_absent(connection)
            assert await _fetch_decision_row(connection) is None, (
                "可选资产缺失时不得写入判定 Prompt 行"
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_optional_write_failure_rolls_back_only_optional_work(
    tmp_path: Path,
) -> None:
    """可选写入后的真实 SQL 层失败：必需资源已提交、可选回滚、命令成功。

    触发器在 INSERT 时把首个正文字段置空，让写入后的共享校验失败：这会
    撤掉同一独立事务里已经写入的可选行，但不得影响已提交的必需资源。
    """
    scratch, url = await _prepare_head_scratch("_tsk182_decision_seed_rollback")
    try:
        fields = sorted(_decision_schema_fields())
        blanked_field = fields[0]
        decision_path = _write_decision_seed_file(
            tmp_path / "decision-with-sentinel.yaml",
            _sentinel_values(),
        )

        connection = await asyncpg.connect(**scratch)
        try:
            await connection.execute(
                f"""
                CREATE FUNCTION tsk182_blank_decision_prompt()
                RETURNS trigger AS $$
                BEGIN
                    NEW.{blanked_field} := '';
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            await connection.execute(
                f"""
                CREATE TRIGGER tsk182_blank_decision_prompt_trigger
                BEFORE INSERT ON {DECISION_TABLE}
                FOR EACH ROW EXECUTE FUNCTION tsk182_blank_decision_prompt();
                """
            )

            result = _run_seed_cli(
                url,
                "--decision-seed-file",
                str(decision_path),
            )
            output = _combined(result)
            assert result.returncode == 0, output
            assert output.count(OPTIONAL_UNAVAILABLE_MESSAGE) == 1, output
            assert SENTINEL not in output, "可选失败诊断不得回显判定正文"
            assert "Traceback" not in output, output
            assert blanked_field not in output, (
                "可选失败诊断不得回显字段级校验细节"
            )

            await _assert_required_prompt_rows_complete(connection)
            await _assert_retired_tables_absent(connection)
            assert await _fetch_decision_row(connection) is None, (
                "可选独立事务必须回滚：失败后不得留下部分写入的判定行"
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_optional_lease_release_failure_after_mandatory_commit_is_isolated(
    tmp_path: Path,
) -> None:
    """可选租约归还失败（提交后）：必需初始化仍成功，诊断固定、无异常外泄。

    真实隔离库 + 真实 CLI 入口，只在共享连接池边界注入外部故障：实际
    观察到判定 Prompt 查询的那次租约在底层真实 acquire 上下文结束之后
    抛出带哨兵的合成归还异常（真实 SQL 与事务不变）。可选阶段属于可选
    错误边界，因此不得让已提交并校验通过的必需初始化变成失败，也不得
    把原始异常回显给用户。
    """
    scratch, url = await _prepare_head_scratch("_tsk182_decision_seed_release")
    try:
        driver = tmp_path / "release_probe_driver.py"
        driver.write_text(RELEASE_PROBE_DRIVER, encoding="utf-8")
        decision_path = _write_decision_seed_file(
            tmp_path / "decision-with-sentinel.yaml",
            _sentinel_values(),
        )

        result = _run_release_probe_cli(
            url,
            driver,
            "--decision-seed-file",
            str(decision_path),
        )
        output = _combined(result)

        # 必需初始化必须已经提交且完整（未修复时也同样成立：这正是被归还
        # 失败掩盖的必需成果）。先验证这一点，再验证命令与诊断。
        connection = await asyncpg.connect(**scratch)
        try:
            try:
                await _assert_required_prompt_rows_complete(connection)
                await _assert_retired_tables_absent(connection)
            except AssertionError as exc:
                pytest.fail(f"{exc}\n--- CLI 输出 ---\n{output}")
            # 提交后发生的归还失败不证明可选写入被撤销：不据此断言判定行
            # 缺失，也不回滚/删除已经持久化的可选行。
        finally:
            await connection.close()

        assert result.returncode == 0, output
        assert output.count(OPTIONAL_UNAVAILABLE_MESSAGE) == 1, output
        assert RELEASE_SENTINEL not in output, "归还失败的原始异常不得外泄"
        assert SENTINEL not in output, "可选失败诊断不得回显判定正文"
        assert "Traceback" not in output, output
    finally:
        await drop_scratch_database(str(scratch["database"]))


async def test_missing_required_prompt_block_still_fails_without_writes(
    tmp_path: Path,
) -> None:
    """必需 manifest 缺 Prompt 块：按既有规则失败且不写入任何数据。"""
    scratch, url = await _prepare_head_scratch("_tsk182_decision_seed_mandatory")
    try:
        raw = yaml.safe_load(DEFAULT_SEED_FILE.read_text(encoding="utf-8")) or {}
        assert _remove_key(raw, "planning_system_prompt"), (
            "默认 manifest 必须包含群总结 Prompt 判别键，夹具才有效"
        )
        bad_manifest = tmp_path / "missing-group-prompt.yaml"
        bad_manifest.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        decision_path = _write_decision_seed_file(
            tmp_path / "decision-valid.yaml",
            _sentinel_values(),
        )

        result = _run_seed_cli(
            url,
            "--seed-file",
            str(bad_manifest),
            "--decision-seed-file",
            str(decision_path),
        )
        output = _combined(result)
        assert result.returncode != 0, output
        assert "缺少 Prompt 初始数据块" in output, output
        assert "group_history_summary" in output, output

        connection = await asyncpg.connect(**scratch)
        try:
            for resource_id in PROMPT_RESOURCE_IDS:
                assert await fetch_prompt_row(connection, resource_id) is None, (
                    f"必需门禁失败时不得写入 {resource_id} Prompt 行"
                )
            assert await _fetch_decision_row(connection) is None, (
                "必需门禁失败时不得写入可选判定 Prompt 行"
            )
            await _assert_retired_tables_absent(connection)
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
