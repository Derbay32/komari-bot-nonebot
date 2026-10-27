"""Retire exactly the management configuration table, preserving business data."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest

from tests.db.test_migration_0033_dsh_execution_retirement import (
    _populate_singleton,
    _rows,
    _schema,
)
from tests.db.tsk197_gate_support import (
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

TABLE = "komari_management_config"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations/versions/0034_management_dotenv_config.py"
)
CODE = "0034_MANAGEMENT_CONFIG_IS_IRREVERSIBLE"


def test_0034_only_drops_management_config_and_refuses_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("migration_0034", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    monkeypatch.setattr(module, "op", SimpleNamespace(execute=statements.append))
    assert module.revision == "0034"
    assert module.down_revision == "0033"
    module.upgrade("other")
    module.downgrade("other")
    assert statements == []
    module.upgrade()
    assert statements == ['DROP TABLE "komari_management_config"']
    with pytest.raises(RuntimeError, match=CODE):
        module.downgrade()


@SKIP_NO_POSTGRES
@pytest.mark.parametrize("populated", [False, True])
async def test_0034_upgrade_preserves_other_tables_and_has_zero_drift(
    *, populated: bool
) -> None:
    scratch = await recreate_scratch_database(f"_management_0034_{int(populated)}")
    url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(url, "upgrade", "0033")
        assert result.returncode == 0, "historical upgrade failed"
        connection = await asyncpg.connect(**scratch)
        try:
            if populated:
                await _populate_singleton(connection, TABLE)
                await _populate_singleton(connection, "komari_context_config")
            before_schema = await _schema(connection)
            before_rows = {
                table: await _rows(connection, table)
                for table in before_schema
                if table not in {TABLE, "alembic_version"}
            }
        finally:
            await connection.close()

        result = run_bootstrap(url, "upgrade", "head")
        assert result.returncode == 0, "management configuration retirement failed"
        check = run_bootstrap(url, "check")
        assert check.returncode == 0, (
            "ORM drift after management configuration retirement"
        )
        connection = await asyncpg.connect(**scratch)
        try:
            expected = {
                table: columns
                for table, columns in before_schema.items()
                if table != TABLE
            }
            assert await _schema(connection) == expected
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0034"
            )
            for table, rows in before_rows.items():
                assert await _rows(connection, table) == rows, table
        finally:
            await connection.close()

        refusal = run_bootstrap(url, "downgrade", "0033")
        assert refusal.returncode != 0
        assert CODE in refusal.stdout + refusal.stderr
        connection = await asyncpg.connect(**scratch)
        try:
            assert await _schema(connection) == expected
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0034"
            )
            for table, rows in before_rows.items():
                assert await _rows(connection, table) == rows, table
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
