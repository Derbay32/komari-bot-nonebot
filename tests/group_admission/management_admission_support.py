"""TSK-231 管理面群目标准入验收共享基础设施（测试专用，不承载生产语义）。

本模块承载把真实管理 Router（reply_fulfillment / announce）装配为可测
FastAPI 应用所需的确定性依赖与 fake，并在内存中安装脚本化 ``adjudicate``
（复用聊天验收的 ScriptedAdjudicate）。旧记忆与 dead-letter 管理面已随
TSK-197 退役，这里只保留仍存在的履约与公告验收依赖。

本模块不新增任何生产文件或生产改动，只提供测试专用 fake / 装配 / helper。
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from tests.group_admission.chat_admission_support import install_scripted_adjudicate

if TYPE_CHECKING:
    from collections.abc import Mapping

    from httpx import AsyncClient
    from pytest import MonkeyPatch

#: 管理凭据矩阵：资源读写 + 通配 SUPERUSER（只负责认证，不提供准入 bypass）。
MANAGEMENT_CREDENTIALS: tuple[Mapping[str, object], ...] = (
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
