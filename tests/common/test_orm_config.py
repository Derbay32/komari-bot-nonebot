"""nonebot-plugin-orm 权威数据库 URL 解析辅助测试（ticket 11）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from komari_bot.common.orm_config import (
    get_orm_database_url,
    is_orm_database_url_configured,
)


def _uninitialized_driver() -> Any:
    def _raise() -> object:
        message = "NoneBot has not been initialized."
        raise ValueError(message)

    return _raise


def test_get_orm_database_url_prefers_real_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SQLALCHEMY_DATABASE_URL",
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot",
    )

    assert get_orm_database_url() == (
        "postgresql+asyncpg://user:pass@localhost:5432/komari_bot"
    )


def test_get_orm_database_url_uses_driver_config_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nonebot

    monkeypatch.delenv("SQLALCHEMY_DATABASE_URL", raising=False)
    fake_driver = SimpleNamespace(
        config=SimpleNamespace(
            sqlalchemy_database_url=(
                "postgresql+asyncpg://dotenv-user:dotenv-pass@db:5432/komari"
            ),
        )
    )
    monkeypatch.setattr(nonebot, "get_driver", lambda: fake_driver)

    assert get_orm_database_url() == (
        "postgresql+asyncpg://dotenv-user:dotenv-pass@db:5432/komari"
    )


def test_get_orm_database_url_raises_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nonebot

    monkeypatch.delenv("SQLALCHEMY_DATABASE_URL", raising=False)
    monkeypatch.setattr(nonebot, "get_driver", _uninitialized_driver())

    with pytest.raises(RuntimeError, match="SQLALCHEMY_DATABASE_URL"):
        get_orm_database_url()


def test_get_orm_database_url_ignores_driver_when_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nonebot

    monkeypatch.setenv(
        "SQLALCHEMY_DATABASE_URL",
        "postgresql+asyncpg://env-user:env-pass@localhost:5432/komari_bot",
    )
    fake_driver = SimpleNamespace(
        config=SimpleNamespace(
            sqlalchemy_database_url=(
                "postgresql+asyncpg://dotenv-user:dotenv-pass@db:5432/komari"
            ),
        )
    )
    monkeypatch.setattr(nonebot, "get_driver", lambda: fake_driver)

    assert get_orm_database_url() == (
        "postgresql+asyncpg://env-user:env-pass@localhost:5432/komari_bot"
    )


def test_is_orm_database_url_configured_reflects_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nonebot

    monkeypatch.delenv("SQLALCHEMY_DATABASE_URL", raising=False)
    monkeypatch.setattr(nonebot, "get_driver", _uninitialized_driver())
    assert is_orm_database_url_configured() is False

    monkeypatch.setenv("SQLALCHEMY_DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    assert is_orm_database_url_configured() is True
