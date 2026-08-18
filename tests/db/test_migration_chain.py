"""Alembic 版本链一致性守卫（无需真实数据库）。

本测试只解析 migrations/ 版本目录，校验版本链结构完整；真实的
``upgrade head`` 与 autogenerate 空 diff 校验由 CI 迁移链卫士工作流
（``.github/workflows/migration-check.yml``）连接临时 PostgreSQL 执行。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
EXPECTED_BASELINE_TABLES = {
    "komari_agent_run_log_index",
    "komari_announcement_dispatches",
    "komari_character_bindings",
    "komari_chat_reply_commit_outbox",
    "komari_custom_proposals",
    "komari_decision_scenes",
    "komari_help",
    "komari_help_scan_leases",
    "komari_knowledge",
    "komari_memory_conversation_embeddings",
    "komari_memory_conversations",
    "komari_memory_interaction_embeddings",
    "komari_memory_interaction_history",
    "komari_memory_jobs",
    "komari_memory_scene_item",
    "komari_memory_scene_runtime",
    "komari_memory_scene_set",
    "komari_memory_user_profile",
    "komari_plugin_configs",
    "komari_prompt_configs",
    "komari_search_index_versions",
    "komari_user_ban_cache_state",
    "komari_user_ban_notification_outbox",
    "komari_user_bans",
    "user_favorability",
    "user_favorability_adjustment_ledger",
}


def _load_script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("version_path_separator", "os")
    return ScriptDirectory.from_config(config)


def test_migration_environment_scaffold_present() -> None:
    """迁移环境脚手架与官方 generic 模板保持一致。"""
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert (MIGRATIONS_DIR / "script.py.mako").is_file()
    assert (MIGRATIONS_DIR / "versions").is_dir()


def test_migration_chain_is_consistent() -> None:
    """版本链无重复 revision，且至多一个分支头（单主干）。"""
    script = _load_script_directory()

    revisions = list(script.walk_revisions())
    assert len(revisions) == len({rev.revision for rev in revisions})

    heads = script.get_heads()
    assert len(heads) == len(set(heads))
    assert len(heads) <= 1


def test_baseline_revision_covers_current_schema() -> None:
    """首个基线 revision（无 down_revision）覆盖既有 PostgreSQL 表。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    baselines = [rev for rev in revisions if rev.down_revision is None]
    assert len(baselines) == 1
    baseline_sql = Path(baselines[0].path).read_text(encoding="utf-8")
    missing_tables = {
        table
        for table in EXPECTED_BASELINE_TABLES
        if f"CREATE TABLE {table}" not in baseline_sql
        and f"CREATE UNLOGGED TABLE {table}" not in baseline_sql
    }

    assert missing_tables == set()


def test_typed_config_tables_revision_exists() -> None:
    """强类型配置表由独立 revision 引入，且挂在基线之后。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    typed_revision = next(
        (
            rev
            for rev in revisions
            if "typed_plugin_config_tables" in Path(rev.path).name
        ),
        None,
    )
    assert typed_revision is not None
    assert typed_revision.down_revision is not None
    revision_sql = Path(typed_revision.path).read_text(encoding="utf-8")
    missing_tables = {
        table
        for table in (
            "komari_agent_run_logger_config",
            "komari_custom_config",
            "komari_decision_config",
            "komari_embedding_provider_config",
            "komari_group_history_summary_config",
            "komari_help_config",
            "komari_knowledge_config",
            "komari_llm_provider_config",
            "komari_management_config",
            "komari_memory_config",
            "komari_search_config",
            "komari_sentry_config",
            "komari_sr_config",
            "komari_user_data_config",
        )
        if f"CREATE TABLE {table}" not in revision_sql
    }
    assert missing_tables == set()


def test_typed_prompt_tables_revision_exists() -> None:
    """强类型 Prompt 表由独立 revision 引入，且挂在配置表 revision 之后。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    typed_revision = next(
        (rev for rev in revisions if "typed_prompt_tables" in Path(rev.path).name),
        None,
    )
    assert typed_revision is not None
    assert typed_revision.down_revision == "0002"
    revision_sql = Path(typed_revision.path).read_text(encoding="utf-8")
    missing_tables = {
        table
        for table in (
            "komari_prompt_komari_chat",
            "komari_prompt_memory_summary",
            "komari_prompt_group_history_summary",
        )
        if f"CREATE TABLE {table}" not in revision_sql
    }
    assert missing_tables == set()
    # 旧版 JSONB KV 表保留给离线迁移脚本（ticket 05），本 revision 不得删表
    assert "DROP TABLE IF EXISTS komari_prompt_configs" not in revision_sql
    assert "DROP TABLE komari_prompt_configs" not in revision_sql


