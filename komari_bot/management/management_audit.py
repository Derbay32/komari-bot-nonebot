"""统一管理 API 的脱敏审计事件。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import uuid4

from fastapi import Header, HTTPException
from nonebot import logger
from starlette import status

if TYPE_CHECKING:
    from .management_api import ManagementPrincipal

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_AUDIT_DIR = _PROJECT_ROOT / "logs" / "komari_management"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_MAX_REASON_LENGTH = 200

type AuditOutcome = Literal["started", "succeeded", "failed"]
type AuditMetadataValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class ManagementAuditEvent:
    """不含 Token、请求正文或目标原始 ID 的管理审计事件。"""

    timestamp: str
    request_id: str
    operator_id: str
    action: str
    resource: str
    reason: str
    outcome: AuditOutcome
    duration_ms: float | None = None
    status_code: int | None = None
    field_name: str | None = None
    target_hash: str | None = None
    error_code: str | None = None
    metadata: Mapping[str, AuditMetadataValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接写入 JSONL 的安全字典。"""
        return {
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "operator_id": self.operator_id,
            "action": self.action,
            "resource": self.resource,
            "field_name": self.field_name,
            "target_hash": self.target_hash,
            "reason": self.reason,
            "outcome": self.outcome,
            "duration_ms": self.duration_ms,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "metadata": dict(self.metadata),
        }


type ManagementAuditRecorder = Callable[[ManagementAuditEvent], Awaitable[None]]


@dataclass(slots=True)
class ManagementAuditSpan:
    """允许业务代码在结束前补充安全计数的审计上下文。"""

    metadata: dict[str, AuditMetadataValue] = field(default_factory=dict)
    status_code: int = status.HTTP_200_OK


class JsonlManagementAuditRecorder:
    """使用单次 O_APPEND 写入的多进程友好 JSONL 审计记录器。"""

    def __init__(self, log_dir: Path = _DEFAULT_AUDIT_DIR) -> None:
        self._log_dir = log_dir
        self._write_lock = threading.Lock()

    def _append(self, event: ManagementAuditEvent) -> None:
        self._log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._log_dir.chmod(0o700)
        log_file = self._log_dir / f"audit-{datetime.now(tz=UTC):%Y-%m-%d}.jsonl"
        payload = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        with self._write_lock:
            descriptor = os.open(
                log_file,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_APPEND
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
                written = os.write(descriptor, payload)
                if written != len(payload):
                    msg = "管理审计事件未完整写入"
                    raise OSError(msg)
            finally:
                os.close(descriptor)

    async def __call__(self, event: ManagementAuditEvent) -> None:
        await asyncio.to_thread(self._append, event)


def require_management_change_reason(
    value: Annotated[
        str | None,
        Header(alias="X-Komari-Change-Reason"),
    ] = None,
) -> str:
    """要求写操作提供简短、可审计且不含控制字符的变更原因。"""
    raw_reason = str(value or "")
    try:
        reason = raw_reason.encode("latin-1").decode("utf-8").strip()
    except (UnicodeDecodeError, UnicodeEncodeError):
        reason = raw_reason.strip()
    if not reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="写操作必须提供 X-Komari-Change-Reason",
        )
    if len(reason) > _MAX_REASON_LENGTH or not reason.isprintable():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"变更原因必须是 {_MAX_REASON_LENGTH} 字以内的可打印文本",
        )
    return reason


def resolve_management_request_id(
    value: Annotated[
        str | None,
        Header(alias="X-Request-ID"),
    ] = None,
) -> str:
    """校验调用方 request ID，未提供时生成新的随机 ID。"""
    request_id = str(value or "").strip()
    if not request_id:
        return uuid4().hex
    if not _REQUEST_ID_PATTERN.fullmatch(request_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="X-Request-ID 格式无效",
        )
    return request_id


def hash_management_target(*parts: object) -> str:
    """为目标 ID 集合生成不可逆、顺序明确的审计关联哈希。"""
    digest = sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:20]


async def _record_final_event(
    recorder: ManagementAuditRecorder,
    event: ManagementAuditEvent,
) -> None:
    try:
        await recorder(event)
    except Exception as exc:
        logger.critical(
            "[Komari Management] 审计结果写入失败: "
            "request_id={} action={} error_type={}",
            event.request_id,
            event.action,
            type(exc).__name__,
        )


@asynccontextmanager
async def management_audit_span(
    *,
    principal: ManagementPrincipal,
    request_id: str,
    reason: str,
    action: str,
    resource: str,
    recorder: ManagementAuditRecorder,
    field_name: str | None = None,
    target_hash: str | None = None,
) -> AsyncIterator[ManagementAuditSpan]:
    """写入 attempt/result 两阶段事件；attempt 失败时不执行变更。"""
    started_at = time.monotonic()
    base_event = ManagementAuditEvent(
        timestamp=datetime.now(tz=UTC).isoformat(),
        request_id=request_id,
        operator_id=principal.operator_id,
        action=action,
        resource=resource,
        field_name=field_name,
        target_hash=target_hash,
        reason=reason,
        outcome="started",
    )
    await recorder(base_event)

    span = ManagementAuditSpan()
    try:
        yield span
    except BaseException as exc:
        status_code = (
            int(exc.status_code)
            if isinstance(exc, HTTPException)
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        await _record_final_event(
            recorder,
            replace(
                base_event,
                timestamp=datetime.now(tz=UTC).isoformat(),
                outcome="failed",
                duration_ms=round((time.monotonic() - started_at) * 1000, 3),
                status_code=status_code,
                error_code=f"http_{status_code}",
                metadata=dict(span.metadata),
            ),
        )
        raise
    else:
        await _record_final_event(
            recorder,
            replace(
                base_event,
                timestamp=datetime.now(tz=UTC).isoformat(),
                outcome="succeeded",
                duration_ms=round((time.monotonic() - started_at) * 1000, 3),
                status_code=span.status_code,
                metadata=dict(span.metadata),
            ),
        )


default_management_audit_recorder = JsonlManagementAuditRecorder()


async def record_management_audit_event(event: ManagementAuditEvent) -> None:
    """使用默认记录器写入一条管理审计事件。"""
    await default_management_audit_recorder(event)


__all__ = [
    "JsonlManagementAuditRecorder",
    "ManagementAuditEvent",
    "ManagementAuditRecorder",
    "ManagementAuditSpan",
    "hash_management_target",
    "management_audit_span",
    "record_management_audit_event",
    "require_management_change_reason",
    "resolve_management_request_id",
]
