"""可选的配置多 worker PostgreSQL 集成测试。"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest

from komari_bot.common.database_config import DatabaseConfigSchema
from komari_bot.plugins.config_manager import storage as storage_module
from komari_bot.plugins.config_manager.storage import ConfigStorage, StoredConfig

POSTGRES_URL = os.getenv("KOMARI_TEST_POSTGRES_URL", "")


def _database_config() -> DatabaseConfigSchema:
    parsed = urlparse(POSTGRES_URL)
    return DatabaseConfigSchema(
        pg_host=parsed.hostname or "127.0.0.1",
        pg_port=parsed.port or 5432,
        pg_database=parsed.path.lstrip("/") or "postgres",
        pg_user=unquote(parsed.username or "postgres"),
        pg_password=unquote(parsed.password or ""),
        pg_pool_min_size=1,
        pg_pool_max_size=1,
    )


@pytest.mark.skipif(not POSTGRES_URL, reason="未配置真实 PostgreSQL 测试连接")
def test_config_initialization_and_revision_propagate_between_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _database_config()
    monkeypatch.setattr(storage_module, "get_shared_database_config", lambda: config)
    first = ConfigStorage()
    second = ConfigStorage()
    plugin_name = f"integration-config-{uuid4().hex}"
    changed = threading.Event()
    observed: list[StoredConfig] = []

    def _observe(snapshot: StoredConfig) -> None:
        observed.append(snapshot)
        changed.set()

    second.register_watcher(
        plugin_name,
        _observe,
        max_staleness_seconds=0.2,
    )
    try:
        first.ensure_schema()
        second.ensure_schema()

        def _initialize(value: int, storage: ConfigStorage) -> StoredConfig:
            return storage.insert_if_absent(
                plugin_name=plugin_name,
                schema_name="IntegrationConfig",
                config_data={"value": value},
                version="1.0",
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda args: _initialize(*args),
                    ((1, first), (2, second)),
                )
            )

        assert results[0].config_data == results[1].config_data
        assert results[0].revision == results[1].revision == 1
        changed.wait(timeout=3)
        changed.clear()
        observed.clear()

        updated = first.update_fields_if_revision(
            plugin_name=plugin_name,
            schema_name="IntegrationConfig",
            config_patch={"value": 3},
            version="1.0",
            expected_revision=1,
        )

        assert updated is not None
        assert updated.revision == 2
        assert changed.wait(timeout=3) is True
        assert any(
            snapshot.revision == 2 and snapshot.config_data["value"] == 3
            for snapshot in observed
        )
    finally:
        first.close()
        second.close()