def test_komari_chat_config_revision_exists() -> None:
    """komari_chat 配置表由独立 revision 引入（KOMARIBOT-7）。

    同一 revision 内完成：建 komari_chat_config → 从 komari_memory_config
    单行搬运活字段 → DROP 旧表 11 列（10 活字段 + 死字段
    proactive_score_threshold）；komari_memory_config 表本身保留。
    """
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    chat_revision = next(
        (rev for rev in revisions if "komari_chat_config" in Path(rev.path).name),
        None,
    )
    assert chat_revision is not None
    assert chat_revision.down_revision == "0003"
    revision_sql = Path(chat_revision.path).read_text(encoding="utf-8")

    assert "CREATE TABLE komari_chat_config" in revision_sql
    # 数据搬运在迁移内完成，不留运行时搬运逻辑
    assert "INSERT INTO komari_chat_config" in revision_sql
    assert "FROM komari_memory_config" in revision_sql

    dropped_columns = (
        "proactive_enabled",
        "proactive_score_threshold",
        "proactive_cooldown",
        "proactive_max_per_hour",
        "proactive_reservation_ttl_seconds",
        "reply_commit_worker_interval_seconds",
        "reply_commit_batch_size",
        "reply_commit_lease_seconds",
        "reply_commit_max_attempts",
        "reply_commit_retry_base_seconds",
        "reply_commit_tombstone_retention_days",
    )
    for column in dropped_columns:
        assert re.search(rf"DROP COLUMN (?:IF EXISTS )?{column}\b", revision_sql), (
            column
        )

    # 旧表保留（其余字段仍归 komari_memory 所有），只删列不删表
    assert "DROP TABLE komari_memory_config" not in revision_sql


