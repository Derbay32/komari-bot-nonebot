"""验证新判定存储的迁移边界，不调用模型或生产服务。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from komari_bot.config.typed_config import ensure_typed_config_model
from tests.db.tsk190_legacy_config_support import (
    RETAINED_DECISION_CONFIG_FIELDS,
    STORAGE_COLUMNS,
    historical_decision_config_row,
)
from tests.db.tsk197_gate_support import (
    POSTGRES_URL,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    same_database,
    scratch_url,
)

SQLALCHEMY_URL = os.getenv("SQLALCHEMY_DATABASE_URL", "")
NEW_TABLES = {
    "komari_prompt_komari_decision",
    "komari_decision_processes",
    "komari_decision_scheduler",
    "komari_decision_groups",
    "komari_decision_dispatches",
    "komari_decision_logs",
}
EXECUTION_DEFAULTS = {
    "max_concurrent_decisions": 4,
    "max_queued_messages_per_group": 5,
    "queue_timeout_seconds": 3.0,
    "reference_timeout_seconds": 3.0,
    "optional_reply_probability": 0.5,
    "decision_log_retention_days": 7,
}
#: TSK-190（0025）之后判定配置表的列集合：3 个存储字段 + 13 个保留字段。
SURVIVING_CONFIG_COLUMNS = frozenset(
    (*STORAGE_COLUMNS, *RETAINED_DECISION_CONFIG_FIELDS)
)


@pytest.mark.skipif(not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL")
@pytest.mark.parametrize("existing_row", [False, True])
@pytest.mark.asyncio
async def test_decision_storage_upgrade_downgrade_preserves_existing_configuration(
    *,
    existing_row: bool,
) -> None:
    """新库和存量库均能升级；显式关闭、修订及旧字段在往返迁移后保持不变。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")
    scratch = await recreate_scratch_database(f"_jev182_{uuid4().hex[:10]}")
    database_url = scratch_url(str(scratch["database"]))
    try:
        upgraded = run_bootstrap(database_url, "upgrade", "0022")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            previous_columns = {
                row["column_name"]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='komari_decision_config'"
                )
            }
            previous = None
            if existing_row:
                # 0024 时代表结构（含 0025 才退役的 38 列）必须用显式历史
                # 测试数据写入；生产 Schema 已不再声明这些字段。
                values = historical_decision_config_row(
                    plugin_enable=False,
                    bot_aliases=["保留显式关闭"],
                    jev_model="jev-1.14.0",
                    revision=23,
                )
                await connection.execute(
                    "INSERT INTO komari_decision_config "
                    "SELECT * FROM json_populate_record(NULL::komari_decision_config, $1::json)",
                    json.dumps(values),
                )
                record = await connection.fetchrow(
                    "SELECT * FROM komari_decision_config WHERE id=1"
                )
                assert record is not None
                previous = dict(record)
        finally:
            await connection.close()

        # TSK-190：中间升级钉在 0024（0025 不可逆，之后不能再 downgrade）
        upgraded = run_bootstrap(database_url, "upgrade", "0024")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            tables = {
                row["table_name"]
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                )
            }
            assert tables >= NEW_TABLES
            retained = await connection.fetchrow(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='komari_decision_dispatches' "
                "AND column_name='recent_retained'"
            )
            assert retained is not None
            assert retained["data_type"] == "boolean"
            assert retained["is_nullable"] == "YES"
            assert retained["column_default"] is None
            columns = {
                row["column_name"]: row
                for row in await connection.fetch(
                    "SELECT column_name, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='komari_decision_config'"
                )
            }
            assert set(columns) == previous_columns | EXECUTION_DEFAULTS.keys()
            for name in EXECUTION_DEFAULTS:
                assert columns[name]["is_nullable"] == "NO"
                assert columns[name]["column_default"] is None
            record = await connection.fetchrow(
                "SELECT * FROM komari_decision_config WHERE id=1"
            )
            if previous is None:
                assert record is None
            else:
                assert record is not None
                assert {name: record[name] for name in previous} == previous
                assert {
                    name: record[name] for name in EXECUTION_DEFAULTS
                } == EXECUTION_DEFAULTS
                assert record["plugin_enable"] is False
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM komari_prompt_komari_decision"
                )
                == 0
            )
        finally:
            await connection.close()

        downgraded = run_bootstrap(database_url, "downgrade", "0022")
        assert downgraded.returncode == 0, downgraded.stdout + downgraded.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            tables = {
                row["table_name"]
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                )
            }
            assert not NEW_TABLES & tables
            record = await connection.fetchrow(
                "SELECT * FROM komari_decision_config WHERE id=1"
            )
            assert (None if record is None else dict(record)) == previous
        finally:
            await connection.close()
        upgraded = run_bootstrap(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            # 0025 之后只剩存储字段与保留字段，退役列在最终 head 上必须消失
            tail_columns = {
                row["column_name"]
                for row in await connection.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='komari_decision_config'"
                )
            }
            assert tail_columns == SURVIVING_CONFIG_COLUMNS
            tail_record = await connection.fetchrow(
                "SELECT * FROM komari_decision_config WHERE id=1"
            )
            if previous is None:
                assert tail_record is None
            else:
                assert tail_record is not None
                # previous 抓自 0022（尚无执行配置列），只比对两代共有列；执行
                # 列在 0024 边界已断言为 0023 的默认值，这里确认 0025 未改写。
                comparable = [
                    name
                    for name in sorted(SURVIVING_CONFIG_COLUMNS)
                    if name in previous
                ]
                assert {name: tail_record[name] for name in comparable} == {
                    name: previous[name] for name in comparable
                }
                assert {
                    name: tail_record[name] for name in EXECUTION_DEFAULTS
                } == EXECUTION_DEFAULTS
                assert tail_record["plugin_enable"] is False
        finally:
            await connection.close()
        checked = run_bootstrap(database_url, "check")
        assert checked.returncode == 0, checked.stdout + checked.stderr
    finally:
        await drop_scratch_database(str(scratch["database"]))


