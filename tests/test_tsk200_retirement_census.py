"""TSK-200: Agent Run logger retirement census.

Static census over the shipped sources: the retired logger must have no
runtime reference, no plugin directory, no log router, no collector factory,
no trace recorder and no per-plugin container log mounts.  This complements
the behavioural entry-point tests (debug safe results, management route/config
removal, migration 0032); it never replaces them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "komari_bot"
SCRIPT_ROOT = PROJECT_ROOT / "scripts"

RETIRED_RUNTIME_SYMBOLS = (
    "agent_run_logger",
    "AgentRunCollector",
    "LLMDiagnosticCollector",
    "create_collector",
    "finalize_collector",
    "record_completion_call",
    "record_failed_call",
    "ToolExecutionTrace",
    "LLMCallTrace",
    "agent_run_logs:",
)

#: Historical migration sources intentionally keep their frozen text.
FROZEN_PATHS = (
    PROJECT_ROOT / "migrations",
    PROJECT_ROOT / "tests",
)


def _production_python_files() -> list[Path]:
    return sorted(
        path
        for root in (PACKAGE_ROOT, SCRIPT_ROOT)
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def test_agent_run_logger_plugin_package_is_deleted() -> None:
    assert not (PACKAGE_ROOT / "plugins" / "agent_run_logger").exists()
    assert not list((PACKAGE_ROOT / "plugins").glob("agent_run_logger*"))


def test_no_production_runtime_reference_to_retired_agent_run_logger() -> None:
    offenders: list[str] = []
    for path in _production_python_files():
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(PROJECT_ROOT)}: {symbol}"
            for symbol in RETIRED_RUNTIME_SYMBOLS
            if symbol in text
        )
    assert offenders == [], "退役的 Agent Run 采集面仍有运行时引用:\n" + "\n".join(
        offenders
    )


def test_frozen_migration_and_test_sources_are_not_scanned() -> None:
    """Guard the census boundary: only shipped runtime sources are scanned."""
    frozen = [path for path in FROZEN_PATHS if path.exists()]
    assert frozen, "冻结历史断言目录必须存在"


def test_no_retired_per_plugin_log_mounts_in_docker_compose() -> None:
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "agent_run_logger" not in compose
    assert "logs/llm_provider" not in compose
    # Ordinary volumes (config/data/cache) are untouched.
    assert "./app/config:/app/config" in compose
    assert "./app/data:/app/data" in compose


def test_legacy_migration_script_no_longer_targets_retired_table() -> None:
    script = (SCRIPT_ROOT / "migrate_legacy_config_to_typed_tables.py").read_text(
        encoding="utf-8"
    )
    assert "komari_agent_run_logger_config" not in script
    assert 'key_value="agent_run_logger"' not in script
    # The rest of the legacy aid stays intact.
    assert "komari_embedding_provider_config" in script


def test_dockerfile_and_pyproject_have_no_retired_plugin_reference() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "agent_run_logger" not in dockerfile
    assert "agent_run_logger" not in pyproject


@pytest.mark.parametrize(
    "retired_test_path",
    [
        "tests/agent_run_logger",
        "tests/llm_provider/test_diagnostic.py",
    ],
)
def test_retired_logger_only_test_surfaces_are_deleted(
    retired_test_path: str,
) -> None:
    assert not (PROJECT_ROOT / retired_test_path).exists()