def test_komari_decision_summary_config_revision_exists() -> None:
    """群总结归类配置列由 0005 revision 加入既有判定配置表。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    summary_revision = next(
        (
            rev
            for rev in revisions
            if "komari_decision_summary_config" in Path(rev.path).name
        ),
        None,
    )
    assert summary_revision is not None
    assert summary_revision.down_revision == "0004"

    revision_sql = Path(summary_revision.path).read_text(encoding="utf-8")
    columns = {
        "summary_embedding_instruction_query",
        "summary_rerank_instruction",
        "summary_scene_top_k",
        "summary_rerank_enabled",
        "summary_rerank_threshold",
        "summary_similarity_threshold",
        "summary_rerank_fallback_enabled",
        "summary_rerank_failure_threshold",
        "summary_rerank_failure_window_seconds",
    }
    for column in columns:
        assert column in revision_sql, column
        assert re.search(rf"DROP COLUMN (?:IF EXISTS )?{column}\b", revision_sql), (
            column
        )

    assert "DROP TABLE komari_decision_config" not in revision_sql


def test_reply_fulfillment_parent_child_revision_exists() -> None:
    """回复履约父子表由 0006 手写 revision 以正交事实建模。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    fulfillment_revision = next(
        (
            rev
            for rev in revisions
            if "reply_fulfillment_parent_child" in Path(rev.path).name
        ),
        None,
    )
    assert fulfillment_revision is not None
    assert fulfillment_revision.revision == "0006"
    assert fulfillment_revision.down_revision == "0005"

    revision_sql = Path(fulfillment_revision.path).read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", revision_sql).upper()
    parent_table = "KOMARI_CHAT_REPLY_FULFILLMENTS"
    child_table = "KOMARI_CHAT_REPLY_FULFILLMENT_COMMITMENTS"

    assert f"CREATE TABLE {parent_table}" in normalized
    assert f"CREATE TABLE {child_table}" in normalized
    assert (
        f"FOREIGN KEY (FULFILLMENT_ID) REFERENCES {parent_table}(FULFILLMENT_ID) "
        "ON DELETE CASCADE"
    ) in normalized
    assert "PRIMARY KEY (FULFILLMENT_ID, COMMITMENT_TYPE)" in normalized

    for commitment_type in (
        "PROACTIVE_REPLY_CONFIRMATION",
        "FAVORABILITY_ADJUSTMENT",
        "ASSISTANT_REPLY_HISTORY",
        "INTERACTION_HISTORY",
    ):
        assert f"'{commitment_type}'" in normalized
    for delivery_state in (
        "NOT_STARTED",
        "PENDING_CONFIRMATION",
        "DELIVERED",
        "NOT_DELIVERED",
    ):
        assert f"'{delivery_state}'" in normalized
    for commitment_state in ("PENDING", "RETRY_WAIT", "COMPLETED", "FAILED"):
        assert f"'{commitment_state}'" in normalized

    parent_columns = {
        "payload_hash",
        "request_trace_id",
        "trigger_message_id",
        "trigger_user_id",
        "group_id",
        "bot_self_id",
        "adapter_name",
        "reply_target_message_id",
        "reply_content",
        "delivery_state",
        "platform_message_id",
        "prepared_at",
        "send_started_at",
        "delivered_at",
        "not_delivered_at",
        "lease_owner",
        "lease_expires_at",
        "completed_at",
    }
    child_columns = {
        "commitment_type",
        "state",
        "attempt_count",
        "next_retry_at",
        "last_error_code",
        "payload",
        "completed_at",
    }
    for column in parent_columns | child_columns:
        assert re.search(rf"\b{column.upper()}\b", normalized), column

    assert "PAYLOAD JSONB" in normalized
    assert "DELIVERY_STATE JSONB" not in normalized
    assert "STATE JSONB" not in normalized
    assert "LEASE_OWNER IS NULL" in normalized
    assert "LEASE_EXPIRES_AT IS NULL" in normalized
    assert normalized.count("CREATE INDEX") >= 2

    child_drop = normalized.find(f"DROP TABLE {child_table}")
    parent_drop = normalized.find(f"DROP TABLE {parent_table}")
    assert 0 <= child_drop < parent_drop


def test_reply_fulfillment_revision_is_self_contained() -> None:
    """父子表迁移不得加载应用运行时，也不得删除旧 outbox。"""
    revision_path = (
        MIGRATIONS_DIR / "versions" / "0006_reply_fulfillment_parent_child.py"
    )
    revision_sql = revision_path.read_text(encoding="utf-8")

    assert "from komari_bot" not in revision_sql
    assert "import komari_bot" not in revision_sql
    assert "DROP TABLE komari_chat_reply_commit_outbox" not in revision_sql


