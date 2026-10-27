"""判定入站分发适配器（TSK-187）。

本模块只承担两件事：

1. 把**当前已连接**的 OneBot V11 群消息投影为一次分发的冻结判定请求，并在
   NoneBot ``T_State`` 中保存同一份共享载体（``get_decision_dispatch``）；
2. 把一条**已确认送达**的角色回复吸收进判定自有近期上下文
   （``observe_delivered_reply``），供既有回复履约承诺在记忆历史写入成功后
   尽力观测。

它不调用模型、不读写记忆模块、不发送消息、不实现遍历/截止/去重，也不提供
第二个引擎 getter（始终复用判定插件顶层 ``get_decision_engine``）。

事件前置处理器只做本地事实：写入一个全新的每分发送持有对象与随机分发标识。
它不读取配置、不访问存储、不调用平台 API，也不依赖与其它前置处理器的先后
顺序。同一 ``Event`` 对象被框架再次分发时得到新的持有对象与新的分发标识。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from nonebot import logger
from nonebot.adapters import Bot, Event  # noqa: TC002 - 前置处理器注解在运行期解析
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.message import event_preprocessor
from nonebot.typing import T_State  # noqa: TC002 - Nonebot2 在运行时解析参数注解

from komari_bot.decision.dispatch import (
    DecisionDispatch,
    DecisionDispatchUnavailableError,
)
from komari_bot.decision.jev import DecisionMessage, DecisionRequest, EventIdentity

from .jev_onebot import (
    PROTOCOL,
    canonical_message_id,
    canonical_platform_id,
    group_business_admitted,
    platform_message_time,
    project_native_segments,
    project_trusted_reply,
    sender_display_name,
)

if TYPE_CHECKING:
    from komari_bot.decision.engine_protocol import DecisionEngineProtocol

DISPATCH_STATE_KEY: Final = "komari_decision_dispatch"
"""NoneBot ``T_State`` 中保存本次分发持有对象的固定键。"""

_FUNCTIONAL_COMMAND_PREFIX_KEY: Final = "_prefix"
_ADAPTER_NAME: Final = OneBotV11Adapter.get_name()
"""OneBot V11 适配器名称（与 Bot 冻结的 ``adapter_name`` 同一字面量）。"""


class _DispatchHolder:
    """一次框架分发内的本地持有对象（可变，随浅拷贝状态共享）。"""

    __slots__ = ("dispatch", "dispatch_id", "identity")

    def __init__(self, dispatch_id: str) -> None:
        self.dispatch_id = dispatch_id
        self.identity: EventIdentity | None = None
        self.dispatch: DecisionDispatch | None = None


class _TargetFacts:
    """入站消息的最小可信身份事实（校验后冻结，不承载正文）。"""

    __slots__ = ("bot", "event", "identity", "sender_id")

    def __init__(
        self,
        *,
        bot: OneBotV11Bot,
        event: GroupMessageEvent,
        identity: EventIdentity,
        sender_id: str,
    ) -> None:
        self.bot = bot
        self.event = event
        self.identity = identity
        self.sender_id = sender_id


@event_preprocessor
async def prepare_decision_dispatch(
    bot: Bot,
    event: Event,
    state: T_State,
) -> None:
    """为本次框架分发写入全新的本地持有对象与随机分发标识。"""
    del bot, event
    state[DISPATCH_STATE_KEY] = _DispatchHolder(str(uuid4()))


def get_decision_dispatch(
    bot: Bot,
    event: Event,
    state: T_State | None,
) -> DecisionDispatch:
    """返回当前框架分发的共享判定载体（同一分发内对象身份一致）。

    缺少前置处理器持有对象或必需 OneBot 事实时故障关闭：不制造缺失状态、
    不猜测身份，也不回退到剥离后的纯文本。
    """
    if state is None:
        msg = "当前框架分发缺少判定持有对象"
        raise DecisionDispatchUnavailableError(msg)
    holder = state.get(DISPATCH_STATE_KEY)
    if not isinstance(holder, _DispatchHolder):
        msg = "当前框架分发缺少判定持有对象"
        raise DecisionDispatchUnavailableError(msg)
    facts = _target_facts(bot, event)
    cached = holder.dispatch
    if cached is not None:
        if holder.identity != facts.identity:
            msg = "判定持有对象与当前事件身份不一致"
            raise DecisionDispatchUnavailableError(msg)
        return cached
    request = _build_request(facts, holder.dispatch_id, state)
    holder.identity = facts.identity
    holder.dispatch = DecisionDispatch(request)
    return holder.dispatch


def _target_facts(bot: Bot, event: Event) -> _TargetFacts:
    """校验 OneBot 事件/ Bot 身份一致性并返回最小可信事实。"""
    if not isinstance(bot, OneBotV11Bot) or not isinstance(event, GroupMessageEvent):
        msg = "判定入站只接受 OneBot V11 群消息"
        raise DecisionDispatchUnavailableError(msg)
    bot_id = canonical_platform_id(bot.self_id)
    event_self_id = canonical_platform_id(event.self_id)
    if bot_id is None or event_self_id is None or bot_id != event_self_id:
        msg = "Bot 与事件的自账号身份不一致"
        raise DecisionDispatchUnavailableError(msg)
    group_id = canonical_platform_id(event.group_id)
    message_id = canonical_message_id(event.message_id)
    sender_id = canonical_platform_id(event.user_id)
    if group_id is None or message_id is None or sender_id is None:
        msg = "事件缺少合法的群、消息或发送者标识"
        raise DecisionDispatchUnavailableError(msg)
    identity = EventIdentity(
        protocol=PROTOCOL,
        account_id=str(bot_id),
        group_id=str(group_id),
        message_id=str(message_id),
    )
    return _TargetFacts(
        bot=bot,
        event=event,
        identity=identity,
        sender_id=str(sender_id),
    )


def _build_request(
    facts: _TargetFacts,
    dispatch_id: str,
    state: T_State,
) -> DecisionRequest:
    """按原始 OneBot 事实冻结本次分发的判定请求。"""
    event = facts.event
    identity = facts.identity
    segments = project_native_segments(event.original_message, strict=False)
    if segments is None:
        msg = "入站原文无法投影为原生消息段"
        raise DecisionDispatchUnavailableError(msg)
    platform_time = platform_message_time(event.time)
    if platform_time is None:
        msg = "入站事件缺少合法平台时间"
        raise DecisionDispatchUnavailableError(msg)
    receipt_at = datetime.now(UTC)
    target = DecisionMessage(
        message_id=identity.message_id,
        group_id=identity.group_id,
        sender_id=facts.sender_id,
        text=segments.text,
        timestamp=platform_time,
        display_name=sender_display_name(event.sender),
        is_bot=facts.sender_id == identity.account_id,
        reference_ids=segments.reference_ids,
        mention_ids=segments.mention_ids,
        image_present=segments.image_present,
        image_description=None,
    )
    known_references: tuple[DecisionMessage, ...] = ()
    reply_to_bot = False
    native_reply_id = segments.reference_ids[0] if segments.reference_ids else None
    if native_reply_id is not None and event.reply is not None:
        trusted = project_trusted_reply(
            event.reply,
            native_reply_id=native_reply_id,
            group_id=identity.group_id,
            bot_self_id=identity.account_id,
            receipt_at=receipt_at,
        )
        if trusted is not None:
            known_references = (trusted,)
            reply_to_bot = trusted.sender_id == identity.account_id
    return DecisionRequest(
        event=identity,
        target=target,
        received_at=receipt_at,
        dispatch_id=dispatch_id,
        known_references=known_references,
        real_platform_at=identity.account_id in segments.mention_ids,
        reply_to_bot=reply_to_bot,
        summary_candidate=False,
        functional_command=_functional_command(state),
    )


def _functional_command(state: T_State | None) -> bool:
    """从 NoneBot 实际解析的命令状态判定功能命令（不用标点前缀启发）。"""
    if state is None:
        return False
    prefix = state.get(_FUNCTIONAL_COMMAND_PREFIX_KEY)
    if not isinstance(prefix, Mapping):
        return False
    command = prefix.get("command")
    if command is None:
        return False
    try:
        return bool(command)
    except Exception:  # 未知命令值只按「不是命令」处理
        return True


async def observe_delivered_reply(
    *,
    group_id: object,
    bot_self_id: object,
    adapter_name: object,
    platform_message_id: object,
    delivered_at: object,
    reply_content: object,
    bot_nickname: object,
) -> None:
    """把一条已确认送达的角色回复吸收进判定自有近期上下文（尽力而为）。

    只在既有送达承诺**已经成功**之后调用：普通失败只记录固定安全诊断与异常
    类型，绝不改变已完成的承诺，也不安排新的重试；``CancelledError`` 原样传播。
    合成消息标识、触发消息标识与旧平台内容都不会被替代使用。
    """
    try:
        await _observe_confirmed_reply(
            group_id=group_id,
            bot_self_id=bot_self_id,
            adapter_name=adapter_name,
            platform_message_id=platform_message_id,
            delivered_at=delivered_at,
            reply_content=reply_content,
            bot_nickname=bot_nickname,
        )
    except Exception as exc:
        logger.debug(
            "[KomariDecision] 已确认回复的判定观测失败: error={}",
            type(exc).__name__,
        )


async def _observe_confirmed_reply(
    *,
    group_id: object,
    bot_self_id: object,
    adapter_name: object,
    platform_message_id: object,
    delivered_at: object,
    reply_content: object,
    bot_nickname: object,
) -> None:
    """校验已确认送达事实并调用真实引擎的既有观测方法。"""
    if adapter_name != _ADAPTER_NAME:
        return
    canonical_group = canonical_platform_id(group_id)
    account_id = canonical_platform_id(bot_self_id)
    message_id = canonical_message_id(platform_message_id)
    if canonical_group is None or account_id is None or message_id is None:
        return
    if not isinstance(delivered_at, datetime) or delivered_at.utcoffset() is None:
        return
    if not isinstance(reply_content, str) or not reply_content.strip():
        return
    nickname = bot_nickname if isinstance(bot_nickname, str) else ""
    engine = _current_engine()
    if engine is None:
        return
    # 判定上下文写入前按当前策略复核该群业务资格（ADR-0012 逐效果裁决）。
    if not group_business_admitted(str(canonical_group)):
        return
    message = DecisionMessage(
        message_id=str(message_id),
        group_id=str(canonical_group),
        sender_id=str(account_id),
        text=reply_content,
        timestamp=delivered_at,
        display_name=nickname,
        is_bot=True,
    )
    await engine.observe_delivered(
        message,
        protocol=PROTOCOL,
        account_id=str(account_id),
        received_at=delivered_at,
    )


def _current_engine() -> DecisionEngineProtocol | None:
    """复用判定插件顶层唯一引擎 getter（不在适配器新增 getter）。"""
    from .. import get_decision_engine

    return get_decision_engine()


__all__ = [
    "DISPATCH_STATE_KEY",
    "get_decision_dispatch",
    "observe_delivered_reply",
    "prepare_decision_dispatch",
]
