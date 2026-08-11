"""回复履约受审计运维服务（management ops 窄边界）。

提供列表、详情、确认送达、确认未送达与指定承诺续跑五个运维能力；
只消费新父子表管理安全投影与原子状态守卫，不读取旧 outbox、不发起
平台发送、不批量执行送达后承诺，也不暴露任何内部载荷、触发用户或
租约信息。管理插件经 komari_chat 顶层窄 seam 获取本服务。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..reply_fulfillment_domain import (
    COMMITMENT_TYPES,
    derive_reply_fulfillment_status,
)
from ..reply_fulfillment_ops_errors import (
    ReplyFulfillmentOpsConflictError,
    ReplyFulfillmentOpsNotFoundError,
    ReplyFulfillmentOpsValidationError,
)
from ..repositories.reply_fulfillment_repository import ReplyFulfillmentRepository
from .proactive_reservation import ProactiveReservationService

if TYPE_CHECKING:
    from collections.abc import Mapping

# 列表项的最小身份字段，顺序与 OpenAPI 契约一致；正文与内部字段
# 一律不在投影内。
_SUMMARY_FIELDS = (
    "fulfillment_id",
    "request_trace_id",
    "trigger_message_id",
    "group_id",
    "status",
    "reply_fingerprint",
    "prepared_at",
    "send_started_at",
    "delivered_at",
    "platform_message_id",
    "not_delivered_at",
    "completed_at",
)


def _to_iso_text(value: Any) -> Any:
    """把存储返回的时间戳统一为 ISO 文本，其他值原样透传。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _project_commitment_summary(child: Mapping[str, Any]) -> dict[str, Any]:
    """承诺最小事实：类型、状态、次数、退避时间与稳定错误码。"""
    return {
        "commitment_type": str(child["commitment_type"]),
        "state": str(child["state"]),
        "attempt_count": int(child.get("attempt_count") or 0),
        "next_retry_at": _to_iso_text(child.get("next_retry_at")),
        "last_error_code": child.get("last_error_code"),
        "completed_at": _to_iso_text(child.get("completed_at")),
    }


def _project_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    """把存储行投影为不含正文与内部载荷的最小列表项。"""
    item: dict[str, Any] = {
        field: _to_iso_text(row.get(field)) for field in _SUMMARY_FIELDS
    }
    item["reply_fingerprint"] = row.get("reply_fingerprint") or row.get("payload_hash")
    item["status"] = derive_reply_fulfillment_status(row)
    item["commitments"] = [
        _project_commitment_summary(child) for child in (row.get("commitments") or ())
    ]
    return item


def _project_detail(row: Mapping[str, Any]) -> dict[str, Any]:
    """详情投影：只有待确认送达才返回核对正文，其他状态一律为空。"""
    item = _project_summary(row)
    item["reply_target_message_id"] = row.get("reply_target_message_id")
    item["reply_content"] = (
        row.get("reply_content") if item["status"] == "pending_confirmation" else None
    )
    return item


def build_reply_fulfillment_ops_service(
    *,
    pg_pool: Any,
    redis_client: Any,
) -> ReplyFulfillmentOpsService:
    """composition root：在 komari_chat 内部装配新父子表 adapter 与预占服务。

    管理插件不构造 Repository，只经插件顶层窄 seam 获取本 builder 的
    产物；运维边界只操作新父子表，不读取旧 outbox、不启动 worker。
    """
    return ReplyFulfillmentOpsService(
        ReplyFulfillmentRepository(pg_pool),
        ProactiveReservationService(redis_client),
    )