def test_reply_delivery_recovery_revision_exists() -> None:
    """0007 为旧运行路径补齐送达事实与回复时效，不提前切换父子表。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    delivery_revision = next(
        (rev for rev in revisions if "reply_delivery_recovery" in Path(rev.path).name),
        None,
    )
    assert delivery_revision is not None
    assert delivery_revision.revision == "0007"
    assert delivery_revision.down_revision == "0006"

    revision_sql = Path(delivery_revision.path).read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", revision_sql).upper()
    old_table = "KOMARI_CHAT_REPLY_COMMIT_OUTBOX"
    parent_table = "KOMARI_CHAT_REPLY_FULFILLMENTS"

    assert f"ALTER TABLE {old_table}" in normalized
    for column in (
        "delivery_state",
        "bot_self_id",
        "adapter_name",
        "reply_target_message_id",
        "prepared_at",
        "send_started_at",
        "not_delivered_at",
    ):
        assert re.search(rf"\b{column.upper()}\b", normalized), column
    for delivery_state in (
        "NOT_STARTED",
        "PENDING_CONFIRMATION",
        "DELIVERED",
        "NOT_DELIVERED",
    ):
        assert f"'{delivery_state}'" in normalized

    assert "REPLY_FULFILLMENT_FRESHNESS_SECONDS" in normalized
    assert "DEFAULT 120" in normalized
    assert ">= 30" in normalized
    assert "<= 300" in normalized
    assert f"ALTER TABLE {parent_table}" in normalized
    assert "CK_REPLY_FULFILLMENT_DELIVERY_TIMESTAMPS" in normalized
    assert "IDX_REPLY_COMMIT_OUTBOX_DELIVERY_FRESHNESS" in normalized
    assert re.search(
        r'op\.execute\(\s*"DROP INDEX IF EXISTS '
        r'idx_reply_commit_outbox_delivery_freshness"',
        revision_sql,
        re.IGNORECASE,
    )
    assert not re.search(
        r'"ALTER TABLE komari_chat_reply_commit_outbox\s*"\s*'
        r'"DROP INDEX',
        revision_sql,
        re.IGNORECASE,
    )

    assert "DROP TABLE KOMARI_CHAT_REPLY_COMMIT_OUTBOX" not in normalized
    assert "DROP TABLE KOMARI_CHAT_REPLY_FULFILLMENTS" not in normalized
    assert "FROM KOMARI_BOT" not in normalized
    assert "IMPORT KOMARI_BOT" not in normalized


def test_reply_fulfillment_lifecycle_revision_exists() -> None:
    """0008 只扩展新父子模型的最小化与两阶段清理事实。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    lifecycle_revision = next(
        (
            rev
            for rev in revisions
            if "reply_fulfillment_lifecycle" in Path(rev.path).name
        ),
        None,
    )
    assert lifecycle_revision is not None
    assert lifecycle_revision.revision == "0008"
    assert lifecycle_revision.down_revision == "0007"

    revision_sql = Path(lifecycle_revision.path).read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", revision_sql).upper()
    parent_table = "KOMARI_CHAT_REPLY_FULFILLMENTS"

    assert f"ALTER TABLE {parent_table}" in normalized
    assert "REPLY_CONTENT DROP NOT NULL" in normalized
    assert "IDEMPOTENCY_EVIDENCE_CLEARED_AT" in normalized
    assert "CREATE INDEX" in normalized
    assert "COMPLETED_AT" in normalized
    assert "NOT_DELIVERED_AT" in normalized

    assert "KOMARI_CHAT_REPLY_COMMIT_OUTBOX" not in normalized
    assert "INSERT INTO KOMARI_CHAT_REPLY_FULFILLMENTS" not in normalized
    assert "DROP TABLE" not in normalized
    assert "FROM KOMARI_BOT" not in normalized
    assert "IMPORT KOMARI_BOT" not in normalized

    assert "REPLY_CONTENT SET NOT NULL" in normalized
    assert re.search(
        rf"UPDATE {parent_table} .*REPLY_CONTENT = ''",
        normalized,
    )
    assert "DROP COLUMN IDEMPOTENCY_EVIDENCE_CLEARED_AT" in normalized


