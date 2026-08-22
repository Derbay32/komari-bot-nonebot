"""TSK-224 全局事件门禁：NoneBot event_preprocessor 实现。

本模块在 ``group_admission`` 包导入时自动注册一个 ``event_preprocessor``
前置处理器，拦截所有 NoneBot 事件并按群聊准入策略进行粗门禁裁决。

事件分类（基于精确 OneBot V11 具体运行时类，禁止 isinstance 泛化吸纳新子类）：

- system_meta（MetaEvent / LifecycleMetaEvent / HeartbeatMetaEvent）：
  直接放行，不做任何准入裁决；
- private_input（PrivateMessageEvent）：静默拒绝，记录一次
  ``private_input_rejected`` 遥测，抛出 ``IgnoredException``；
- group_business（12 类）：提取 ``group_id``（仅当为正整数时），以
  ``BUSINESS`` 意图进行准入裁决；获准则放行，否则抛出 ``IgnoredException``；
- unsupported failclosed（其他所有事件，含基类、好友事件、未知子类）：
  以空关联群 ``BUSINESS`` 裁决（恒定被拒），抛出 ``IgnoredException``。

事件族（``event_family``）基于类层次确定，只取封闭值：
``message`` / ``notice`` / ``request`` / ``unknown``。
"""

from __future__ import annotations

from nonebot.adapters.onebot.v11.event import (
    Event,
    GroupAdminNoticeEvent,
    GroupBanNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    GroupRequestEvent,
    GroupUploadNoticeEvent,
    HeartbeatMetaEvent,
    HonorNotifyEvent,
    LifecycleMetaEvent,
    LuckyKingNotifyEvent,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    NotifyEvent,
    PokeNotifyEvent,
    PrivateMessageEvent,
    RequestEvent,
)
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor

from . import runtime as _runtime_module
from .contracts import AdmissionIntent, AdmissionQualification

# 3 类系统元事件闭集：精确运行时类身份（不接纳子类）。
_SYSTEM_META_CLASSES: frozenset[type[Event]] = frozenset({
    MetaEvent,
    LifecycleMetaEvent,
    HeartbeatMetaEvent,
})

# 12 类群业务事件闭集：frozenset 精确运行时类身份（不接纳子类）。
# 精确锁定 OneBot V11 具体类型，不通过 getattr/泛化继承接纳新适配器子类。
_GROUP_BUSINESS_CLASSES: frozenset[type[Event]] = frozenset({
    GroupMessageEvent,
    GroupUploadNoticeEvent,
    GroupAdminNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupBanNoticeEvent,
    GroupRecallNoticeEvent,
    NotifyEvent,
    PokeNotifyEvent,
    LuckyKingNotifyEvent,
    HonorNotifyEvent,
    GroupRequestEvent,
})


def _event_family(event: Event) -> str:
    """根据事件祖先确定封闭事件族（message / notice / request / unknown）。

    仅用于安全闭族遥测（event_family），不参与门禁分类裁决。
    MetaEvent 应在调用本函数前被处理，不会进入此分支。
    """
    if isinstance(event, MessageEvent):
        return "message"
    if isinstance(event, NoticeEvent):
        return "notice"
    if isinstance(event, RequestEvent):
        return "request"
    return "unknown"


@event_preprocessor
async def _admission_event_gate(event: Event) -> None:
    """全局事件前置处理器：按群聊准入策略进行粗门禁裁决。

    1. system_meta → 直接放行；
    2. private_input → 静默拒绝；
    3. group_business → 提取 group_id 并裁决；获准放行，否则拒绝；
    4. unsupported failclosed → 以空关联群裁决，恒定拒绝。
    """
    # 运行时精确类身份：一次捕获，用于所有闭集检查（禁止 isinstance 泛化吸纳新子类）
    event_class = type(event)

    # 1. system_meta：精确运行时类身份匹配三种 MetaEvent，直接放行
    if event_class in _SYSTEM_META_CLASSES:
        return

    # 2. private_input：精确运行时类身份匹配 PrivateMessageEvent → 静默拒绝
    if event_class is PrivateMessageEvent:
        _runtime_module._runtime.record_private_input_rejected(
            event_family="message"
        )
        raise IgnoredException("group_admission_rejected") from None

    # 3. group_business：精确运行时类身份匹配 12 类群业务事件闭集
    if event_class in _GROUP_BUSINESS_CLASSES:
        raw_group_id = getattr(event, "group_id", None)
        if type(raw_group_id) is int and raw_group_id > 0:
            group_ids: list[int] = [raw_group_id]
        else:
            group_ids = []
        family = _event_family(event)
        result = _runtime_module._runtime.adjudicate(
            group_ids, intent=AdmissionIntent.BUSINESS, event_family=family
        )
        if result.qualification is AdmissionQualification.BUSINESS:
            return
        raise IgnoredException("group_admission_rejected") from None

    # 4. unsupported failclosed：所有未列在以上闭集中的事件（含基类、子类、未被
    #    显式列出的未知事件类型），以空关联群 BUSINESS 裁决（恒定被拒）
    family = _event_family(event)
    _runtime_module._runtime.adjudicate(
        [], intent=AdmissionIntent.BUSINESS, event_family=family
    )
    raise IgnoredException("group_admission_rejected") from None
