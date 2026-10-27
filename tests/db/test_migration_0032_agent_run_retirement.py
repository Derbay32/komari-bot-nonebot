"""TSK-200: revision 0032 retires the Agent Run logger storage.

The Agent Run logger plugin (JSONL storage, PostgreSQL index, query API and
managed config resource) is retired completely.  Revision 0032 is the only
schema/state step left:

- drop exactly ``komari_agent_run_logger_config`` and
  ``komari_agent_run_log_index`` by handwritten DDL (no runtime DDL);
- filter the retired ``agent_run_logs:*`` permission strings from persisted
  management credentials, preserving every other permission and credential
  value, and removing credentials that would be left without any permission;
- advance the management config revision/timestamp only when filtering
  actually changed the persisted value;
- refuse a downgrade with a stable forward-repair code.
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from tests.db.tsk197_gate_support import (
    HEAD_REVISION,
    SKIP_NO_POSTGRES,
    drop_scratch_database,
    recreate_scratch_database,
    run_bootstrap,
    scratch_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT / "migrations" / "versions" / "0032_agent_run_retirement.py"
)
CONFIG_TABLE = "komari_agent_run_logger_config"
INDEX_TABLE = "komari_agent_run_log_index"
MANAGEMENT_TABLE = "komari_management_config"
CHAT_TABLE = "komari_chat_config"
DOWNGRADE_CODE = "0032_TSK200_IS_IRREVERSIBLE"

#: Past timestamp proving the migration advances ``updated_at`` on change.
OLD_UPDATED_AT = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
MANAGEMENT_REVISION = 7
CHAT_SENTINEL_MODEL = "tsk-0032-chat-sentinel"
CHAT_REVISION = 4242

RETIRED_PERMISSIONS = ("agent_run_logs:read", "agent_run_logs:*")


def _load_migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("mig_0032_guard", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── source shape (no services required) ─────────────────────────────────


def test_0032_source_only_retires_agent_run_storage_and_permissions() -> None:
    """0032 is the two-table drop plus permission filtering and refusal guard."""
    assert MIGRATION_PATH.name == "0032_agent_run_retirement.py"
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    module = _load_migration_module()

    assert module.revision == "0032"
    assert module.down_revision == "0031"
    assert DOWNGRADE_CODE in source
    assert CONFIG_TABLE in source
    assert INDEX_TABLE in source
    assert "agent_run_logs:" in source
    assert MANAGEMENT_TABLE in source

    dropped = set(re.findall(r"DROP TABLE (?:IF EXISTS )?\"?([a-z_]+)", source))
    assert dropped == {CONFIG_TABLE, INDEX_TABLE}

    # Only the retirement DDL/DML: no new table/column/index and no runtime
    # module import (migrations never import production packages).
    for forbidden in (
        "create_table",
        "add_column",
        "alter_column",
        "create_index",
        "drop_index",
        "bulk_insert",
        "drop_column",
        "from komari_bot",
        "import komari_bot",
    ):
        assert forbidden not in source, forbidden

    # Named non-default migration binds remain a no-op, exactly like 0031.
    module.upgrade("test-named-bind")
    module.downgrade("test-named-bind")
    with pytest.raises(RuntimeError, match=DOWNGRADE_CODE):
        module.downgrade()


# ── real isolated database cases ────────────────────────────────────────


async def _table_exists(connection: asyncpg.Connection, table: str) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT to_regclass($1) IS NOT NULL",
            f"public.{table}",
        )
    )


async def _management_row(connection: asyncpg.Connection) -> dict[str, Any]:
    row = await connection.fetchrow(f"SELECT * FROM {MANAGEMENT_TABLE} WHERE id = 1")
    assert row is not None
    return dict(row)


async def _chat_row(connection: asyncpg.Connection) -> dict[str, Any]:
    row = await connection.fetchrow(f"SELECT * FROM {CHAT_TABLE} WHERE id = 1")
    assert row is not None
    return dict(row)


def _decode_credentials(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw = row["api_credentials"]
    decoded = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(decoded, list)
    return decoded


async def _seed_management_credentials(
    connection: asyncpg.Connection,
    credentials: list[dict[str, Any]],
) -> None:
    """Create the lazily-initialized management singleton with fixed values."""
    await connection.execute(
        f"INSERT INTO {MANAGEMENT_TABLE} ("
        "id, revision, updated_at, plugin_enable, api_credentials, "
        "api_allowed_origins, announce_status_page_url, "
        "announce_max_group_count, announce_send_interval_seconds, "
        "announce_request_cooldown_seconds"
        ") VALUES (1, $1, $2, TRUE, $3, '[]'::jsonb, "
        "'https://status.example/x', 37, 1.0, 30.0) "
        "ON CONFLICT (id) DO UPDATE SET "
        "api_credentials = EXCLUDED.api_credentials, "
        "revision = EXCLUDED.revision, "
        "updated_at = EXCLUDED.updated_at, "
        "plugin_enable = EXCLUDED.plugin_enable, "
        "announce_max_group_count = EXCLUDED.announce_max_group_count",
        MANAGEMENT_REVISION,
        OLD_UPDATED_AT,
        json.dumps(credentials),
    )


async def _seed_unrelated_chat_row(connection: asyncpg.Connection) -> None:
    await connection.execute(
        f"UPDATE {CHAT_TABLE} SET llm_model_chat = $1, revision = $2 WHERE id = 1",
        CHAT_SENTINEL_MODEL,
        CHAT_REVISION,
    )


@SKIP_NO_POSTGRES
async def test_0032_drops_retired_log_storage_and_filters_permissions() -> None:
    """Mixed credentials: retired scopes filtered, others and unrelated data kept."""
    scratch = await recreate_scratch_database("_tsk200_0032_mixed")
    db_url = scratch_url(str(scratch["database"]))
    legacy_only = {
        "credential_id": "legacy-only",
        "token": "legacy-only-token-000000",
        "permissions": ["agent_run_logs:read", "agent_run_logs:*"],
        "revoked_at": None,
    }
    mixed = {
        "credential_id": "mixed",
        "token": "mixed-token-0000000000",
        "permissions": [
            "config:read",
            "agent_run_logs:read",
            "announce:send",
            "agent_run_logs:*",
            "config:read",
        ],
        "revoked_at": "2027-01-01T00:00:00+00:00",
    }
    unrelated = {
        "credential_id": "unrelated",
        "token": "unrelated-token-000000",
        "permissions": ["search:read", "knowledge:read"],
        "revoked_at": None,
    }
    seeded = [legacy_only, mixed, unrelated]
    try:
        result = run_bootstrap(db_url, "upgrade", "0031")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            assert await _table_exists(connection, CONFIG_TABLE)
            assert await _table_exists(connection, INDEX_TABLE)
            await _seed_management_credentials(connection, seeded)
            await _seed_unrelated_chat_row(connection)
            before_chat = await _chat_row(connection)
            before_management = await _management_row(connection)
            assert _decode_credentials(before_management) == seeded
        finally:
            await connection.close()

        result = run_bootstrap(db_url, "upgrade", "0032")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0032"
            )
            assert not await _table_exists(connection, CONFIG_TABLE)
            assert not await _table_exists(connection, INDEX_TABLE)

            after_management = await _management_row(connection)
            credentials = _decode_credentials(after_management)
            # legacy-only credential removed; mixed credential filtered while
            # preserving order, duplicates and every other value; unrelated
            # credential untouched.
            assert [item["credential_id"] for item in credentials] == [
                "mixed",
                "unrelated",
            ]
            assert credentials[0] == {
                "credential_id": "mixed",
                "token": "mixed-token-0000000000",
                "permissions": ["config:read", "announce:send", "config:read"],
                "revoked_at": "2027-01-01T00:00:00+00:00",
            }
            assert credentials[1] == unrelated
            # Every credential keeps its exact secret value and revoke time.
            assert credentials[1]["token"] == unrelated["token"]

            # Persisted config changed → revision/timestamp advance.
            assert after_management["revision"] == MANAGEMENT_REVISION + 1
            assert after_management["updated_at"] > OLD_UPDATED_AT
            # Unrelated management columns survive.
            assert after_management["plugin_enable"] is True
            assert after_management["announce_max_group_count"] == 37
            # Unrelated table/data survive untouched.
            assert await _chat_row(connection) == before_chat
            assert before_chat["llm_model_chat"] == CHAT_SENTINEL_MODEL
        finally:
            await connection.close()

        downgrade = run_bootstrap(db_url, "downgrade", "0031")
        assert downgrade.returncode != 0
        assert DOWNGRADE_CODE in f"{downgrade.stdout}\n{downgrade.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == "0032"
            )
            assert not await _table_exists(connection, CONFIG_TABLE)
            assert not await _table_exists(connection, INDEX_TABLE)
            assert await _management_row(connection) == after_management
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


@SKIP_NO_POSTGRES
async def test_0032_leaves_config_untouched_without_retired_permissions() -> None:
    """No retired permission → credentials, revision and timestamp unchanged."""
    scratch = await recreate_scratch_database("_tsk200_0032_clean")
    db_url = scratch_url(str(scratch["database"]))
    kept = [
        {
            "credential_id": "operator",
            "token": "operator-token-000000",
            "permissions": ["config:read", "config:write", "*"],
            "revoked_at": None,
        }
    ]
    try:
        result = run_bootstrap(db_url, "upgrade", "0031")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            await _seed_management_credentials(connection, kept)
            before = await _management_row(connection)
        finally:
            await connection.close()

        result = run_bootstrap(db_url, "upgrade", "0032")
        assert result.returncode == 0, result.stderr

        connection = await asyncpg.connect(**scratch)
        try:
            after = await _management_row(connection)
            assert after == before
            assert _decode_credentials(after) == kept
            assert after["revision"] == MANAGEMENT_REVISION
            assert after["updated_at"] == OLD_UPDATED_AT
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))


@SKIP_NO_POSTGRES
async def test_0032_fresh_full_chain_reaches_head_without_log_storage() -> None:
    """A fresh database converges to head without the retired tables."""
    scratch = await recreate_scratch_database("_tsk200_0032_fresh")
    db_url = scratch_url(str(scratch["database"]))
    try:
        result = run_bootstrap(db_url, "upgrade", "head")
        assert result.returncode == 0, result.stderr

        check = run_bootstrap(db_url, "check")
        assert check.returncode == 0, f"{check.stdout}\n{check.stderr}"

        connection = await asyncpg.connect(**scratch)
        try:
            assert (
                await connection.fetchval("SELECT version_num FROM alembic_version")
                == HEAD_REVISION
            )
            assert not await _table_exists(connection, CONFIG_TABLE)
            assert not await _table_exists(connection, INDEX_TABLE)
            # The management singleton has no seeded row on a fresh database;
            # filtering must not fabricate one.
            assert (
                await connection.fetchval(
                    f"SELECT EXISTS (SELECT 1 FROM {MANAGEMENT_TABLE} WHERE id = 1)"
                )
            ) is False
        finally:
            await connection.close()
    finally:
        await drop_scratch_database(str(scratch["database"]))