def test_reply_fulfillment_alert_revision_exists() -> None:
    """0009 只持久化两类告警转换的跨进程去重事实。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    alert_revision = next(
        (rev for rev in revisions if "reply_fulfillment_alert" in Path(rev.path).name),
        None,
    )
    assert alert_revision is not None
    assert alert_revision.revision == "0009"
    assert alert_revision.down_revision == "0008"

    revision_sql = Path(alert_revision.path).read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", revision_sql).upper()

    assert "KOMARI_CHAT_REPLY_FULFILLMENTS" in normalized
    assert "PENDING_CONFIRMATION_ALERTED_AT" in normalized
    assert "KOMARI_CHAT_REPLY_FULFILLMENT_COMMITMENTS" in normalized
    assert "DISPOSITION_ALERTED_AT" in normalized
    assert "DROP COLUMN PENDING_CONFIRMATION_ALERTED_AT" in normalized
    assert "DROP COLUMN DISPOSITION_ALERTED_AT" in normalized

    assert "KOMARI_CHAT_REPLY_COMMIT_OUTBOX" not in normalized
    assert "DROP TABLE" not in normalized
    assert "FROM KOMARI_BOT" not in normalized
    assert "IMPORT KOMARI_BOT" not in normalized


def test_reply_fulfillment_backfill_revision_exists() -> None:
    """0010 在停机事务内预检并回填旧宽 outbox，仍保留旧表。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    backfill_revision = next(
        (
            rev
            for rev in revisions
            if "reply_fulfillment_backfill" in Path(rev.path).name
        ),
        None,
    )
    assert backfill_revision is not None
    assert backfill_revision.revision == "0010"
    assert backfill_revision.down_revision == "0009"

    revision_sql = Path(backfill_revision.path).read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", revision_sql).upper()
    old_table = "KOMARI_CHAT_REPLY_COMMIT_OUTBOX"
    parent_table = "KOMARI_CHAT_REPLY_FULFILLMENTS"
    child_table = "KOMARI_CHAT_REPLY_FULFILLMENT_COMMITMENTS"

    assert f"FROM {old_table}" in normalized
    assert f"INSERT INTO {parent_table}" in normalized
    assert f"INSERT INTO {child_table}" in normalized
    assert "PENDING_CONFIRMATION" in normalized
    assert "NOT_DELIVERED" in normalized
    assert "RETRY_WAIT" in normalized
    assert "FAILED" in normalized
    assert "FOR UPDATE" in normalized
    assert "REPLY_COMMIT_TOMBSTONE_RETENTION_DAYS" in normalized
    assert "AMBIGUOUS_FAILED_COUNT=" in normalized
    assert "MINIMUM_FULFILLMENT_ID=" in normalized
    assert "COUNT(*)" in normalized
    assert "COUNT(DISTINCT" in normalized
    assert "LEASE_OWNER" in normalized
    assert "LEASE_EXPIRES_AT" in normalized
    assert "PAYLOAD_HASH" in normalized
    assert "DISPOSITION_ALERTED_AT" in normalized
    assert "PENDING_CONFIRMATION_ALERTED_AT" in normalized

    assert f"DROP TABLE {old_table}" not in normalized
    assert "DROP TABLE" not in normalized
    assert "CREATE TABLE" not in normalized
    assert "ALTER TABLE" not in normalized
    assert "FROM KOMARI_BOT" not in normalized
    assert "IMPORT KOMARI_BOT" not in normalized


