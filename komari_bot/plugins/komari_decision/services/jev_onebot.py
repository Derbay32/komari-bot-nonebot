"""实际 OneBot V11 引用补取适配器（TSK-182 生产引用读取边界）。

本模块只做一件事：把 ``ReferenceResolver`` 请求的一个引用消息标识，经**当前
已连接**的 OneBot V11 Bot 取回官方 ``get_msg`` 响应，并投影为冻结的
``DecisionMessage``。它不实现遍历、不实现截止时间、不调用模型、不下载媒体。

冻结的协议事实（官方 OneBot 11 文档
``_autodocs/api-reference/api-public.md``）：``get_msg`` 响应声明
``time`` / ``message_type`` / ``message_id`` / ``real_id`` / ``sender`` /
``message``，**不要求** ``group_id``。因此：

- 群来源只沿**可信群事件**出发的**平台原生**引用边传递（``ReferenceResolver``
  只读取这些边），响应缺少 ``group_id`` 时沿用该可信来源；
- 提供方**确实**给出 ``group_id`` 时要求精确相等；显式跨群矛盾视为缺失；
- 私聊/未知 ``message_type``、消息标识不符、缺失或非法发送者/时间、结构畸形
  一律视为不可读，绝不猜测、绝不回退到其它群；
- 文本里的 CQ 样式字样不产生引用边，只有原生 ``reply`` 消息段产生引用标识。

本模块不弱化也不导入 ``character_binding`` 更强的身份证据契约（那里要求
payload 群号 + 缓存签名 + 原文一致），只是消费通用引用 API。

每次远程读取前后都按**顶层** ``group_admission.adjudicate`` 复核该群业务资格；
任一环节不可用只返回 ``None``（判定侧记为缺失），``CancelledError`` 原样传播。
任何异常都不记录原始 payload / 异常正文，只记录异常类型。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.event import Reply
from nonebot.compat import type_validate_python

from komari_bot.decision.jev import DecisionMessage, EventIdentity

if TYPE_CHECKING:
    from collections.abc import Callable

PROTOCOL: Final = "onebot_v11"
"""OneBot V11 的规范协议标识（与判定事件身份的 ``protocol`` 一致）。"""

_INT32_MIN: Final = -(2**31)
_INT32_MAX: Final = 2**31 - 1
_MAX_PLATFORM_ID: Final = 2**63 - 1
_MAX_UNIX_TIME: Final = 2**53

_PLATFORM_ID_MAX_DIGITS: Final = len(str(_MAX_PLATFORM_ID))


def canonical_platform_id(value: object) -> int | None:
    """把群号/账号/发送者标识解析为真实正整数；非法一律 ``None``。

    只接受真实数值标识：``int`` 或纯 ASCII 十进制字符串。布尔值、浮点数、
    空串、符号/空白前缀、超出平台标识范围或完全缺失都返回 ``None``——绝不
    发明 ``unknown`` 之类的占位标识。
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > _PLATFORM_ID_MAX_DIGITS:
            return None
        if not (text.isascii() and text.isdigit()):
            return None
        number = int(text)
    else:
        return None
    if not 1 <= number <= _MAX_PLATFORM_ID:
        return None
    return number


def canonical_message_id(value: object) -> int | None:
    """把消息标识解析为协议允许的**有符号 int32**；非法一律 ``None``。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        digits = text.removeprefix("-")
        if not digits or not (digits.isascii() and digits.isdigit()):
            return None
        if len(digits) > 10:
            return None
        number = int(text)
    else:
        return None
    if not _INT32_MIN <= number <= _INT32_MAX:
        return None
    return number


def group_business_admitted(group_id: str) -> bool:
    """按**顶层** ``group_admission.adjudicate`` 复核该群业务资格。

    同步、无 I/O；任何异常都故障关闭（返回 ``False``），只记录异常类型。
    """
    number = canonical_platform_id(group_id)
    if number is None:
        return False
    try:
        from komari_bot.plugins.group_admission import (
            AdmissionIntent,
            AdmissionQualification,
            adjudicate,
        )

        result = adjudicate({number}, intent=AdmissionIntent.BUSINESS)
    except Exception as exc:
        logger.debug(
            "[KomariDecision] Jev 引用补取准入裁决不可用: error={}", type(exc).__name__
        )
        return False
    return result.qualification is AdmissionQualification.BUSINESS


def _default_bot_resolver(account_id: str) -> object | None:
    """默认连接 Bot 解析：只返回 NoneBot 当前已连接的 Bot 对象。"""
    from nonebot import get_bot

    return get_bot(account_id)


def platform_message_time(value: object) -> datetime | None:
    """把协议 ``time``（Unix 秒）转成带时区 UTC 时间；非法返回 ``None``。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 < value <= _MAX_UNIX_TIME:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def sender_display_name(sender: object) -> str:
    """发送者展示名：优先群名片，其次昵称（均为不可信数据，不做改写）。"""
    if isinstance(sender, Mapping):
        card = sender.get("card")
        nickname = sender.get("nickname")
    else:
        card = getattr(sender, "card", None)
        nickname = getattr(sender, "nickname", None)
    for candidate in (card, nickname):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return ""


