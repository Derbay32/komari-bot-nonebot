"""TSK-231 管理面群目标准入红基线测试共享基础设施（测试专用，不承载生产语义）。

本模块承载把真实管理 Router（reply_fulfillment / announce / dead-letter /
komari_memory）装配为可测 FastAPI 应用所需的确定性依赖与 fake，并在内存中
安装脚本化 ``adjudicate``（复用聊天验收的 ScriptedAdjudicate）。当前生产
管理端点在处理群目标效果前不调用顶层 ``adjudicate``，因此受限/失败态下效果
仍执行、列表仍回显受限群存在性，本工单验收用例以红态失败终止，证明接入缺失。

本模块不新增任何生产文件或生产改动，只提供测试专用 fake / 装配 / helper。
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from komari_bot.plugins.komari_memory.services.conversation_processing import (
    ConversationDeadLetter,
)
from tests.group_admission.chat_admission_support import install_scripted_adjudicate

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx import AsyncClient
    from pytest import MonkeyPatch

#: 管理凭据矩阵：资源读写 + 通配 SUPERUSER（只负责认证，不提供准入 bypass）。
MANAGEMENT_CREDENTIALS: tuple[Mapping[str, object], ...] = (
    {
        "credential_id": "memory-reader",
        "token": "memory-reader-token-0000",
        "permissions": ["memory:read"],
    },
    {
        "credential_id": "memory-writer",
        "token": "memory-writer-token-0000",
        "permissions": ["memory:write"],
    },
    {
        "credential_id": "fulfillment-manager",
        "token": "fulfillment-manage-token-00",
        "permissions": ["reply_fulfillment:read", "reply_fulfillment:manage"],
    },
    {
        "credential_id": "announce-operator",
        "token": "announce-send-token-000000",
        "permissions": ["announce:read", "announce:send"],
    },
    {
        "credential_id": "superuser",
        "token": "superuser-token-00000000",
        "permissions": ["*"],
    },
)

#: 管理面用到的受控群号（数字字符串），受限组统一 10002，可获准组 10001。
ALLOWED_GROUP_ID = "10001"
RESTRICTED_GROUP_ID = "10002"

#: ScriptedAdjudicate 断言的归一化正整群集合形态（传参必须归一化正 int 集合）。
NORMALIZED_ALLOWED = [10001]
NORMALIZED_RESTRICTED = [10002]


def install_scripted(monkeypatch: MonkeyPatch, scripted: Any) -> None:
    """把脚本化 adjudicate 安装到 group_admission 包顶层命名空间。"""
    install_scripted_adjudicate(monkeypatch, scripted)


def asgi_client(app: Any) -> AsyncClient:
    """ASGI 内存客户端（不启动真实前端服务、不触发 lifespan）。"""
    import httpx

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://manage.test")


def auth_headers(
    token: str,
    *,
    reason: str | None = "acceptance-tsk231",
    request_id: str | None = None,
) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if reason:
        headers["X-Komari-Change-Reason"] = reason
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


# ---------------------------------------------------------------------------
# 回复履约运维 fake（对外部效果计数，供 admission 拦截后断言“未执行”）
# ---------------------------------------------------------------------------
def _fulfillment_detail(group_id: str = ALLOWED_GROUP_ID) -> dict[str, Any]:
    return {
        "fulfillment_id": "reply-target-0001",
        "request_trace_id": "trace-safe-0001",
        "trigger_message_id": "message-safe-0001",
        "group_id": group_id,
        "status": "pending_confirmation",
        "reply_fingerprint": "a" * 64,
        "prepared_at": "2026-08-11T00:00:00+00:00",
        "send_started_at": "2026-08-11T00:00:01+00:00",
        "delivered_at": None,
        "platform_message_id": None,
        "not_delivered_at": None,
        "completed_at": None,
        "commitments": [
            {
                "commitment_type": "favorability_adjustment",
                "state": "FAILED",
                "attempt_count": 3,
                "next_retry_at": None,
                "last_error_code": "service_unavailable",
                "completed_at": None,
            }
        ],
        "reply_target_message_id": "message-target-safe-0001",
        "reply_content": "pending-body-canary-0001",
    }


class FakeOpsService:
    """回复履约运维服务的受控替身；记录每次下游调用。"""

    def __init__(self, *, group_id: str = ALLOWED_GROUP_ID) -> None:
        self.group_id = group_id
        self.detail: dict[str, Any] | None = _fulfillment_detail(group_id)
        self.delivered_calls: list[tuple[str, str | None]] = []
        self.not_delivered_calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    def _summary(self) -> dict[str, Any]:
        assert self.detail is not None
        summary = deepcopy(self.detail)
        summary.pop("reply_target_message_id", None)
        summary.pop("reply_content", None)
        return summary

    async def list_fulfillments(
        self, *, status: str | None, limit: int, offset: int
    ) -> dict[str, Any]:
        self.list_calls.append({"status": status, "limit": limit, "offset": offset})
        return {
            "items": [self._summary()],
            "total": 1,
            "limit": limit,
            "offset": offset,
        }

    async def get_fulfillment(self, fulfillment_id: str) -> dict[str, Any] | None:
        self.get_calls.append(fulfillment_id)
        return deepcopy(self.detail)

    async def confirm_delivered(
        self, fulfillment_id: str, *, platform_message_id: str | None
    ) -> dict[str, Any]:
        self.delivered_calls.append((fulfillment_id, platform_message_id))
        return {
            "fulfillment_id": fulfillment_id,
            "status": "not_delivered_state_pending",
            "idempotent_replay": False,
            "platform_message_id": platform_message_id,
        }

    async def confirm_not_delivered(self, fulfillment_id: str) -> dict[str, Any]:
        self.not_delivered_calls.append(fulfillment_id)
        return {
            "fulfillment_id": fulfillment_id,
            "status": "not_delivered",
            "idempotent_replay": False,
            "reservation_released": True,
        }

    async def resume_commitment(
        self, fulfillment_id: str, *, commitment_type: str
    ) -> dict[str, Any]:
        self.resume_calls.append((fulfillment_id, commitment_type))
        return {
            "fulfillment_id": fulfillment_id,
            "status": "not_delivered",
            "commitment_type": commitment_type,
            "state": "PENDING",
        }


# ---------------------------------------------------------------------------
# dead-letter fake（Redis 摘要、无正文；list / requeue 计数）
# ---------------------------------------------------------------------------
class FakeDeadLetterManager:
    """对话失败快照 Redis 管理器替身；记录 list / requeue 调用。"""

    def __init__(self, *, group_ids: list[str] | None = None) -> None:
        self.group_ids = group_ids or [ALLOWED_GROUP_ID, RESTRICTED_GROUP_ID]
        self.list_calls: list[int] = []
        self.requeue_calls: list[tuple[str, str]] = []
        #: 无正文摘要：只含 group/snapshot/失败元数据（生产 REST 契约）。
        self.items = [
            ConversationDeadLetter(
                group_id=group_id,
                snapshot_id=f"snap-{group_id}",
                failure_code="conversation_processing_failed",
                attempt_count=2,
                failed_at_ms=1_000_000,
                message_count=0,
                chunk_state_count=2,
            )
            for group_id in self.group_ids
        ]

    async def list_conversation_dead_letters(self, *, limit: int = 100) -> list[ConversationDeadLetter]:
        self.list_calls.append(limit)
        return list(self.items)

    async def requeue_conversation_dead_letter(self, *, group_id: str, snapshot_id: str) -> int | None:
        self.requeue_calls.append((group_id, snapshot_id))
        if group_id not in self.group_ids:
            return None  # 未找到 -> 404
        return 3  # restored_message_count

# ---------------------------------------------------------------------------
# 群记忆服务 fake（create / list / get / update / delete conversation 计数）
# ---------------------------------------------------------------------------
def conversation_entry(*, conversation_id: int = 1, group_id: str = ALLOWED_GROUP_ID) -> dict[str, Any]:
    """构造一条对话记忆条目（REST 契约形状）。"""
    return {
        "id": conversation_id,
        "group_id": group_id,
        "summary": "一起聊了布丁",
        "participants": ["u1"],
        "start_time": "2026-04-10T12:00:00+00:00",
        "end_time": "2026-04-10T12:00:00+00:00",
        "importance_initial": 4,
        "importance_current": 4,
        "last_accessed": "2026-04-10T12:00:00+00:00",
        "created_at": "2026-04-10T12:00:00+00:00",
    }


class FakeMemoryService:
    """komari_memory 服务替身；记录 create / delete / list 调用用于“已拦截”断言。"""

    def __init__(self, *, group_ids: list[str] | None = None) -> None:
        self.group_ids = group_ids or [ALLOWED_GROUP_ID, RESTRICTED_GROUP_ID]
        self.rows: dict[int, dict[str, Any]] = {}
        for idx, group_id in enumerate(self.group_ids, start=1):
            self.rows[idx] = conversation_entry(conversation_id=idx, group_id=group_id)
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[int] = []
        self.list_calls: int = 0

    async def list_conversations(
        self, **kwargs: object
    ) -> tuple[list[dict[str, Any]], int]:
        del kwargs
        self.list_calls += 1
        return list(self.rows.values()), len(self.rows)

    async def get_conversation_entry(
        self, conversation_id: int
    ) -> dict[str, Any] | None:
        return self.rows.get(conversation_id)

    async def create_conversation_entry(self, **kwargs: object) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        new_id = max(self.rows, default=0) + 1
        entry = conversation_entry(conversation_id=new_id, group_id=str(kwargs.get("group_id") or ALLOWED_GROUP_ID))
        self.rows[new_id] = entry
        return entry

    async def update_conversation_entry(
        self, conversation_id: int, **kwargs: object
    ) -> dict[str, Any] | None:
        del kwargs
        return self.rows.get(conversation_id)

    async def delete_conversation_entry(self, conversation_id: int) -> bool:
        self.delete_calls.append(conversation_id)
        return conversation_id in self.rows


# ---------------------------------------------------------------------------
# 维护公告 fake bot（get_group_list / send_group_msg，sent 计数）
# ---------------------------------------------------------------------------
class FakeBot:
    """应答 ``get_group_list`` 与 ``send_group_msg`` 的受控 Bot 替身。"""

    def __init__(
        self,
        *,
        groups: list[dict[str, Any]] | None = None,
        fail_group_ids: set[int] | None = None,
    ) -> None:
        self.groups = groups if groups is not None else [
            {"group_id": int(ALLOWED_GROUP_ID), "group_name": "可获准群", "member_count": 12},
            {"group_id": int(RESTRICTED_GROUP_ID), "group_name": "受限群", "member_count": 8},
        ]
        self.fail_group_ids = fail_group_ids or set()
        self.sent_messages: list[dict[str, Any]] = []

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        if api == "get_group_list":
            return self.groups
        if api == "send_group_msg":
            self.sent_messages.append({"api": api, **kwargs})
            if kwargs["group_id"] in self.fail_group_ids:
                raise RuntimeError("发送失败")
            return {"message_id": 1}
        raise AssertionError(f"未料到的 api: {api}")  # noqa: TRY003
