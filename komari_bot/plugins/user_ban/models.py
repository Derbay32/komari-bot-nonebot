"""用户封禁数据模型与输入规范。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

BanScope = Literal["chat", "command"]
BanTargetScope = Literal["chat", "command", "all"]
BanMutationKind = Literal["created", "updated", "unchanged", "removed"]

BAN_SCOPES: tuple[BanScope, ...] = ("chat", "command")
MAX_BAN_REASON_LENGTH = 500
MAX_BAN_DURATION = timedelta(days=3650)
_DURATION_PATTERN = re.compile(r"^(?P<value>[1-9]\d*)(?P<unit>[mhdw])$", re.IGNORECASE)
_DURATION_UNITS: dict[str, timedelta] = {
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


def expand_target_scope(scope: BanTargetScope) -> tuple[BanScope, ...]:
    """将命令作用域展开为实际存储作用域。"""
    if scope == "all":
        return BAN_SCOPES
    return (scope,)


def normalize_qq_user_id(value: object) -> str | None:
    """规范化 QQ 号；无效值返回 ``None``。"""
    normalized = str(value).strip()
    if not normalized or not normalized.isdigit() or normalized.startswith("0"):
        return None
    return normalized


def normalize_ban_reason(value: str | None) -> str | None:
    """规范化封禁理由，并校验最大长度。"""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_BAN_REASON_LENGTH:
        msg = f"封禁理由不能超过 {MAX_BAN_REASON_LENGTH} 个字符"
        raise ValueError(msg)
    return normalized


def parse_ban_duration(
    value: str | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """将命令时长解析为到期时间；空值或 ``permanent`` 表示永久。"""
    normalized = (value or "permanent").strip().lower()
    if normalized == "permanent":
        return None

    matched = _DURATION_PATTERN.fullmatch(normalized)
    if matched is None:
        msg = "封禁时长必须为 permanent 或正整数加 m、h、d、w 单位"
        raise ValueError(msg)

    unit_duration = _DURATION_UNITS[matched.group("unit").lower()]
    amount = int(matched.group("value"))
    try:
        duration = unit_duration * amount
    except OverflowError as error:
        msg = "封禁时长不能超过十年"
        raise ValueError(msg) from error
    if duration > MAX_BAN_DURATION:
        msg = "封禁时长不能超过十年"
        raise ValueError(msg)

    base_time = now or datetime.now(UTC)
    if base_time.tzinfo is None or base_time.utcoffset() is None:
        msg = "计算封禁期限时必须使用带时区的时间"
        raise ValueError(msg)
    return base_time + duration


@dataclass(frozen=True, slots=True)
class BanRecord:
    """单个用户、单个作用域的封禁记录。"""

    user_id: str
    ban_scope: BanScope
    operator_id: str
    created_at: datetime
    updated_at: datetime
    reason: str | None = None
    expires_at: datetime | None = None

    @property
    def is_permanent(self) -> bool:
        """返回是否为永久封禁。"""
        return self.expires_at is None

    def is_active_at(self, now: datetime | None = None) -> bool:
        """判断记录在指定时间是否仍然有效。"""
        if self.expires_at is None:
            return True
        current_time = now or datetime.now(UTC)
        return self.expires_at > current_time

    @property
    def is_active(self) -> bool:
        """返回记录当前是否仍然有效。"""
        return self.is_active_at()


@dataclass(frozen=True, slots=True)
class UserBanStatus:
    """用户当前全部封禁记录。"""

    user_id: str
    records: tuple[BanRecord, ...] = ()

    @property
    def active_records(self) -> tuple[BanRecord, ...]:
        """返回当前仍生效的记录。"""
        now = datetime.now(UTC)
        return tuple(record for record in self.records if record.is_active_at(now))

    @property
    def active_scopes(self) -> frozenset[BanScope]:
        """返回当前生效的封禁作用域。"""
        return frozenset(record.ban_scope for record in self.active_records)


@dataclass(frozen=True, slots=True)
class BanMutationResult:
    """封禁或解封操作结果。"""

    status: UserBanStatus
    target_scope: BanTargetScope
    changed: bool
    mutation_kind: BanMutationKind = "unchanged"
    affected_records: tuple[BanRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """一次私信通知的尝试结果。"""

    attempted: bool
    sent: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BanListPage:
    """用户封禁分页结果。"""

    items: tuple[UserBanStatus, ...]
    total: int
    limit: int
    offset: int