@dataclass(frozen=True, slots=True)
class NativeSegments:
    """一条原生消息段的投影事实（不含任何补造或解析猜测）。"""

    text: str
    reference_ids: tuple[str, ...] = ()
    mention_ids: tuple[str, ...] = ()
    image_present: bool = False


def project_native_segments(
    message: Message,
    *,
    strict: bool = True,
) -> NativeSegments | None:
    """用官方 SDK 解析出的消息段投影引用/提及/媒体/纯文本事实。

    只认原生段类型：``reply`` 提供引用标识、``at`` 提供提及标识、``image``
    提供「发送了图片」这一事实。文本段里即便写着 CQ 样式字样也不产生引用边。

    ``strict`` 为真时（引用补取既有语义）任何非法原生标识都判整条消息不可读；
    入站目标投影用非严格模式跳过非法标识——非法标识只能不构成引用/ summon
    事实，不能让一条正常正文整条不可读。
    """
    reference_ids: list[str] = []
    mention_ids: list[str] = []
    text_parts: list[str] = []
    image_present = False
    for segment in message:
        kind = segment.type
        if kind == "reply":
            number = canonical_message_id(segment.data.get("id"))
            if number is None:
                if strict:
                    return None
                continue
            _append_unique(reference_ids, str(number))
        elif kind == "at":
            number = canonical_platform_id(segment.data.get("qq"))
            if number is not None:
                _append_unique(mention_ids, str(number))
        elif kind == "image":
            image_present = True
        elif kind == "text":
            value = segment.data.get("text")
            if isinstance(value, str):
                text_parts.append(value)
    return NativeSegments(
        text="".join(text_parts),
        reference_ids=tuple(reference_ids),
        mention_ids=tuple(mention_ids),
        image_present=image_present,
    )


def project_trusted_reply(  # noqa: PLR0911 - 每个显式矛盾都必须独立拒绝
    reply: Reply,
    *,
    native_reply_id: str,
    group_id: str,
    bot_self_id: str,
    receipt_at: datetime,
) -> DecisionMessage | None:
    """把**已可获得**的引用消息投影为可信已知引用；任一矛盾返回 ``None``。

    可信条件：原生 ``reply`` 段标识与引用消息标识一致、群来源一致（显式矛盾
    即拒绝）、引用不晚于本地接收截止点、发送者与时间可解析。私聊引用与凭空
    猜测都不能成为可信引用或真实平台召唤。
    """
    canonical_group = canonical_platform_id(group_id)
    if canonical_group is None:
        return None
    requested_id = canonical_message_id(reply.message_id)
    if requested_id is None or str(requested_id) != native_reply_id:
        return None
    if reply.message_type != "group":
        return None
    provided_group = getattr(reply, "group_id", None)
    if provided_group is not None:
        canonical_provided = canonical_platform_id(provided_group)
        if canonical_provided is None or canonical_provided != canonical_group:
            return None
    timestamp = platform_message_time(reply.time)
    if timestamp is None or timestamp > receipt_at:
        return None
    sender_id = canonical_platform_id(reply.sender.user_id)
    if sender_id is None:
        return None
    message = reply.message
    if not isinstance(message, Message):
        return None
    segments = project_native_segments(message, strict=False)
    if segments is None:
        return None
    return DecisionMessage(
        message_id=str(requested_id),
        group_id=str(canonical_group),
        sender_id=str(sender_id),
        text=segments.text,
        timestamp=timestamp,
        display_name=sender_display_name(reply.sender),
        is_bot=str(sender_id) == bot_self_id,
        reference_ids=segments.reference_ids,
        mention_ids=segments.mention_ids,
        image_present=segments.image_present,
        image_description=None,
    )


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)


