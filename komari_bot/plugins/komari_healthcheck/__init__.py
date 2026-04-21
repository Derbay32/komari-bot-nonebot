"""Komari Healthcheck 健康检查插件。"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import FastAPI, Response
from nonebot import get_bots, get_driver, logger
from nonebot.plugin import PluginMetadata, require

from .config_schema import HealthCheckConfig

config_manager_plugin = require("config_manager")
config_manager = config_manager_plugin.get_config_manager(
    "komari_healthcheck",
    HealthCheckConfig,
)

__plugin_meta__ = PluginMetadata(
    name="komari_healthcheck",
    description="提供无认证健康检查端点，供外部系统探测机器人在线状态",
    usage="自动运行，无需命令",
    config=HealthCheckConfig,
)

try:
    driver = get_driver()
except ValueError:
    driver = None


def _route_methods(route: object) -> set[str]:
    methods = getattr(route, "methods", None)
    if not isinstance(methods, Iterable):
        return set()
    return {str(method).upper() for method in methods}


def register_healthcheck_route(app: FastAPI, config: HealthCheckConfig) -> bool:
    """向 FastAPI 应用注册健康检查端点。"""
    if getattr(app.state, "komari_healthcheck_registered", False):
        return False

    for route in app.routes:
        if getattr(route, "path", None) != config.endpoint_path:
            continue
        if "GET" not in _route_methods(route):
            continue

        logger.warning(
            f"[Komari Healthcheck] 端点 {config.endpoint_path} 已存在，跳过注册"
        )
        return False

    async def healthcheck() -> Response:
        if get_bots():
            return Response(
                content=config.online_message,
                media_type="text/plain",
                status_code=200,
            )
        return Response(
            content=config.offline_message,
            media_type="text/plain",
            status_code=503,
        )

    app.add_api_route(
        config.endpoint_path,
        healthcheck,
        methods=["GET"],
        include_in_schema=False,
        name="komari_healthcheck",
    )
    app.state.komari_healthcheck_registered = True
    logger.info(f"[Komari Healthcheck] 健康检查端点已注册: {config.endpoint_path}")
    return True


if driver is not None:

    @driver.on_startup
    async def on_startup() -> None:
        """插件启动时挂载健康检查端点。"""
        config = config_manager.get()
        if not config.plugin_enable:
            logger.info("[Komari Healthcheck] 插件未启用，跳过健康检查端点注册")
            return

        driver_type = getattr(driver, "type", None)
        server_app = getattr(driver, "server_app", None)
        if driver_type != "fastapi" or not isinstance(server_app, FastAPI):
            logger.warning(
                "[Komari Healthcheck] 当前驱动不是 FastAPI，无法注册健康检查端点"
            )
            return

        register_healthcheck_route(server_app, config)


__all__ = ["register_healthcheck_route"]
