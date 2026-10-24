"""Komari Decision - Jev 类型化判定插件入口。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot.plugin import PluginMetadata, require

# 判定运行时、诊断路由与生命周期装配的硬依赖必须在 import 对应模块前声明。
require("config_manager")
require("group_admission")
require("nonebot_plugin_apscheduler")

from .management_api import register_decision_api
from .services.jev_dispatch import get_decision_dispatch, observe_delivered_reply
from .services.jev_runtime import get_decision_runtime

if TYPE_CHECKING:
    from .services.jev_engine import JevDecisionEngine

__plugin_meta__ = PluginMetadata(
    name="小鞠判定",
    description="Jev 类型化判定、单次业务交接与判定诊断",
    usage="被其他插件通过服务接口调用",
)

__all__ = [
    "get_decision_dispatch",
    "get_decision_engine",
    "observe_delivered_reply",
    "register_decision_api",
]


def get_decision_engine() -> JevDecisionEngine | None:
    """返回常驻判定运行时当前持有的真实引擎（唯一公共引擎 getter）。

    运行时在读取可能非法的模型配置**之前**就已经构建引擎，因此本地存储健康
    但配置暂不可用时调用方仍能取得真实引擎；真正没有引擎（运行时尚未初始化、
    已关闭或初始化失败）时返回 None，由调用方按既有故障关闭语义安全停止。
    没有第二条引擎构建/缓存路径，也不把缺失引擎当作可绕过持久身份保护。
    """
    return get_decision_runtime().engine


try:
    from nonebot import get_driver

    driver = get_driver()
except ValueError:
    driver = None

if driver is not None:
    # 生产生命周期与调度装配只在 Driver 就绪时加载；本插件不再注册旧场景钩子。
    from .services import jev_lifecycle  # noqa: F401 — 生产生命周期与调度装配
