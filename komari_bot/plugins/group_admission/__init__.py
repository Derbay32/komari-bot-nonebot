"""统一群聊准入插件。

拥有群聊准入策略的解释、不可变策略修订快照、最近有效策略（LKG）、冷
启动状态与同步准入裁决面。业务插件只消费准入裁决，不自行解释策略；见
ADR-0012 与 CONTEXT.md「群聊准入」。

业务调用面只有两个同步无 I/O 的 callable：``adjudicate`` 与
``get_runtime_state``，顶层同时重导出五个契约类型身份。本票不注册
driver hooks、管理 HTTP Adapter、遥测或事件前置钩子；生产装配由后续
票落地。
"""

from __future__ import annotations

# 签名注解必须在运行时可解析：typing.get_type_hints() 依赖本符号
from collections.abc import Collection  # noqa: TC003

from nonebot.plugin import PluginMetadata, require

from . import runtime as _runtime_module
from .contracts import (
    AdmissionIntent,
    AdmissionQualification,
    AdmissionResult,
    AdmissionRuntimeState,
    AdmissionRuntimeStatus,
)

require("config_manager")

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
