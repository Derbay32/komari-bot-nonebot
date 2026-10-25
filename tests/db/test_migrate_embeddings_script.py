"""独立向量迁移脚本测试（TSK-197 后仅保留 knowledge 目标）。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts/migrate_embeddings.py"


def _load_script_module(module_name: str = "tests.scripts.migrate_embeddings_script") -> Any:
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakePool:
    def __init__(self, conn: "_FakeConnection") -> None:
        self.conn = conn
        self.closed = False

    def acquire(self) -> "_FakeAcquire":
        return _FakeAcquire(self.conn)

    async def close(self) -> None:
        self.closed = True


class _FakeAcquire:
    def __init__(self, conn: "_FakeConnection") -> None:
        self.conn = conn

    async def __aenter__(self) -> "_FakeConnection":
        return self.conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _FakeConnection:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: object) -> object:
        self.fetchval_calls.append((query, args))
        if "to_regclass" in query:
            return True
        if "COUNT" in query:
            return 2
        return None

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.fetchrow_calls.append((query, args))
        return {"atttypmod": 512}

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        return [{"row_id": 1, "text": "文本"}]

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "OK"

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()


def test_expand_targets_selects_only_knowledge() -> None:
    module = _load_script_module()

    assert [target.name for target in module.expand_targets({"knowledge"})] == [
        "knowledge"
    ]
    assert [target.name for target in module.expand_targets({"all"})] == ["knowledge"]
    assert [target.name for target in module.expand_targets(set())] == ["knowledge"]
    with pytest.raises(ValueError, match="不支持的迁移目标: memory"):
        module.expand_targets({"memory"})


def test_cli_rejects_memory_before_any_database_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module("tests.scripts.migrate_embeddings_cli")
    monkeypatch.setattr(sys, "argv", ["migrate_embeddings", "--target", "memory"])

    with pytest.raises(SystemExit) as exc_info:
        module.parse_args()

    assert exc_info.value.code == 2


def test_main_async_dry_run_reuses_pool_and_reports_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module()
    conn = _FakeConnection()
    pool = _FakePool(conn)

    async def _fake_create_pool(dsn: str, *, command_timeout: int) -> _FakePool:
        assert dsn == "postgresql+asyncpg://user:pass@localhost:5432/komari_bot"
        assert command_timeout == 60
        return pool

    monkeypatch.setattr(module.asyncpg, "create_pool", _fake_create_pool)

    asyncio.run(
        module.main_async(
            targets={"all"},
            apply=False,
            dsn="postgresql+asyncpg://user:pass@localhost:5432/komari_bot",
            embedding_config=module.EmbeddingConfig(
                model="test-model",
                dimension=1536,
                api_url="",
                api_key="",
            ),
        )
    )

    assert pool.closed is True
    assert [call[1][0] for call in conn.fetchval_calls if "to_regclass" in call[0]] == [
        "komari_knowledge"
    ]
    assert conn.execute_calls == []


def test_migrate_knowledge_target_apply_writes_source_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module()
    conn = _FakeConnection()
    pool = _FakePool(conn)

    async def _fake_request_embedding(text: str, config: Any) -> str:
        assert text == "文本"
        assert config.model == "test-model"
        return "[0.1,0.2]"

    monkeypatch.setattr(module, "request_embedding", _fake_request_embedding)

    result = asyncio.run(
        module.migrate_target(
            pool,
            target=module.KNOWLEDGE_TARGET,
            embedding_config=module.EmbeddingConfig(
                model="test-model",
                dimension=1536,
                api_url="http://example.test/embeddings",
                api_key="key",
            ),
            dry_run=False,
        )
    )

    assert result.updated_rows == 1
    assert any(
        "ALTER TABLE komari_knowledge ADD COLUMN embedding VECTOR(1536)" in call[0]
        for call in conn.execute_calls
    )
    assert any(
        "UPDATE komari_knowledge SET embedding = $1" in call[0]
        for call in conn.execute_calls
    )


def test_script_imports_without_komari_bot_package(monkeypatch: Any) -> None:
    original_modules = dict(sys.modules)
    for name in list(sys.modules):
        if name == "komari_bot" or name.startswith("komari_bot."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "不存在的路径"))

    module = _load_script_module("tests.scripts.migrate_embeddings_script_isolated")

    assert "komari_bot" not in sys.modules
    assert module.expand_targets({"all"})[0].source_table == "komari_knowledge"

    sys.modules.clear()
    sys.modules.update(original_modules)
