"""Redis 共享配置读取测试（ticket 11：PG 连接配置退役后仅剩 Redis）。"""

from __future__ import annotations

from typing import Any

import nonebot
import pytest

from komari_bot.common import redis_config as redis_config_module
from komari_bot.common.redis_config import (
    RedisConfigSchema,
    get_shared_redis_config,
    load_redis_config_from_env,
)


def test_redis_config_reads_uppercase_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("REDIS_HOST", "redis.example")
    monkeypatch.setenv("REDIS_PORT", "16379")
    monkeypatch.setenv("REDIS_PASSWORD", "redis-secret")

    config = load_redis_config_from_env()

    assert config.redis_host == "redis.example"
    assert config.redis_port == 16379
    assert config.redis_password == "redis-secret"


def test_redis_config_has_no_postgres_fields(monkeypatch: Any) -> None:
    monkeypatch.setenv("REDIS_HOST", "redis.example")
    monkeypatch.setenv("PG_HOST", "postgres.example")
    monkeypatch.setenv("PG_USER", "legacy-user")
    monkeypatch.setenv("PG_PASSWORD", "legacy-pass")
    monkeypatch.setenv("PG_POOL_MAX_SIZE", "9")

    config = load_redis_config_from_env()

    assert config.redis_host == "redis.example"
    for retired_field in (
        "pg_host",
        "pg_port",
        "pg_database",
        "pg_user",
        "pg_password",
        "pg_pool_min_size",
        "pg_pool_max_size",
        "pg_pool_process_budget",
    ):
        with pytest.raises(AttributeError):
            getattr(config, retired_field)


def test_redis_config_schema_rejects_no_inconsistent_pool_fields() -> None:
    config = RedisConfigSchema(redis_host="localhost", redis_port=6379)
    assert config.redis_password == ""


def test_runtime_plugin_config_error_does_not_fall_back_to_environment(
    monkeypatch: Any,
) -> None:
    get_shared_redis_config.cache_clear()
    monkeypatch.setenv("REDIS_HOST", "wrong-fallback-redis")
    monkeypatch.setattr(nonebot, "get_driver", lambda: object())

    def _raise_config_error(_schema: object) -> object:
        raise RuntimeError("运行时配置损坏")

    monkeypatch.setattr(nonebot, "get_plugin_config", _raise_config_error)

    with pytest.raises(RuntimeError, match="运行时配置损坏"):
        get_shared_redis_config()
    get_shared_redis_config.cache_clear()


def test_uninitialized_nonebot_explicitly_uses_environment(monkeypatch: Any) -> None:
    get_shared_redis_config.cache_clear()
    monkeypatch.setenv("REDIS_PORT", "17000")

    def _raise_uninitialized() -> object:
        message = "NoneBot has not been initialized."
        raise ValueError(message)

    monkeypatch.setattr(nonebot, "get_driver", _raise_uninitialized)

    config = redis_config_module.get_shared_redis_config()

    assert config.redis_port == 17000
    get_shared_redis_config.cache_clear()