def _project(  # noqa: PLR0911 - 每个显式矛盾都必须独立拒绝
    payload: Mapping[str, Any],
    *,
    identity: EventIdentity,
    requested_id: int,
    bot: OneBotV11Bot,
) -> DecisionMessage | None:
    """把一条 ``get_msg`` 响应投影为冻结的 ``DecisionMessage``。

    身份、时间与消息标识先按**原始 JSON 值**校验，再交给 SDK 做消息段解析：
    SDK 的宽松整形不得把 ``bool`` / ``float`` 变成合法整数。
    """
    # 原始字段先于 SDK 宽松整形校验：bool/float 不是消息标识或时间。
    if canonical_message_id(payload.get("message_id")) != requested_id:
        return None
    if payload.get("message_type") != "group":
        return None
    timestamp = platform_message_time(payload.get("time"))
    if timestamp is None:
        return None
    raw_sender = payload.get("sender")
    if not isinstance(raw_sender, Mapping):
        return None
    sender_id = canonical_platform_id(raw_sender.get("user_id"))
    if sender_id is None:
        return None
    group_id = canonical_platform_id(identity.group_id)
    if group_id is None:
        return None
    if "group_id" in payload:
        # 提供方**确实**给出 group_id 键时必须是合法同群；显式 null 也是矛盾。
        canonical_group = canonical_platform_id(payload["group_id"])
        if canonical_group is None or canonical_group != group_id:
            return None
    reply = type_validate_python(Reply, payload)
    message = reply.message
    if not isinstance(message, Message):
        return None
    segments = project_native_segments(message)
    if segments is None:
        return None
    return DecisionMessage(
        message_id=str(requested_id),
        group_id=str(group_id),
        sender_id=str(sender_id),
        text=segments.text,
        timestamp=timestamp,
        display_name=sender_display_name(raw_sender),
        is_bot=str(sender_id) == str(bot.self_id),
        reference_ids=segments.reference_ids,
        mention_ids=segments.mention_ids,
        image_present=segments.image_present,
        image_description=None,
    )


class OneBotReferenceReader:
    """``ReferenceReader`` 的生产实现（一次调用读取一个引用节点）。

    构造参数只用于替换**外部 I/O 边界**（已连接 Bot 解析与群准入裁决）；业务
    投影、协议校验与错误收敛全部固定在本类内，生产无需注入其它实现。
    """

    def __init__(
        self,
        *,
        bot_resolver: Callable[[str], object | None] | None = None,
        group_admitted: Callable[[str], bool] | None = None,
    ) -> None:
        self._bot_resolver = bot_resolver or _default_bot_resolver
        self._group_admitted = group_admitted or group_business_admitted

    async def __call__(
        self, identity: EventIdentity, message_id: str
    ) -> DecisionMessage | None:
        """读取一个引用节点；不可读返回 ``None``，取消原样传播。"""
        try:
            return await self._read(identity, message_id)
        except Exception as exc:
            # 绝不记录原始 payload / 异常正文 / 消息标识。
            logger.debug(
                "[KomariDecision] Jev 引用补取节点不可读: error={}",
                type(exc).__name__,
            )
            return None

    async def _read(  # noqa: PLR0911 - 每个显式矛盾都必须独立拒绝
        self, identity: EventIdentity, message_id: str
    ) -> DecisionMessage | None:
        if identity.protocol != PROTOCOL:
            return None
        group_id = canonical_platform_id(identity.group_id)
        account_id = canonical_platform_id(identity.account_id)
        requested_id = canonical_message_id(message_id)
        if group_id is None or account_id is None or requested_id is None:
            return None
        if message_id != str(requested_id):
            return None
        if not self._group_admitted(str(group_id)):
            return None
        bot = self._resolve_bot(str(account_id))
        if bot is None:
            return None
        payload = await bot.get_msg(message_id=requested_id)
        if not isinstance(payload, Mapping):
            return None
        message = _project(
            payload,
            identity=identity,
            requested_id=requested_id,
            bot=bot,
        )
        if message is None:
            # 只记录固定原因，不回显任何原始 payload 字段。
            logger.debug("[KomariDecision] Jev 引用补取响应不符合冻结协议")
            return None
        # 远程读取完成后再次复核准入：读取期间撤权的节点绝不交付正文。
        if not self._group_admitted(str(group_id)):
            return None
        return message

    def _resolve_bot(self, account_id: str) -> OneBotV11Bot | None:
        """解析该账号**已连接**的 OneBot V11 Bot；不匹配一律 ``None``。"""
        try:
            bot = self._bot_resolver(account_id)
        except Exception:
            return None
        if not isinstance(bot, OneBotV11Bot):
            return None
        if str(bot.self_id) != account_id:
            return None
        return bot


__all__ = [
    "PROTOCOL",
    "NativeSegments",
    "OneBotReferenceReader",
    "canonical_message_id",
    "canonical_platform_id",
    "group_business_admitted",
    "platform_message_time",
    "project_native_segments",
    "project_trusted_reply",
    "sender_display_name",
]
