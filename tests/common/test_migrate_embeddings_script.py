"""Migration script orchestration tests."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from komari_bot.common.database_config import DatabaseConfigSchema
from komari_bot.common.embedding_migration import TableMigrationResult

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/migrate_embeddings.py"


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "tests.scripts.migrate_embeddings_script",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakePool:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeEmbeddingService:
    instances: ClassVar[list["_FakeEmbeddingService"]] = []

    def __init__(self, config: object) -> None:
        self.config = config
        self.cleaned = False
        self.__class__.instances.append(self)

    async def cleanup(self) -> None:
        self.cleaned = True


def _make_db_config() -> DatabaseConfigSchema:
    return DatabaseConfigSchema(
        pg_host="localhost",
        pg_port=5432,
        pg_database="komari_bot",
        pg_user="user",
        pg_password="pass",
    )


def test_main_async_apply_reuses_pool_and_cleans_up_resources(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    _FakeEmbeddingService.instances.clear()
    db_config = _make_db_config()
    created_pools: list[_FakePool] = []
    migrate_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        module,
        "load_embedding_config",
        lambda _path: types.SimpleNamespace(
            embedding_model="test-model",
            embedding_dimension=1536,
        ),
    )
    monkeypatch.setattr(
        module, "resolve_knowledge_database_config", lambda **_kwargs: db_config
    )
    monkeypatch.setattr(
        module, "resolve_memory_database_config", lambda **_kwargs: db_config
    )

    async def _fake_create_pool(
        config: DatabaseConfigSchema, *, command_timeout: int
    ) -> _FakePool:
        assert command_timeout == 60
        pool = _FakePool(config.pg_database)
        created_pools.append(pool)
        return pool

    async def _fake_migrate(
        pool: _FakePool,
        *,
        spec: Any,
        target_dimension: int,
        dry_run: bool,
        embedding_service: object | None,
    ) -> TableMigrationResult:
        assert pool is created_pools[0]
        assert target_dimension == 1536
        assert dry_run is False
        assert embedding_service is _FakeEmbeddingService.instances[0]
        migrate_calls.append((spec.target_name, pool.label))
        return TableMigrationResult(
            target_name=spec.target_name,
            table_name=spec.table_name,
            dry_run=False,
            table_exists=True,
            current_dimension=512,
            target_dimension=1536,
            schema_changed=True,
            row_total=1,
            updated_rows=1,
            failed_rows=0,
        )

    fake_embedding_module = types.ModuleType(
        "komari_bot.plugins.embedding_provider.embedding_service"
    )
    cast("Any", fake_embedding_module).EmbeddingService = _FakeEmbeddingService
    monkeypatch.setitem(
        sys.modules,
        "komari_bot.plugins.embedding_provider.embedding_service",
        fake_embedding_module,
    )
    monkeypatch.setattr(module, "create_postgres_pool", _fake_create_pool)
    monkeypatch.setattr(module, "migrate_table_embeddings", _fake_migrate)

    asyncio.run(
        module.main_async(
            shared_db_config_path=tmp_path / "database.json",
            knowledge_config_path=tmp_path / "knowledge.json",
            memory_config_path=tmp_path / "memory.json",
            embedding_config_path=tmp_path / "embedding.json",
            targets={"knowledge", "memory"},
            apply=True,
        )
    )

    assert len(created_pools) == 1
    assert migrate_calls == [("knowledge", "komari_bot"), ("memory", "komari_bot")]
    assert len(_FakeEmbeddingService.instances) == 1
    assert _FakeEmbeddingService.instances[0].cleaned is True
    assert created_pools[0].closed is True


def test_main_async_apply_cleans_up_on_migration_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    module = _load_script_module()
    _FakeEmbeddingService.instances.clear()
    db_config = _make_db_config()
    created_pools: list[_FakePool] = []

    monkeypatch.setattr(
        module,
        "load_embedding_config",
        lambda _path: types.SimpleNamespace(
            embedding_model="test-model",
            embedding_dimension=1536,
        ),
    )
    monkeypatch.setattr(
        module, "resolve_knowledge_database_config", lambda **_kwargs: db_config
    )
    monkeypatch.setattr(
        module, "resolve_memory_database_config", lambda **_kwargs: db_config
    )

    async def _fake_create_pool(
        config: DatabaseConfigSchema, *, command_timeout: int
    ) -> _FakePool:
        assert command_timeout == 60
        pool = _FakePool(config.pg_database)
        created_pools.append(pool)
        return pool

    async def _raise_migrate(*_args: Any, **_kwargs: Any) -> TableMigrationResult:
        raise RuntimeError("boom")

    fake_embedding_module = types.ModuleType(
        "komari_bot.plugins.embedding_provider.embedding_service"
    )
    cast("Any", fake_embedding_module).EmbeddingService = _FakeEmbeddingService
    monkeypatch.setitem(
        sys.modules,
        "komari_bot.plugins.embedding_provider.embedding_service",
        fake_embedding_module,
    )
    monkeypatch.setattr(module, "create_postgres_pool", _fake_create_pool)
    monkeypatch.setattr(module, "migrate_table_embeddings", _raise_migrate)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            module.main_async(
                shared_db_config_path=tmp_path / "database.json",
                knowledge_config_path=tmp_path / "knowledge.json",
                memory_config_path=tmp_path / "memory.json",
                embedding_config_path=tmp_path / "embedding.json",
                targets={"knowledge"},
                apply=True,
            )
        )

    assert len(_FakeEmbeddingService.instances) == 1
    assert _FakeEmbeddingService.instances[0].cleaned is True
    assert created_pools[0].closed is True
