"""nonebot-plugin-orm 迁移命令引导入口。

在容器 prestart 阶段（Gunicorn 启动应用进程前）执行 Alembic 迁移命令。
本模块不依赖 nb-cli：它先初始化 NoneBot 并只加载 ORM 基础设施，随后把
命令行参数转发给 nonebot-plugin-orm 自带的 CLI 入口。迁移命令不导入业务
插件，避免在 Alembic 连接建立前触发旧连接池或其他运行时副作用。

迁移失败时 CLI 以非零退出码结束，``docker/start.sh`` 的 ``set -e``
会随之中止容器启动（fail fast）。

用法示例::

    python -m komari_bot.common.orm_bootstrap upgrade head
    python -m komari_bot.common.orm_bootstrap heads
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def _bootstrap_nonebot() -> None:
    """初始化 NoneBot 并只加载迁移所需的 ORM 插件。"""
    from komari_bot.common.nonebot_compat import (
        install_nonebot_forwardref_compatibility,
    )

    install_nonebot_forwardref_compatibility()

    import nonebot

    nonebot.init()
    if nonebot.load_plugin("nonebot_plugin_orm") is None:
        msg = "nonebot-plugin-orm 加载失败，无法执行数据库迁移"
        raise RuntimeError(msg)


def main(argv: Sequence[str] | None = None) -> None:
    """初始化应用上下文后转发到 nonebot-plugin-orm CLI。

    ``nb orm`` 官方流程由 nb-cli 重写 ``sys.argv`` 后调用入口函数，
    Click 独立模式按 ``sys.argv`` 解析命令；这里保持同样的调用语义。

    Args:
        argv: 转发给迁移 CLI 的参数；缺省使用进程命令行参数。
    """
    _bootstrap_nonebot()

    from nonebot_plugin_orm.__main__ import main as orm_cli_main

    if argv is not None:
        sys.argv = [sys.argv[0], *argv]
    orm_cli_main()


if __name__ == "__main__":
    main()