def test_reply_fulfillment_cutover_revision_exists() -> None:
    """0011 完成停机 contract：门禁完整后改名配置并删除旧宽表。"""
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    cutover_revision = next(
        (
            rev
            for rev in revisions
            if "reply_fulfillment_cutover" in Path(rev.path).name
        ),
        None,
    )
    assert cutover_revision is not None
    assert cutover_revision.revision == "0011"
    assert cutover_revision.down_revision == "0010"

    revision_sql = Path(cutover_revision.path).read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", revision_sql).upper()
    old_table = "KOMARI_CHAT_REPLY_COMMIT_OUTBOX"
    parent_table = "KOMARI_CHAT_REPLY_FULFILLMENTS"
    child_table = "KOMARI_CHAT_REPLY_FULFILLMENT_COMMITMENTS"
    config_table = "KOMARI_CHAT_CONFIG"
    renamed_columns = {
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
    }

    assert "FOR UPDATE" in normalized
    assert f"FROM {old_table}" in normalized
    assert f"FROM {parent_table}" in normalized
    assert "MISSING_BACKFILL_COUNT=" in normalized
    assert "MINIMUM_FULFILLMENT_ID=" in normalized
    assert "COUNT(*)" in normalized
    assert "COUNT(DISTINCT" in normalized
    for old_name, new_name in renamed_columns.items():
        assert re.search(
            rf"ALTER TABLE {config_table} .*RENAME COLUMN "
            rf"{old_name.upper()} TO {new_name.upper()}",
            normalized,
        ), old_name
    assert "REPLY_FULFILLMENT_RETRY_MAX_SECONDS" in normalized
    assert "DEFAULT 3600" in normalized
    assert f"DROP TABLE {old_table}" in normalized
    assert normalized.index("MISSING_BACKFILL_COUNT=") < normalized.index(
        f"DROP TABLE {old_table}"
    )

    assert f"DROP TABLE {parent_table}" not in normalized
    assert f"DROP TABLE {child_table}" not in normalized
    assert f"DROP TABLE {config_table}" not in normalized
    assert "FROM KOMARI_BOT" not in normalized
    assert "IMPORT KOMARI_BOT" not in normalized
    assert "0011_REPLY_FULFILLMENT_CUTOVER_IS_IRREVERSIBLE" in normalized


