"""OneBot 群任务失败通知的共享边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nonebot import get_driver, logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

_SUMMARY_MAX_CHARS = 120
_BEARER_CREDENTIAL_PATTERN = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)
_SECRET_TOKEN_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:(?:proxy[_-]?)?authorization|x[_-]?api[_-]?key|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|token|client[_-]?secret|"
    r"secret|password|passwd|pwd|set-cookie|cookie|session|private[_-]?key|dsn)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_DATA_IMAGE_PATTERN = re.compile(r"data:image/[^\s]+", re.IGNORECASE)
_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*]\([^)]*\)")
_CQ_IMAGE_PATTERN = re.compile(r"\[CQ:image,[^]]*]", re.IGNORECASE)
_IMAGE_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:image|images|image_url)\s*[:=].*$|(?:图片|图片摘要)\s*[:=：].*$",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^\s]+", re.IGNORECASE)
_BUSINESS_FIELD_PATTERN = re.compile(
    r"\b(?:message_body|prompt|reasoning|tool_arguments|business_payload)\s*[:=]"
    r"|消息正文\s*[:=：]",
    re.IGNORECASE,
)
#: 图片卡 trace 行的安全标识字符白名单（ASCII 字母数字 + ``._:-``，1..128）。
_TRACE_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._:\-]{1,128}$")


def _project_safe_trace(trace: str | None) -> str | None:
    """把请求 trace 投影为图片卡的窄安全值；非法/空值直接省略。

    TSK-196 复审：图片失败汇总卡的 trace 行只允许项目 trace 的安全标识字符
    （ASCII 字母数字与 ``._:-``，长度 1..128）；URL/base64/CQ/换行等恶意或
    非法值直接省略，绝不把原值或替换值写卡。generic 卡保持现有格式。
    """
    if not trace:
        return None
    if _TRACE_SAFE_PATTERN.fullmatch(trace):
        return trace
    return None


class _FailureNotificationBot(Protocol):
    async def call_api(self, api: str, **data: object) -> object: ...

    async def send_private_msg(self, *, user_id: int, message: str) -> object: ...


class _RedisSetClient(Protocol):
    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool,
        ex: int,
    ) -> object: ...


@runtime_checkable
class _FailureNotificationCooldown(Protocol):
    async def acquire(
        self,
        *,
        task_kind: str,
        group_id: int,
        reason_code: str,
    ) -> bool: ...


_VALID_IMAGE_DIAGNOSTIC_MODES = frozenset({"native", "delegated"})
_VALID_IMAGE_DIAGNOSTIC_STAGES = frozenset({"invalid", "download", "vision"})
_VALID_IMAGE_DIAGNOSTIC_ERROR_TYPES = frozenset(
    {"invalid_index", "image_unavailable", "vision_failed"}
)


@dataclass(frozen=True, slots=True)
class ImageFailureDiagnostic:
    """图片理解失败的安全聚合摘要（TSK-196；白名单字段，无 URL/base64/正文）。

    只包含模式、失败数量、去重排序后的失败阶段与归一化错误类型，供
    SUPERUSER 诊断卡渲染；绝不携带图片 URL、base64、视觉描述或消息正文。
    纵深防御（TSK-196 复审）：构造时运行时校验并确定性去重排序——mode 仅
    native/delegated、stage 仅 invalid/download/vision、error type 仅
    invalid_index/image_unavailable/vision_failed、failed_count 必须是
    正整数（bool 禁止）、stage/error 至少一项；恶意 URL/base64/CQ/换行值
    一律 ``ValueError``。renderer 只消费已验证对象。
    """

    mode: str
    failed_count: int
    stages: tuple[str, ...]
    error_types: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mode not in _VALID_IMAGE_DIAGNOSTIC_MODES:
            msg = f"非法图片失败模式: {self.mode!r}"
            raise ValueError(msg)
        if isinstance(self.failed_count, bool) or not isinstance(
            self.failed_count, int
        ):
            msg = "failed_count 必须是正整数"
            raise TypeError(msg)
        if self.failed_count <= 0:
            msg = "failed_count 必须是正整数（大于 0）"
            raise ValueError(msg)
        stages = tuple(sorted(set(self.stages)))
        if not stages:
            msg = "stages 不能为空"
            raise ValueError(msg)
        if any(stage not in _VALID_IMAGE_DIAGNOSTIC_STAGES for stage in stages):
            msg = f"非法失败阶段: {stages!r}"
            raise ValueError(msg)
        error_types = tuple(sorted(set(self.error_types)))
        if not error_types:
            msg = "error_types 不能为空"
            raise ValueError(msg)
        if any(
            error_type not in _VALID_IMAGE_DIAGNOSTIC_ERROR_TYPES
            for error_type in error_types
        ):
            msg = f"非法失败类型: {error_types!r}"
            raise ValueError(msg)
        object.__setattr__(self, "stages", stages)
        object.__setattr__(self, "error_types", error_types)


def image_failure_reason_code(diagnostic: ImageFailureDiagnostic) -> str:
    """生成稳定、确定性的图片失败 reason_code（跨任务可去重）。

    由模式与排序去重后的错误类型组成（``ImageFailureDiagnostic`` 构造时已
    归一化），与读取顺序无关；同一群+同一 reason_code 在不同任务间共享冷却
    去重。
    """
    return "_".join(("image", diagnostic.mode, *diagnostic.error_types))


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupTaskFailureNotification:
    """调用方可提交的群任务失败通知窄契约。"""

    group_id: int
    message_id: int
    group_text: str | None
    task_kind: str
    stage: str
    reason_code: str
    notify_superusers: bool
    request_trace_id: str | None = None
    summary: str | None = None
    image_diagnostic: ImageFailureDiagnostic | None = None


class InMemoryFailureNotificationCooldown:
    """进程内失败通知冷却。"""

    def __init__(self, *, cooldown_seconds: int = 300) -> None:
        self._cooldown_seconds = max(1, cooldown_seconds)
        self._expires_at: dict[tuple[str, int, str], float] = {}
        self._lock = asyncio.Lock()

    async def acquire(
        self,
        *,
        task_kind: str,
        group_id: int,
        reason_code: str,
    ) -> bool:
        key = (task_kind, group_id, reason_code)
        now = time.monotonic()
        async with self._lock:
            if self._expires_at.get(key, 0.0) > now:
                return False
            self._expires_at[key] = now + self._cooldown_seconds
            return True


class RedisFailureNotificationCooldown:
    """跨进程 Redis 失败通知冷却；存储不可用时故障开放。"""

    def __init__(
        self,
        redis: _RedisSetClient | None,
        *,
        cooldown_seconds: int = 300,
    ) -> None:
        self._redis = redis
        self._cooldown_seconds = max(1, cooldown_seconds)

    async def acquire(
        self,
        *,
        task_kind: str,
        group_id: int,
        reason_code: str,
    ) -> bool:
        if self._redis is None:
            return True
        dimensions = json.dumps(
            [task_kind, group_id, reason_code],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(dimensions.encode()).hexdigest()
        key = f"komari:onebot:group_task_failure:{digest}"
        try:
            acquired = await self._redis.set(
                key,
                "1",
                nx=True,
                ex=self._cooldown_seconds,
            )
            return bool(acquired)
        except Exception:
            logger.debug("[OneBot] Redis 通知冷却不可用，降级为继续投递", exc_info=True)
            return True


def _runtime_superusers() -> Iterable[object]:
    """读取当前 NoneBot 运行时的 SUPERUSER 配置。"""
    return get_driver().config.superusers


def _resolve_superuser_ids(
    provider: Callable[[], Iterable[object]],
) -> list[int]:
    """解析合法 QQ 号；配置读取或枚举失败时返回空白名单。"""
    try:
        raw_entries = list(provider())
    except Exception:
        logger.debug("[OneBot] 无法读取运行时 SUPERUSER 配置", exc_info=True)
        return []

    user_ids: set[int] = set()
    for raw in raw_entries:
        try:
            user_id = int(str(raw).strip())
        except Exception:
            logger.debug("[OneBot] 跳过非法 SUPERUSER 配置条目")
            continue
        if user_id <= 0:
            logger.debug("[OneBot] 跳过非正数 SUPERUSER 配置条目")
            continue
        user_ids.add(user_id)
    return sorted(user_ids)


def _project_summary(summary: str | None) -> str | None:
    """把异常摘要投影为不含凭据、链接、图片和业务载荷的单行文本。"""
    if not summary:
        return None
    first_line = summary.splitlines()[0].strip()
    if not first_line:
        return None

    business_field = _BUSINESS_FIELD_PATTERN.search(first_line)
    if business_field is not None:
        first_line = first_line[: business_field.start()].rstrip()
    first_line = _MARKDOWN_IMAGE_PATTERN.sub("[已移除图片]", first_line)
    first_line = _CQ_IMAGE_PATTERN.sub("[已移除图片]", first_line)
    first_line = _DATA_IMAGE_PATTERN.sub("[已移除图片]", first_line)
    first_line = _IMAGE_ASSIGNMENT_PATTERN.sub("[已移除图片]", first_line)
    first_line = _BEARER_CREDENTIAL_PATTERN.sub("[已移除凭据]", first_line)
    first_line = _SECRET_TOKEN_PATTERN.sub("[已移除凭据]", first_line)
    first_line = _CREDENTIAL_ASSIGNMENT_PATTERN.sub("[已移除凭据]", first_line)
    first_line = _URL_PATTERN.sub("[已移除链接]", first_line)
    projected = " ".join(first_line.split())[:_SUMMARY_MAX_CHARS]
    return projected or None


class GroupTaskFailureNotifier:
    """投递群内固定提示与 SUPERUSER 白名单诊断卡。"""

    def __init__(
        self,
        *,
        superusers_provider: Callable[[], Iterable[object]] = _runtime_superusers,
        cooldown: object | None = None,
    ) -> None:
        self._superusers_provider = superusers_provider
        if cooldown is None:
            self._cooldown: _FailureNotificationCooldown = (
                InMemoryFailureNotificationCooldown()
            )
        elif isinstance(cooldown, _FailureNotificationCooldown):
            self._cooldown = cooldown
        else:
            msg = "cooldown 必须实现异步 acquire()"
            raise TypeError(msg)

    async def notify(
        self,
        *,
        bot: _FailureNotificationBot,
        notification: GroupTaskFailureNotification,
    ) -> None:
        if notification.group_text is not None:
            try:
                await bot.call_api(
                    "send_group_msg",
                    group_id=notification.group_id,
                    message=[
                        {
                            "type": "reply",
                            "data": {"id": str(notification.message_id)},
                        },
                        {
                            "type": "text",
                            "data": {"text": notification.group_text},
                        },
                    ],
                )
            except Exception:
                logger.warning("[OneBot] 群任务失败提示投递失败", exc_info=True)

        if not notification.notify_superusers:
            return

        try:
            acquired = await self._cooldown.acquire(
                task_kind=notification.task_kind,
                group_id=notification.group_id,
                reason_code=notification.reason_code,
            )
        except Exception:
            logger.debug("[OneBot] 群任务失败通知冷却异常，降级为继续投递", exc_info=True)
            acquired = True
        if not acquired:
            return

        if notification.image_diagnostic is not None:
            # TSK-196 复审：图片失败汇总卡只渲染严格白名单字段——群/trace/
            # 图片模式/失败阶段/失败数量/失败类型；不含任务、generic 阶段/
            # 原因/摘要与 message_id。diagnostic 构造时已确定性去重排序，
            # renderer 只消费已验证对象；trace 经窄安全投影，非法/空值省略。
            diagnostic = notification.image_diagnostic
            lines = [f"群: {notification.group_id}"]
            safe_trace = _project_safe_trace(notification.request_trace_id)
            if safe_trace:
                lines.append(f"trace: {safe_trace}")
            lines.append(f"图片模式: {diagnostic.mode}")
            lines.append(f"失败阶段: {', '.join(diagnostic.stages)}")
            lines.append(f"失败数量: {diagnostic.failed_count}")
            lines.append(f"失败类型: {', '.join(diagnostic.error_types)}")
        else:
            # generic 卡片保持原格式（任务/群/阶段/原因/trace/摘要）
            lines = [
                f"任务: {notification.task_kind}",
                f"群: {notification.group_id}",
                f"阶段: {notification.stage}",
                f"原因: {notification.reason_code}",
            ]
            if notification.request_trace_id:
                lines.append(f"trace: {notification.request_trace_id}")
            summary = _project_summary(notification.summary)
            if summary:
                lines.append(f"摘要: {summary}")
        text = "\n".join(lines)
        for user_id in _resolve_superuser_ids(self._superusers_provider):
            try:
                await bot.send_private_msg(user_id=user_id, message=text)
            except Exception:
                logger.warning(
                    "[OneBot] 群任务失败诊断私聊投递失败: user={}",
                    user_id,
                    exc_info=True,
                )
