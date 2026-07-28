"""独立迁移脚本测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

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


def test_expand_memory_targets_contains_two_memory_tables() -> None:
    module = _load_script_module()

    targets = module.expand_targets({"memory"})

    assert [target.name for target in targets] == [
        "memory_conversations",
        "memory_interactions",
    ]
    assert targets[0].embedding_table == "komari_memory_conversation_embeddings"
    assert targets[1].embedding_table == "komari_memory_interaction_embeddings"


def test_main_async_dry_run_reuses_pool_and_reports_memory_targets(monkeypatch: Any) -> None:
    module = _load_script_module()
    conn = _FakeConnection()
    pool = _FakePool(conn)

    async def _fake_create_pool(config: Any, *, command_timeout: int) -> _FakePool:
        assert config.pg_database == "komari_bot"
        assert command_timeout == 60
        return pool

    monkeypatch.setattr(module, "create_postgres_pool", _fake_create_pool)

    asyncio.run(
        module.main_async(
            targets={"memory"},
            apply=False,
            database_config=module.DatabaseConfig(
                pg_host="localhost",
                pg_port=5432,
                pg_database="komari_bot",
                pg_user="user",
                pg_password="pass",
            ),
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
        "komari_memory_conversations",
        "komari_memory_conversation_embeddings",
        "komari_memory_interaction_history",
        "komari_memory_interaction_embeddings",
    ]
    assert conn.execute_calls == []


def test_migrate_memory_target_apply_writes_embedding_side_table(monkeypatch: Any) -> None:
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
            target=module.CONVERSATION_MEMORY_TARGET,
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
        "CREATE TABLE IF NOT EXISTS komari_memory_conversation_embeddings" in call[0]
        for call in conn.execute_calls
    )
    assert any(
        "INSERT INTO komari_memory_conversation_embeddings" in call[0]
        for call in conn.execute_calls
    )


def test_script_imports_without_komari_bot_package(monkeypatch: Any) -> None:
    original_modules = dict(sys.modules)
    for name in list(sys.modules):
        if name == "komari_bot" or name.startswith("komari_bot."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "不存在的路径"))

    module = _load_script_module("tests.scripts.migrate_embeddings_script_isolated")

    assert "komari_bot.common" not in sys.modules
    assert module.expand_targets({"memory"})[0].source_table == "komari_memory_conversations"

    sys.modules.clear()
    sys.modules.update(original_modules)