def test_migration_cli_can_inspect_chain_without_loading_application(
    tmp_path: Path,
) -> None:
    """迁移命令只加载 ORM 基础设施，不依赖业务插件或旧连接池。"""
    env = os.environ.copy()
    env.update(
        {
            "ALEMBIC_SCRIPT_LOCATION": str(MIGRATIONS_DIR),
            "ENVIRONMENT": "orm_migration_test",
            "PYTHONPATH": str(PROJECT_ROOT),
            "SQLALCHEMY_DATABASE_URL": (
                "postgresql+asyncpg://komari_bot:change_me@localhost:5432/komari_bot"
            ),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "komari_bot.db.orm_bootstrap",
            "heads",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = f"{result.stdout}\n{result.stderr}"
    assert "config_manager" not in output
    assert "PostgresPool" not in output


def test_chat_prompt_behavior_columns_revision_exists() -> None:
    """AC3：0011 的直接后继 revision 显式删除 output_instruction 并新增行为列。

    新列集合由强类型 Schema 与 0003 保留列差集推导，不猜测迁移实现；
    迁移必须自包含（不导入运行时），并将删除旧列与新增新列落实在 DDL。
    删除旧列接受项目允许的显式 Alembic 表达（``op.drop_column`` 或自包含
    ``DROP COLUMN`` SQL），不锁定 raw SQL 实现；真实 schema 变更由
    ``test_prompt_seed_bootstrap_integration.py`` 的隔离库迁移用例验证，
    本用例承担无数据库的静态守卫。
    """
    from tests.config.chat_prompt_field_contract import (
        new_chat_prompt_column_names,
    )

    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    children = [rev for rev in revisions if rev.down_revision == "0011"]
    assert len(children) == 1, f"0011 的直接后继 revision 必须唯一: {children}"

    revision_sql = Path(children[0].path).read_text(encoding="utf-8")
    assert _has_explicit_drop_column(revision_sql, "output_instruction"), (
        "迁移必须显式删除 output_instruction 列"
        "（op.drop_column(..., 'output_instruction') 或 "
        "DROP COLUMN [IF EXISTS] output_instruction SQL）"
    )

    for column in sorted(new_chat_prompt_column_names()):
        assert re.search(rf"\b{re.escape(column)}\b", revision_sql), (
            f"迁移必须新增聊天 Prompt 行为列: {column}"
        )

    assert "from komari_bot" not in revision_sql
    assert "import komari_bot" not in revision_sql


def test_agent_budget_config_revision_exists() -> None:
    """TSK-192：0012 的直接后继 revision 新增三项回复 Agent 预算列。

    新列（agent_max_rounds / agent_max_tool_calls_per_round /
    agent_max_total_tool_calls）必须显式声明跨字段 CHECK（AC1/AC2 的
    数据库侧表达），迁移需自包含。本用例是静态守卫：只守新增 revision /
    列名 / 自包含 / 显式约束声明，不锁定 raw SQL 的 ``DEFAULT 10`` /
    ``CHECK(...)`` 文本形态（接受 Alembic op 或 SQLModel 合法表达）；
    真实默认值与约束语义由 ``test_agent_budget_config_migration.py`` 的
    隔离库用例验证。
    """
    script = _load_script_directory()
    revisions = list(script.walk_revisions())
    children = [rev for rev in revisions if rev.down_revision == "0012"]
    assert len(children) == 1, f"0012 的直接后继 revision 必须唯一: {children}"

    revision_sql = Path(children[0].path).read_text(encoding="utf-8")
    for column in (
        "agent_max_rounds",
        "agent_max_tool_calls_per_round",
        "agent_max_total_tool_calls",
    ):
        assert re.search(rf"\b{re.escape(column)}\b", revision_sql), (
            f"迁移必须新增预算列: {column}"
        )
    assert _has_explicit_cross_field_check(revision_sql), (
        "迁移必须显式声明跨字段 CHECK（op.create_check_constraint / "
        "CheckConstraint / 含三列的 CHECK SQL 任一形态）"
    )

    assert "from komari_bot" not in revision_sql
    assert "import komari_bot" not in revision_sql


def _has_explicit_cross_field_check(revision_sql: str) -> bool:
    """识别显式跨字段 CHECK 声明，不锁定 SQL 字符串形态。

    接受 Alembic ``op.create_check_constraint``、SQLModel/SQLAlchemy
    ``CheckConstraint`` 或 raw / ``op.execute`` SQL 中的 ``CHECK (...)``，
    只要同一声明同时引用三项预算列即认可；不校验默认值文本或约束命名。
    真实约束语义由 PostgreSQL 隔离库用例验证。
    """
    budget_columns = (
        "agent_max_tool_calls_per_round",
        "agent_max_total_tool_calls",
        "agent_max_rounds",
    )

    def _mentions_all(text: str) -> bool:
        return all(
            re.search(rf"\b{re.escape(column)}\b", text) for column in budget_columns
        )

    def _paren_body(position: int) -> str:
        """从 ``(`` 之后的 position 扫描平衡括号，返回括号体。"""
        depth = 1
        index = position
        while index < len(revision_sql) and depth:
            if revision_sql[index] == "(":
                depth += 1
            elif revision_sql[index] == ")":
                depth -= 1
            index += 1
        return revision_sql[position : index - 1]

    # raw SQL / op.execute 字符串内的 CHECK (...)
    for match in re.finditer(r"\bCHECK\s*\(", revision_sql, re.IGNORECASE):
        if _mentions_all(_paren_body(match.end())):
            return True

    # Alembic op.create_check_constraint(...) 或 SQLModel CheckConstraint(...)
    for match in re.finditer(
        r"\b(?:op\.create_check_constraint|CheckConstraint)\s*\(",
        revision_sql,
    ):
        if _mentions_all(_paren_body(match.end())):
            return True

    return False


def _has_explicit_drop_column(revision_sql: str, column: str) -> bool:
    """识别项目允许的显式列删除表达：``op.drop_column`` 或自包含 SQL。

    真实删除行为由 PostgreSQL 集成测试（``test_prompt_seed_``
    ``bootstrap_integration.py``）确认；静态守卫只要求显式表达存在，
    不锁死 raw SQL 实现。
    """
    alembic_drop = re.search(
        rf"op\.drop_column\(\s*(['\"])[^'\"]*\1\s*,\s*(['\"])"
        rf"{re.escape(column)}\2",
        revision_sql,
        re.IGNORECASE,
    )
    raw_sql_drop = re.search(
        rf"DROP\s+COLUMN(?:\s+IF\s+EXISTS)?\s+{re.escape(column)}\b",
        revision_sql,
        re.IGNORECASE,
    )
    return bool(alembic_drop or raw_sql_drop)
