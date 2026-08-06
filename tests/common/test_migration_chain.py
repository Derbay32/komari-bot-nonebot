"""Alembic 版本链一致性守卫（无需真实数据库）。

本测试只解析 migrations/ 版本目录，校验版本链结构完整；真实的
``upgrade head`` 与 autogenerate 空 diff 校验由 CI 迁移链卫士工作流
（``.github/workflows/migration-check.yml``）连接临时 PostgreSQL 执行。
"""

from __future__ import annotations

import os
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
        (rev for rev in revisions if "typed_plugin_config_tables" in Path(rev.path).name),
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
            "komari_bot.common.orm_bootstrap",
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
