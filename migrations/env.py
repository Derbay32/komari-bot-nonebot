from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from alembic import context
from nonebot_plugin_orm import AlembicConfig, plugin_config
from sqlalchemy.util import await_only
from sqlmodel import SQLModel

from komari_bot.common.typed_config import (
    load_all_plugin_orm_models,
    load_all_typed_config_models,
)

if TYPE_CHECKING:
    from sqlalchemy import Connection
    from sqlalchemy.ext.asyncio import AsyncEngine

# Alembic Config 对象, 它提供正在使用的 .ini 文件中的值.
config = cast("AlembicConfig", context.config)

# 默认 AsyncEngine
engine: AsyncEngine = config.attributes["engines"][""]

# 模型的 MetaData, 用于 "autogenerate" 支持.
# nonebot-plugin-orm 默认元数据之外，合并动态配置强类型表的 SQLModel
# 元数据。load_all_typed_config_models 只按源文件加载各插件
# config_schema（不执行插件包 __init__，不访问数据库）；业务关系表的
# SQLModel 模型（orm_models.py）走同一套安全加载器，避免执行插件入口。
load_all_typed_config_models()
load_all_plugin_orm_models()
target_metadata: Any = [
    config.attributes["metadatas"][""],
    SQLModel.metadata,
]

# 其他来自 config 的值, 可以按 env.py 的需求定义, 例如可以获取:
# my_important_option = config.get_main_option("my_important_option")
# ... 等等.


def include_object(
    _obj: object,
    _name: str | None,
    type_: str,
    reflected: bool,  # noqa: FBT001 Alembic include_object 约定签名
    compare_to: object | None,
) -> bool:
    """禁止 autogenerate 为 metadata 之外的表生成 drop。

    基线迁移 0001 以原生 SQL 创建的表（含保留的 legacy 配置表）不进入
    任何 ORM metadata；nonebot-plugin-orm 自带的 no_drop_table 只在
    check 命令下过滤，revision 命令仍会把它们误判为待删除。这里对所有
    命令统一过滤：删除表必须手写迁移，绝不依赖 autogenerate。
    """
    return not (type_ == "table" and reflected and compare_to is None)


def run_migrations_offline() -> None:
    """在“离线”模式下运行迁移.

    虽然这里也可以获得 Engine, 但我们只需要一个 URL 即可配置 context.
    通过跳过 Engine 的创建, 我们甚至不需要 DBAPI 可用.

    在这里调用 context.execute() 会将给定的字符串写入到脚本输出.
    """

    opts: dict[str, Any] = {
        "url": engine.url,
        "dialect_opts": {"paramstyle": "named"},
        "target_metadata": target_metadata,
        "literal_binds": True,
    } | plugin_config.alembic_context
    context.configure(**opts)

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    opts: dict[str, Any] = {
        "connection": connection,
        "render_as_batch": True,
        "target_metadata": target_metadata,
        "include_object": include_object,
    } | plugin_config.alembic_context
    context.configure(**opts)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在“在线”模式下运行迁移.

    这种情况下, 我们需要为 context 创建一个连接.
    """

    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    coro = run_migrations_online()

    try:
        asyncio.run(coro)
    except RuntimeError:
        await_only(coro)
