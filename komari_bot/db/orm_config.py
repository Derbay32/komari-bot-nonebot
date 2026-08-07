"""nonebot-plugin-orm 权威数据库 URL（SQLALCHEMY_DATABASE_URL）解析辅助。

v2.0.0 起数据库连接唯一权威是 nonebot-plugin-orm 的 ``SQLALCHEMY_DATABASE_URL``
（Pydantic settings，真实环境变量优先级高于 dotenv 文件）。本模块供两类
消费方复用：

- 同步桥（prompt_storage / config_manager storage 的一次性短命引擎）；
- 插件启动守卫（komari_knowledge / komari_help / komari_custom 的预检）。

解析顺序与 nonebot 配置合并语义保持一致：

1. 真实环境变量 ``SQLALCHEMY_DATABASE_URL``——它在 nonebot 的 settings
   合并中永远覆盖 dotenv，直接读 ``os.environ`` 得到的就是最终生效值；
2. NoneBot driver 已加载配置（含 ``.env`` / ``.env.{ENVIRONMENT}`` 的
   dotenv 值，字段名大小写不敏感）——仅当环境变量缺失时读取；
3. 两者都缺失则抛出 ``RuntimeError``，调用方按「数据库未配置」处理。
"""

from __future__ import annotations

import os
from typing import Any, cast


def get_orm_database_url() -> str:
    """返回 nonebot-plugin-orm 权威数据库 URL，未配置时抛出 RuntimeError。"""
    url = os.environ.get("SQLALCHEMY_DATABASE_URL")
    if url:
        return url

    try:
        from nonebot import get_driver

        driver = get_driver()
    except (ImportError, ValueError):
        driver = None

    if driver is not None:
        config = cast("Any", driver.config)
        url = config.sqlalchemy_database_url
        if isinstance(url, str) and url:
            return url

    msg = (
        "未配置 SQLALCHEMY_DATABASE_URL，无法建立数据库连接；"
        "请在环境变量或 dotenv 中设置该连接串"
    )
    raise RuntimeError(msg)


def is_orm_database_url_configured() -> bool:
    """SQLALCHEMY_DATABASE_URL 是否已配置（供插件启动守卫预检使用）。"""
    try:
        get_orm_database_url()
    except RuntimeError:
        return False
    return True


__all__ = ["get_orm_database_url", "is_orm_database_url_configured"]