@pytest.mark.skipif(not POSTGRES_URL, reason="未设置 KOMARI_TEST_POSTGRES_URL")
@pytest.mark.asyncio
async def test_disable_transition_fences_unhanded_work_without_freeing_live_slots() -> (
    None
):
    """关闭再开启仍撤销旧资格，但保留在途名额和已交接的业务事实。"""
    if not same_database(POSTGRES_URL, SQLALCHEMY_URL):
        pytest.skip("KOMARI_TEST_POSTGRES_URL 与 SQLALCHEMY_DATABASE_URL 不一致")
    scratch = await recreate_scratch_database(f"_jev182f_{uuid4().hex[:10]}")
    database_url = scratch_url(str(scratch["database"]))
    try:
        upgraded = run_bootstrap(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            schema = ensure_typed_config_model("komari_decision")
            assert schema is not None
            values = schema(plugin_enable=True).model_dump(mode="json")
            values.update(id=1, revision=1, updated_at=datetime.now(UTC).isoformat())
            await connection.execute(
                "INSERT INTO komari_decision_config "
                "SELECT * FROM json_populate_record(NULL::komari_decision_config, $1::json)",
                json.dumps(values),
            )
            await connection.execute(
                "INSERT INTO komari_decision_scheduler (id,pseudonym_salt) VALUES (1,$1)",
                b"x" * 32,
            )
            owner = await connection.fetchval(
                "INSERT INTO komari_decision_processes DEFAULT VALUES RETURNING id"
            )
            for index, state in enumerate(
                ["queued", "running", "success", "bypass", "failed", "success"]
            ):
                await connection.execute(
                    "INSERT INTO komari_decision_dispatches "
                    "(decision_id,protocol,account_id,group_id,message_id,owner_process_id,dispatch_id,"
                    "generation,state,reason_code,enqueued_at,queue_deadline,slot_held,business_route,"
                    "handed_off_at,notification_eligible) "
                    "VALUES ($1,'onebot_v11','synthetic-account','synthetic-group',$2,$3,$4,"
                    "0,$5,'fixture',clock_timestamp(),clock_timestamp()+interval '3 seconds',$6,$7,"
                    "CASE WHEN $8 THEN clock_timestamp() ELSE NULL END,$9)",
                    uuid4(),
                    str(index),
                    owner,
                    uuid4(),
                    state,
                    state == "running",
                    "chat" if state in {"success", "bypass"} else "none",
                    index == 5,
                    state == "failed",
                )
            await connection.execute(
                "UPDATE komari_decision_config SET plugin_enable=false,revision=revision+1 WHERE id=1"
            )
            await connection.execute(
                "UPDATE komari_decision_config SET plugin_enable=true,revision=revision+1 WHERE id=1"
            )
            rows = await connection.fetch(
                "SELECT * FROM komari_decision_dispatches ORDER BY message_id"
            )
            for row in rows[:5]:
                assert row["state"] == "cancelled"
                assert row["reason_code"] == "disabled"
                assert row["business_route"] == "none"
                assert row["notification_eligible"] is False
                assert row["finished_at"] is not None
            assert rows[1]["slot_held"] is True
            assert rows[5]["state"] == "success"
            assert rows[5]["business_route"] == "chat"
            assert rows[5]["handed_off_at"] is not None
            assert (
                await connection.fetchval(
                    "SELECT generation FROM komari_decision_scheduler WHERE id=1"
                )
                == 1
            )
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
