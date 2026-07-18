"""共享数据库 dotenv 配置读取测试。"""

from __future__ import annotations

from typing import Any

import nonebot
import pytest

from komari_bot.common import database_config as database_config_module
from komari_bot.common.database_config import (
    DatabaseConfigSchema,
    get_shared_database_config,
    load_database_config_from_env,
)


def test_database_config_reads_uppercase_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("PG_HOST", "postgres.example")
    monkeypatch.setenv("PG_PORT", "15432")
    monkeypatch.setenv("PG_DATABASE", "komari_test")
    monkeypatch.setenv("PG_USER", "komari_user")
    monkeypatch.setenv("PG_PASSWORD", "secret")
    monkeypatch.setenv("PG_POOL_MIN_SIZE", "3")
    monkeypatch.setenv("PG_POOL_MAX_SIZE", "9")
    monkeypatch.setenv("PG_POOL_PROCESS_BUDGET", "12")
    monkeypatch.setenv("REDIS_HOST", "redis.example")
    monkeypatch.setenv("REDIS_PORT", "16379")
    monkeypatch.setenv("REDIS_PASSWORD", "redis-secret")

    config = load_database_config_from_env()

    assert config.pg_host == "postgres.example"
    assert config.pg_port == 15432
    assert config.pg_database == "komari_test"
    assert config.pg_user == "komari_user"
    assert config.pg_password == "secret"
    assert config.pg_pool_min_size == 3
    assert config.pg_pool_max_size == 9
    assert config.pg_pool_process_budget == 12
    assert config.redis_host == "redis.example"
    assert config.redis_port == 16379
    assert config.redis_password == "redis-secret"


def test_database_config_rejects_inconsistent_pool_limits() -> None:
    with pytest.raises(ValueError, match="pg_pool_min_size"):
        DatabaseConfigSchema(pg_pool_min_size=5, pg_pool_max_size=4)

    with pytest.raises(ValueError, match="pg_pool_max_size"):
        DatabaseConfigSchema(pg_pool_max_size=6, pg_pool_process_budget=5)


def test_runtime_plugin_config_error_does_not_fall_back_to_environment(
    monkeypatch: Any,
) -> None:
    get_shared_database_config.cache_clear()
    monkeypatch.setenv("PG_DATABASE", "wrong-fallback-database")
    monkeypatch.setattr(nonebot, "get_driver", lambda: object())

    def _raise_config_error(_schema: object) -> object:
        raise RuntimeError("运行时配置损坏")

    monkeypatch.setattr(nonebot, "get_plugin_config", _raise_config_error)

    with pytest.raises(RuntimeError, match="运行时配置损坏"):
        get_shared_database_config()
    get_shared_database_config.cache_clear()


def test_uninitialized_nonebot_explicitly_uses_environment(monkeypatch: Any) -> None:
    get_shared_database_config.cache_clear()
    monkeypatch.setenv("PG_DATABASE", "script-database")

    def _raise_uninitialized() -> object:
        message = "NoneBot has not been initialized."
        raise ValueError(message)

    monkeypatch.setattr(nonebot, "get_driver", _raise_uninitialized)

    config = database_config_module.get_shared_database_config()

    assert config.pg_database == "script-database"
    get_shared_database_config.cache_clear()
