"""TSK-207: source census for the retired Python model execution surface.

The census is deliberately static.  Behavioural retirement evidence lives in
the chat/summary/DSH entry-point suites, the Jev seed tests and the isolated
PostgreSQL migration tests; this file only proves that no retired runtime
entry point, prompt resource, dedicated configuration field or obsolete
dependency is still advertised by the shipped tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "komari_bot"
PLUGINS_ROOT = PACKAGE_ROOT / "plugins"
SCRIPT_ROOT = PROJECT_ROOT / "scripts"
MIGRATIONS_ROOT = PROJECT_ROOT / "migrations"

RETIRED_RUNTIME_PATHS = (
    PLUGINS_ROOT / "llm_provider",
    PLUGINS_ROOT / "komari_chat" / "services" / "agent_budget.py",
    PLUGINS_ROOT / "komari_chat" / "prompt_schema.py",
    PLUGINS_ROOT / "group_history_summary" / "prompt_schema.py",
    PLUGINS_ROOT / "komari_chat" / "services" / "llm_service.py",
    PLUGINS_ROOT / "komari_chat" / "services" / "query_rewrite_service.py",
    PLUGINS_ROOT / "komari_chat" / "services" / "prompt_builder.py",
    PLUGINS_ROOT / "komari_chat" / "services" / "prompt_template.py",
    PLUGINS_ROOT / "komari_chat" / "services" / "vision_service.py",
    PLUGINS_ROOT / "komari_chat" / "services" / "image_reading_session.py",
    PLUGINS_ROOT / "group_history_summary" / "prompt_template.py",
    PACKAGE_ROOT / "llm" / "dsv4_instruct.py",
    PACKAGE_ROOT / "llm" / "llm_protocol.py",
    PACKAGE_ROOT / "db" / "initial_data" / "scenes.yaml",
    SCRIPT_ROOT / "migrate_prompt_config_to_pg.py",
)

RETIRED_RUNTIME_SYMBOLS = (
    "llm_provider",
    "AgentExecutionBudget",
    "AgentBudgetLedger",
    "prompt_guided",
    "komari_llm_provider_config",
    "komari_prompt_komari_chat",
    "komari_prompt_group_history_summary",
    "query_rewrite_service",
    "prompt_builder",
    "prompt_template",
    "vision_service",
    "image_reading_session",
    "dsv4_instruct",
    "llm_protocol",
    "apply_llm_security_boundary",
    "LLM_SECURITY_SYSTEM_INSTRUCTION",
    "format_message_for_prompt",
    "READ_IMAGE_TOOL",
    "read_images",
    "ImageReadingSession",
)

CHAT_RETIRED_COLUMNS = frozenset(
    {
        "llm_model_chat",
        "llm_temperature_chat",
        "llm_max_tokens_chat",
        "llm_request_api_chat",
        "llm_stream_enabled_chat",
        "llm_thinking_mode_chat",
        "llm_reasoning_effort_chat",
        "assistant_prefill_enabled",
        "dsv4_roleplay_instruct_mode",
        "query_rewrite_model",
        "query_rewrite_request_api",
        "query_rewrite_stream_enabled",
        "query_rewrite_thinking_mode",
        "query_rewrite_reasoning_effort",
        "agent_tool_call_mode",
        "image_understanding_mode",
    }
)

SUMMARY_RETIRED_COLUMNS = frozenset(
    {
        "summary_planning_model",
        "summary_planning_max_tokens",
        "summary_planning_request_api",
        "summary_planning_stream_enabled",
        "summary_planning_thinking_mode",
        "summary_planning_reasoning_effort",
        "summary_model",
        "summary_temperature",
        "summary_max_tokens",
        "summary_request_api",
        "summary_stream_enabled",
        "summary_thinking_mode",
        "summary_reasoning_effort",
        "assistant_prefill_enabled",
        "dsv4_roleplay_instruct_mode",
    }
)

RETIRED_TABLES = frozenset(
    {
        "komari_llm_provider_config",
        "komari_prompt_komari_chat",
        "komari_prompt_group_history_summary",
    }
)


def _production_python_files() -> list[Path]:
    return sorted(
        path
        for root in (PACKAGE_ROOT, SCRIPT_ROOT)
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_retired_runtime_paths_are_deleted() -> None:
    present = [
        str(path.relative_to(PROJECT_ROOT))
        for path in RETIRED_RUNTIME_PATHS
        if path.exists()
    ]
    assert present == [], f"retired runtime paths still exist: {present}"


def test_no_production_runtime_reference_to_retired_entry_points() -> None:
    offenders: list[str] = []
    for path in _production_python_files():
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(PROJECT_ROOT)}: {symbol}"
            for symbol in RETIRED_RUNTIME_SYMBOLS
            if symbol in text
        )
    assert offenders == [], (
        "retired model execution surface is still referenced:\n" + "\n".join(offenders)
    )


def test_config_schemas_no_longer_advertise_retired_fields() -> None:
    from komari_bot.plugins.group_history_summary.config_schema import (
        DynamicConfigSchema as SummaryConfig,
    )
    from komari_bot.plugins.komari_chat.config_schema import KomariChatConfigSchema

    chat_fields = set(KomariChatConfigSchema.model_fields)
    summary_fields = set(SummaryConfig.model_fields)

    assert CHAT_RETIRED_COLUMNS.isdisjoint(chat_fields)
    assert SUMMARY_RETIRED_COLUMNS.isdisjoint(summary_fields)

    # Surviving budgets are the accepted task/input policy and must remain.
    assert {
        "agent_max_rounds",
        "agent_max_tool_calls_per_round",
        "agent_max_total_tool_calls",
    } <= chat_fields
    assert {
        "summary_planning_round_limit",
        "vision_image_download_max_count",
        "vision_image_download_max_bytes",
        "vision_image_download_total_max_bytes",
        "vision_image_download_max_pixels",
        "vision_image_download_concurrency",
        "vision_image_download_connect_timeout_seconds",
        "vision_image_download_read_timeout_seconds",
        "vision_image_download_total_timeout_seconds",
    } <= chat_fields | summary_fields


def test_seed_cli_only_exposes_optional_decision_asset() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "komari_bot.db.seed_bootstrap", "--help"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    assert "--decision-seed-file" in output
    assert "--seed-file" not in output


def test_management_prompt_registration_only_names_decision_prompt() -> None:
    source = (PLUGINS_ROOT / "komari_management" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "komari_chat.prompt_schema" not in source
    assert "group_history_summary.prompt_schema" not in source
    prompt_block = source.split("prompt_resources=(", 1)[1]
    assert 'resource_id="komari_decision"' in prompt_block
    assert 'resource_id="komari_chat"' not in prompt_block
    assert 'resource_id="group_history_summary"' not in prompt_block


def test_pyproject_and_lockfile_do_not_carry_direct_openai_dependency() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    dependencies = pyproject["project"]["dependencies"]
    assert not any(dependency.split()[0] == "openai" for dependency in dependencies)
    # SDK transitives remain owned by Poetry; removing the direct dependency
    # must not force removal of a still-required transitive package.


def test_migration_0033_declares_exact_retirement_allowlist() -> None:
    migration = MIGRATIONS_ROOT / "versions" / "0033_dsh_execution_retirement.py"
    assert migration.is_file(), "0033 retirement migration is missing"
    source = migration.read_text(encoding="utf-8")
    assert 'revision: str = "0033"' in source
    assert 'down_revision: str | Sequence[str] | None = "0032"' in source
    assert "0033_TSK207_IS_IRREVERSIBLE" in source
    for table in RETIRED_TABLES:
        assert table in source
    for column in CHAT_RETIRED_COLUMNS | SUMMARY_RETIRED_COLUMNS:
        assert column in source


def test_historical_migrations_are_not_rewritten() -> None:
    """The census only retires the current runtime, never the frozen chain."""
    historical = [
        path
        for path in sorted((MIGRATIONS_ROOT / "versions").glob("*.py"))
        if path.name < "0033_"
    ]
    assert historical, "historical migration chain must remain present"
    assert any(
        "komari_prompt_komari_chat" in path.read_text(encoding="utf-8")
        for path in historical
    )
    assert any(
        "komari_llm_provider_config" in path.read_text(encoding="utf-8")
        for path in historical
    )


def test_retired_model_fields_have_no_runtime_reader() -> None:
    offenders = [
        f"{path.relative_to(PROJECT_ROOT)}: {field}"
        for path in _production_python_files()
        for field in CHAT_RETIRED_COLUMNS | SUMMARY_RETIRED_COLUMNS
        if field in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
