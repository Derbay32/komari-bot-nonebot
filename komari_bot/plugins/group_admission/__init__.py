"""统一群聊准入插件。

拥有群聊准入策略的解释、不可变策略修订快照、最近有效策略（LKG）、冷
启动状态与同步准入裁决面。业务插件只消费准入裁决，不自行解释策略；见
ADR-0012 与 CONTEXT.md「群聊准入」。

业务调用面只有两个同步无 I/O 的 callable：``adjudicate`` 与
``get_runtime_state``，顶层同时重导出五个契约类型身份与
``register_group_admission_api`` 管理控制面装配入口。

TSK-223 已落地管理 HTTP Adapter（``register_group_admission_api``，经
``komari_management`` 装配）与基础 status / telemetry 投影。
TSK-224 已挂载全局事件前置钩子（``event_preprocessor``，经
``event_gate`` 模块 import 时自动注册）。TSK-248 已落地生产生命周期：
经 ``lifecycle`` 模块在 Driver 就绪时注册恰一个 startup / shutdown 钩子与
一个 interval 可观测性 job（startup 经 config_manager 顶层唯一 getter 获取
manager 并启动 runtime，shutdown 停 job、close 并注销 listener）。

装配顺序：本插件是 NoneBot 插件，入口在 import 任何依赖子模块前先经
``require`` 声明硬依赖 ``config_manager`` 与 ``nonebot_plugin_apscheduler``
（先声明后引用，见 AGENTS.md「插件架构与依赖关系」），随后再 import
event_gate / lifecycle / runtime / contracts / management_api。lifecycle 在
Driver 未初始化时（测试 / 工具在 ``nonebot.init()`` 前直接 import 本包）只
跳过生命周期装配，业务调用面与管理 Router 装配不受影响；受支持生产启动顺
序（``docker/bot.py`` 先 ``nonebot.init()`` 再 ``load_from_toml()``）保证插
件加载期 Driver 已就绪，不会走跳过分支。本包不承诺同进程 pre-init import
后自动补装生命周期。
"""

from __future__ import annotations

# 签名注解必须在运行时可解析：typing.get_type_hints() 依赖本符号
from collections.abc import Collection  # noqa: TC003

from nonebot.plugin import PluginMetadata, require

require("config_manager")
require("nonebot_plugin_apscheduler")

from . import (
    event_gate,  # noqa: F401 — TSK-224 自动注册事件前置处理器
    lifecycle,  # noqa: F401 — TSK-248 生产生命周期与观测调度装配
)
from . import runtime as _runtime_module
from .contracts import (
    AdmissionIntent,
    AdmissionQualification,
    AdmissionResult,
    AdmissionRuntimeState,
    AdmissionRuntimeStatus,
)
from .management_api import register_group_admission_api

__plugin_meta__ = PluginMetadata(
    name="group_admission",
    description="统一群聊准入：策略解释、版本化快照与同步准入裁决",
    usage="adjudicate(associated_group_ids, *, intent) / get_runtime_state()",
)

__all__ = [
    "AdmissionIntent",
    "AdmissionQualification",
    "AdmissionResult",
    "AdmissionRuntimeState",
    "AdmissionRuntimeStatus",
    "adjudicate",
    "get_runtime_state",
    "register_group_admission_api",
]


def adjudicate(
    associated_group_ids: Collection[int],
    *,
    intent: AdmissionIntent = AdmissionIntent.BUSINESS,
) -> AdmissionResult:
    """为紧随其后的单个不可分业务效果执行一次准入裁决。

    同步、无 I/O，只读取进程内不可变运行时快照。全部关联群获准方可开
    展业务；任一关联群受限，整个行为受限。``intent`` 声明行为目的，既
    成事实收尾与技术清理按各自资格规则授予，不得扩张解释为开展业务。
    """
    return _runtime_module._runtime.adjudicate(associated_group_ids, intent=intent)


def get_runtime_state() -> AdmissionRuntimeState:
    """单调用原子读取进程内不可变的准入运行时健康状态，同步、无 I/O。"""
    return _runtime_module._runtime.get_state()
