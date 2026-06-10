"""user_data 数据库访问层测试。"""

from __future__ import annotations

import pytest

from komari_bot.plugins.user_data.config_schema import DynamicConfigSchema
from komari_bot.plugins.user_data.database import UserDataDB


def _build_db() -> UserDataDB:
    return UserDataDB(DynamicConfigSchema())


@pytest.mark.asyncio
async def test_get_user_favorability_requires_initialized_pool() -> None:
    db = _build_db()

    with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
        await db.get_user_favorability("1047195267")


@pytest.mark.asyncio
async def test_get_user_count_requires_initialized_pool() -> None:
    db = _build_db()

    with pytest.raises(RuntimeError, match="UserDataDB 连接池未初始化"):
        await db.get_user_count()
