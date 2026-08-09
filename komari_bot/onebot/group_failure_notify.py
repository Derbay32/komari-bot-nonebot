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