class ReplyFulfillmentOpsService:
    """回复履约运维窄边界：只消费管理投影与原子状态守卫。"""

    def __init__(
        self,
        repository: ReplyFulfillmentRepository,
        proactive_reservation: Any,
    ) -> None:
        self._repository = repository
        self._proactive_reservation = proactive_reservation

    async def list_fulfillments(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """分页列出履约最小身份与派生状态。"""
        rows, total = await self._repository.list_for_management(
            status=status,
            limit=limit,
            offset=offset,
        )
        return {
            "items": [_project_summary(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def get_fulfillment(
        self,
        fulfillment_id: str,
    ) -> dict[str, Any] | None:
        """获取单个履约详情；不存在返回 None。"""
        row = await self._repository.get_for_management(fulfillment_id)
        if row is None:
            return None
        return _project_detail(row)

    async def confirm_delivered(
        self,
        fulfillment_id: str,
        *,
        platform_message_id: str | None,
    ) -> dict[str, Any]:
        """确认送达：先原子持久化送达事实，不同步批量执行承诺。

        同一平台 ID 重放幂等；不同平台 ID 冲突与状态冲突抛出
        ``ReplyFulfillmentOpsConflictError``。
        """
        outcome = await self._repository.reconcile_delivered(
            fulfillment_id,
            platform_message_id=platform_message_id,
        )
        if outcome == "not_found":
            raise ReplyFulfillmentOpsNotFoundError("回复履约不存在")
        if outcome in {"platform_message_conflict", "state_conflict"}:
            raise ReplyFulfillmentOpsConflictError("送达对账冲突")
        return {
            "fulfillment_id": fulfillment_id,
            "status": "processing",
            "idempotent_replay": outcome == "idempotent",
            "platform_message_id": platform_message_id,
        }

    async def confirm_not_delivered(self, fulfillment_id: str) -> dict[str, Any]:
        """确认未送达：终态落库后幂等释放主动预占。

        释放失败不回滚已落库的终态，只如实返回
        ``reservation_released=False``；已送达不可翻案。
        """
        result = await self._repository.reconcile_not_delivered(fulfillment_id)
        outcome = str(result["outcome"])
        if outcome == "not_found":
            raise ReplyFulfillmentOpsNotFoundError("回复履约不存在")
        if outcome == "state_conflict":
            raise ReplyFulfillmentOpsConflictError("送达对账冲突")
        if outcome == "idempotent":
            return {"idempotent_replay": True, "reservation_released": False}
        group_id = result.get("proactive_group_id")
        reservation_id = result.get("proactive_reservation_id")
        released = False
        if group_id is not None and reservation_id is not None:
            try:
                await self._proactive_reservation.release(group_id, reservation_id)
            except Exception:
                released = False
            else:
                released = True
        return {"idempotent_replay": False, "reservation_released": released}

    async def resume_commitment(
        self,
        fulfillment_id: str,
        *,
        commitment_type: str,
    ) -> dict[str, Any]:
        """续跑指定失败承诺：只接受固定 COMMITMENT_TYPES，不开放动态注册。

        只允许父 DELIVERED 且未完成、子 FAILED -> PENDING；不重新发送
        回复，也不伪造完成。非法承诺类型抛出
        ``ReplyFulfillmentOpsValidationError``，状态冲突抛出
        ``ReplyFulfillmentOpsConflictError``。
        """
        if commitment_type not in COMMITMENT_TYPES:
            msg = f"不支持的承诺类型: {commitment_type!r}"
            raise ReplyFulfillmentOpsValidationError(msg)
        outcome = await self._repository.resume_failed_commitment(
            fulfillment_id,
            commitment_type=commitment_type,
        )
        if outcome == "not_found":
            raise ReplyFulfillmentOpsNotFoundError("回复履约或承诺不存在")
        if outcome == "state_conflict":
            raise ReplyFulfillmentOpsConflictError("承诺状态不允许续跑")
        return {
            "fulfillment_id": fulfillment_id,
            "status": "processing",
            "commitment_type": commitment_type,
            "state": "PENDING",
        }


__all__ = [
    "ReplyFulfillmentOpsConflictError",
    "ReplyFulfillmentOpsNotFoundError",
    "ReplyFulfillmentOpsService",
    "ReplyFulfillmentOpsValidationError",
    "build_reply_fulfillment_ops_service",
    "derive_reply_fulfillment_status",
]
