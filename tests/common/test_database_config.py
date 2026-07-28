"""共享数据库 dotenv 配置读取测试。"""

from __future__ import annotations

from typing import Any

from komari_bot.common.database_config import load_database_config_from_env


def test_database_config_reads_uppercase_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("PG_HOST", "postgres.example")
    monkeypatch.setenv("PG_PORT", "15432")
    monkeypatch.setenv("PG_DATABASE", "komari_test")
    monkeypatch.setenv("PG_USER", "komari_user")
    monkeypatch.setenv("PG_PASSWORD", "secret")
    monkeypatch.setenv("PG_POOL_MIN_SIZE", "3")
    monkeypatch.setenv("PG_POOL_MAX_SIZE", "9")
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
    assert config.redis_host == "redis.example"
    assert config.redis_port == 16379
    assert config.redis_password == "redis-secret"
