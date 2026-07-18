"""Komari Management 维护通知 REST API。"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from nonebot import logger
from nonebot.adapters.onebot.v11.exception import NetworkError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from komari_bot.common.content_budget import (
    TITLE_TEXT_BUDGET,
    ContentValidationError,
    TextBudget,
    normalize_required_text,
    validate_text_budget,
)
from komari_bot.common.management_api import (
    ManagementPrincipal,
    create_bearer_auth_dependency,
    ensure_management_cors,
)
from komari_bot.common.management_audit import (
    hash_management_target,
    management_audit_span,
    record_management_audit_event,
    require_management_change_reason,
    resolve_management_request_id,
)
from komari_bot.common.onebot_messages import plain_text_message

from .announcement_repository import (
    DispatchClaim,
    get_announcement_dispatch_repository,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from komari_bot.common.management_api import ManagementTokenSource
    from komari_bot.common.management_audit import ManagementAuditRecorder

API_PREFIX = "/api/komari-announce/v1"
ANNOUNCE_CONTENT_BUDGET = TextBudget(3000, 9000, 3000)
ANNOUNCE_TIME_BUDGET = TextBudget(128, 512, 128)
ANNOUNCE_MESSAGE_BUDGET = TextBudget(4096, 12_288, 4096)
ANNOUNCE_BOT_API_TIMEOUT_SECONDS = 15.0
ANNOUNCE_MAX_FAILOVER_CANDIDATES = 3
ANNOUNCE_MIN_DISPATCH_LEASE_SECONDS = 300
ANNOUNCE_MAX_DISPATCH_LEASE_SECONDS = 10_800


class AnnouncementDispatchProtocol(Protocol):
    async def claim(
        self,
        *,
        request_id: str,
        payload_hash: str,
        owner_token: str,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> DispatchClaim: ...

    async def complete(
        self,
        *,
        request_id: str,
        owner_token: str,
        response_payload: dict[str, Any],
    ) -> bool: ...

    async def mark_reconciliation_required(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> None: ...

    async def cancel_unstarted(
        self,
        *,
        request_id: str,
        owner_token: str,
    ) -> bool: ...


class GroupInfo(BaseModel):
    """群信息摘要。"""

    group_id: int
    group_name: str
    member_count: int
    bot_ids: list[str] = Field(default_factory=list)


class GroupListResponse(BaseModel):
    """群列表响应。"""

    groups: list[GroupInfo]
    total: int
    unavailable_bot_count: int = 0


class MaintenanceAnnounceRequest(BaseModel):
    """维护通知发送请求。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="维护标题", min_length=1)
    content: str = Field(description="维护内容，多行文本，每行一条", min_length=1)
    scheduled_time: str = Field(description="预定维护时间", min_length=1)
    group_ids: list[int] = Field(
        description="目标群号列表",
        min_length=1,
        max_length=100,
    )

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return normalize_required_text(
            value,
            label="维护标题",
            budget=TITLE_TEXT_BUDGET,
        )

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return normalize_required_text(
            value,
            label="维护内容",
            budget=ANNOUNCE_CONTENT_BUDGET,
        )

    @field_validator("scheduled_time")
    @classmethod
    def validate_scheduled_time(cls, value: str) -> str:
        return normalize_required_text(
            value,
            label="预定维护时间",
            budget=ANNOUNCE_TIME_BUDGET,
        )

    @model_validator(mode="after")
    def validate_unique_groups(self) -> MaintenanceAnnounceRequest:
        if len(self.group_ids) != len(set(self.group_ids)):
            raise ValueError("目标群列表不允许重复")
        return self


class AnnounceResult(BaseModel):
    """单个群发送结果。"""

    group_id: int
    success: bool
    status: Literal["success", "unreachable", "failed"]
    bot_id: str | None = None
    error_code: str | None = None
    error: str | None = None


class MaintenanceAnnounceResponse(BaseModel):
    """维护通知发送结果。"""

    results: list[AnnounceResult]
    total: int
    success_count: int
    failed_count: int
    unreachable_count: int
    unavailable_bot_count: int


@dataclass(frozen=True, slots=True)
class _BotGroupSnapshot:
    """一次多 Bot 群可达性探测结果。"""

    routes: dict[int, tuple[tuple[str, Any], ...]]
    groups: list[GroupInfo]
    unavailable_bot_count: int


def _build_maintenance_message(
    title: str,
    content: str,
    scheduled_time: str,
    status_page_url: str,
) -> str:
    """拼接维护通知纯文本消息。"""
    message = (
        "📢 预定维护通知\n\n"
        "【维护标题】\n"
        f"{title}\n\n"
        "【维护内容】\n"
        f"{content}\n\n"
        "【预定维护时间】\n"
        f"{scheduled_time}\n\n"
        "※ 实际的维护结束时间可能会提前或推迟\n"
        "※ 具体维护情况参考 Komari Bot Status 页面：\n"
        f"   {status_page_url}"
    )
    return validate_text_budget(
        message,
        label="维护通知总消息",
        budget=ANNOUNCE_MESSAGE_BUDGET,
    )


def _announcement_payload_hash(
    payload: MaintenanceAnnounceRequest,
    message_text: str,
) -> str:
    canonical = json.dumps(
        {
            "group_ids": payload.group_ids,
            "message_text": message_text,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _dispatch_lease_seconds(
    *,
    group_count: int,
    send_interval_seconds: float,
) -> int:
    """按最坏发送耗时为请求生成足够长、但有上限的 owner 租约。"""
    estimated = 120.0 + group_count * (
        max(send_interval_seconds, 0.0)
        + ANNOUNCE_BOT_API_TIMEOUT_SECONDS * ANNOUNCE_MAX_FAILOVER_CANDIDATES
    )
    return min(
        ANNOUNCE_MAX_DISPATCH_LEASE_SECONDS,
        max(ANNOUNCE_MIN_DISPATCH_LEASE_SECONDS, math.ceil(estimated)),
    )


def _require_completed_dispatch(*, completed: bool) -> None:
    if not completed:
        raise HTTPException(
            status_code=503,
            detail="公告已执行但幂等结果确认失败，需要人工对账",
        )


def _delivery_result_is_unknown(exc: Exception) -> bool:
    """网络中断和调用超时无法证明平台未接收，禁止换 Bot 重发。"""
    return isinstance(exc, (TimeoutError, NetworkError, OSError))


def _build_group_info(raw_group: dict[str, Any], bot_id: str) -> GroupInfo:
    group_id = int(raw_group["group_id"])
    return GroupInfo(
        group_id=group_id,
        group_name=str(raw_group.get("group_name") or group_id),
        member_count=int(raw_group.get("member_count", 0) or 0),
        bot_ids=[bot_id],
    )


async def _fetch_bot_groups(
    bot_id: str,
    bot: Any,
) -> tuple[str, Any, list[dict[str, Any]] | None]:
    try:
        raw_groups = await asyncio.wait_for(
            bot.call_api("get_group_list"),
            timeout=ANNOUNCE_BOT_API_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "[Komari Management] 获取 Bot 群列表失败: "
            "bot_id={} error_type={}",
            bot_id,
            type(exc).__name__,
        )
        return bot_id, bot, None
    if not isinstance(raw_groups, list):
        logger.warning(
            "[Komari Management] Bot 群列表响应结构无效: bot_id={}",
            bot_id,
        )
        return bot_id, bot, None
    return (
        bot_id,
        bot,
        [item for item in raw_groups if isinstance(item, dict)],
    )


async def _discover_bot_group_routes(
    bots: Mapping[str, Any],
) -> _BotGroupSnapshot:
    """并发读取所有 Bot 群列表，并按群构造确定性路由。"""
    fetched = await asyncio.gather(
        *(
            _fetch_bot_groups(str(bot_id), bot)
            for bot_id, bot in sorted(bots.items(), key=lambda item: str(item[0]))
        )
    )
    routes: dict[int, list[tuple[str, Any]]] = {}
    group_info: dict[int, GroupInfo] = {}
    unavailable_bot_count = 0

    for bot_id, bot, raw_groups in fetched:
        if raw_groups is None:
            unavailable_bot_count += 1
            continue
        for raw_group in raw_groups:
            try:
                info = _build_group_info(raw_group, bot_id)
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "[Komari Management] 忽略无效群列表条目: bot_id={}",
                    bot_id,
                )
                continue
            routes.setdefault(info.group_id, []).append((bot_id, bot))
            existing = group_info.get(info.group_id)
            if existing is None:
                group_info[info.group_id] = info
            else:
                existing.bot_ids.append(bot_id)
                existing.bot_ids.sort()
                existing.member_count = max(existing.member_count, info.member_count)
                if existing.group_name == str(info.group_id):
                    existing.group_name = info.group_name

    frozen_routes = {
        group_id: tuple(sorted(candidates, key=lambda item: item[0]))
        for group_id, candidates in routes.items()
    }
    return _BotGroupSnapshot(
        routes=frozen_routes,
        groups=sorted(group_info.values(), key=lambda item: item.group_id),
        unavailable_bot_count=unavailable_bot_count,
    )


async def _send_group_announcement(
    *,
    group_id: int,
    candidates: tuple[tuple[str, Any], ...],
    message_text: str,
) -> AnnounceResult:
    """仅在平台明确失败时切换候选 Bot；未知送达结果绝不重发。"""
    last_failed_bot_id: str | None = None
    for bot_id, bot in candidates[:ANNOUNCE_MAX_FAILOVER_CANDIDATES]:
        try:
            await asyncio.wait_for(
                bot.call_api(
                    "send_group_msg",
                    group_id=group_id,
                    message=plain_text_message(message_text),
                ),
                timeout=ANNOUNCE_BOT_API_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            last_failed_bot_id = bot_id
            if _delivery_result_is_unknown(exc):
                logger.error(
                    "[Komari Management] 维护公告送达结果未知，禁止自动重发: "
                    "bot_id={} target_hash={} error_type={}",
                    bot_id,
                    hash_management_target(group_id),
                    type(exc).__name__,
                )
                return AnnounceResult(
                    group_id=group_id,
                    success=False,
                    status="failed",
                    bot_id=bot_id,
                    error_code="delivery_unknown",
                    error="平台送达结果未知，已停止自动重发，请人工核对",
                )
            logger.warning(
                "[Komari Management] 维护公告候选 Bot 明确发送失败: "
                "bot_id={} target_hash={} error_type={}",
                bot_id,
                hash_management_target(group_id),
                type(exc).__name__,
            )
            continue
        return AnnounceResult(
            group_id=group_id,
            success=True,
            status="success",
            bot_id=bot_id,
        )
    return AnnounceResult(
        group_id=group_id,
        success=False,
        status="failed",
        bot_id=last_failed_bot_id,
        error_code="send_failed",
        error="所有候选 Bot 的平台发送接口均明确失败",
    )


def create_announce_router(
    *,
    api_token: ManagementTokenSource,
    status_page_url: str,
    announce_max_group_count: int = 20,
    announce_send_interval_seconds: float = 1.0,
    announce_request_cooldown_seconds: float = 30.0,
    audit_recorder: ManagementAuditRecorder | None = None,
    dispatch_repository: AnnouncementDispatchProtocol | None = None,
) -> APIRouter:
    """创建维护通知路由。"""
    dispatches = dispatch_repository or get_announcement_dispatch_repository()
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问维护通知接口",
        required_permission="announce:read",
    )
    send_auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权发送维护通知",
        required_permission="announce:send",
    )
    recorder = audit_recorder or record_management_audit_event
    router = APIRouter(
        prefix=API_PREFIX,
        dependencies=[Depends(auth_dependency)],
        tags=["komari-announce"],
    )

    @router.get("/groups", response_model=GroupListResponse)
    async def list_groups() -> GroupListResponse:
        """获取 Bot 加入的所有群列表。"""
        from nonebot import get_bots

        bots = get_bots()
        if not bots:
            return GroupListResponse(groups=[], total=0)

        snapshot = await _discover_bot_group_routes(bots)
        return GroupListResponse(
            groups=snapshot.groups,
            total=len(snapshot.groups),
            unavailable_bot_count=snapshot.unavailable_bot_count,
        )

    @router.post("/maintenance", response_model=MaintenanceAnnounceResponse)
    async def send_maintenance_announce(
        payload: Annotated[MaintenanceAnnounceRequest, Body()],
        principal: ManagementPrincipal = Depends(send_auth_dependency),  # noqa: FAST002
        reason: str = Depends(require_management_change_reason),  # noqa: FAST002
        request_id: str = Depends(resolve_management_request_id),  # noqa: FAST002
    ) -> MaintenanceAnnounceResponse:
        """向指定群发送维护通知。"""
        from nonebot import get_bots

        target_hash = hash_management_target(*sorted(payload.group_ids))
        async with management_audit_span(
            principal=principal,
            request_id=request_id,
            reason=reason,
            action="announce.maintenance.send",
            resource="group_announcement",
            target_hash=target_hash,
            recorder=recorder,
        ) as audit:
            if len(payload.group_ids) > announce_max_group_count:
                raise HTTPException(
                    status_code=400,
                    detail=f"目标群数量超过上限 {announce_max_group_count}",
                )
            try:
                message_text = _build_maintenance_message(
                    payload.title,
                    payload.content,
                    payload.scheduled_time,
                    status_page_url,
                )
            except ContentValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            owner_token = f"announce-{uuid4().hex}"
            try:
                claim = await dispatches.claim(
                    request_id=request_id,
                    payload_hash=_announcement_payload_hash(payload, message_text),
                    owner_token=owner_token,
                    lease_seconds=_dispatch_lease_seconds(
                        group_count=len(payload.group_ids),
                        send_interval_seconds=announce_send_interval_seconds,
                    ),
                    cooldown_seconds=announce_request_cooldown_seconds,
                )
            except Exception as exc:
                logger.error(
                    "[Komari Management] 公告幂等账本不可用: error_type={}",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=503,
                    detail="维护公告幂等账本暂不可用，未发送任何消息",
                ) from exc

            match claim.state:
                case "replay":
                    if claim.response_payload is None:
                        raise HTTPException(
                            status_code=409,
                            detail="该 request ID 的历史结果需要人工对账",
                        )
                    audit.metadata["idempotent_replay"] = True
                    return MaintenanceAnnounceResponse.model_validate(
                        claim.response_payload
                    )
                case "payload_conflict":
                    raise HTTPException(
                        status_code=409,
                        detail="同一 request ID 不允许绑定不同公告内容",
                    )
                case "in_progress":
                    raise HTTPException(
                        status_code=409,
                        detail="该 request ID 正在由另一个进程处理",
                    )
                case "reconciliation_required":
                    raise HTTPException(
                        status_code=409,
                        detail="该 request ID 的发送状态不确定，需要人工对账，禁止自动重发",
                    )
                case "cooldown":
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "message": "维护通知发送过于频繁，请稍后再试",
                            "remaining_seconds": round(
                                claim.remaining_seconds or 0.0,
                                3,
                            ),
                        },
                    )
                case "claimed":
                    pass

            bots = get_bots()
            if not bots:
                try:
                    released = await dispatches.cancel_unstarted(
                        request_id=request_id,
                        owner_token=owner_token,
                    )
                except Exception as exc:
                    logger.critical(
                        "[Komari Management] 离线公告抢占释放失败: error_type={}",
                        type(exc).__name__,
                    )
                    released = False
                if not released:
                    raise HTTPException(
                        status_code=503,
                        detail="Bot 不在线，且公告账本释放失败，请人工核对后再试",
                    )
                raise HTTPException(status_code=503, detail="Bot 不在线，无法发送消息")

            try:
                snapshot = await _discover_bot_group_routes(bots)
                results: list[AnnounceResult] = []
                for index, group_id in enumerate(payload.group_ids):
                    candidates = snapshot.routes.get(group_id, ())
                    if not candidates:
                        results.append(
                            AnnounceResult(
                                group_id=group_id,
                                success=False,
                                status="unreachable",
                                error_code="group_unreachable",
                                error="没有在线 Bot 可访问该群",
                            )
                        )
                    else:
                        results.append(
                            await _send_group_announcement(
                                group_id=group_id,
                                candidates=candidates,
                                message_text=message_text,
                            )
                        )
                    if (
                        announce_send_interval_seconds > 0
                        and index < len(payload.group_ids) - 1
                    ):
                        await asyncio.sleep(announce_send_interval_seconds)

                success_count = sum(
                    1 for result in results if result.status == "success"
                )
                unreachable_count = sum(
                    1 for result in results if result.status == "unreachable"
                )
                failed_count = len(results) - success_count
                response = MaintenanceAnnounceResponse(
                    results=results,
                    total=len(results),
                    success_count=success_count,
                    failed_count=failed_count,
                    unreachable_count=unreachable_count,
                    unavailable_bot_count=snapshot.unavailable_bot_count,
                )
                completed = await dispatches.complete(
                    request_id=request_id,
                    owner_token=owner_token,
                    response_payload=response.model_dump(mode="json"),
                )
                _require_completed_dispatch(completed=completed)
            except BaseException:
                try:
                    await dispatches.mark_reconciliation_required(
                        request_id=request_id,
                        owner_token=owner_token,
                    )
                except Exception as exc:
                    logger.critical(
                        "[Komari Management] 公告对账状态写入失败: error_type={}",
                        type(exc).__name__,
                    )
                raise

            audit.metadata.update(
                {
                    "target_count": len(results),
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "unreachable_count": unreachable_count,
                    "unavailable_bot_count": snapshot.unavailable_bot_count,
                    "idempotent_replay": False,
                }
            )
            return response

    return router


def register_announce_api(
    app: FastAPI,
    *,
    api_token: ManagementTokenSource,
    allowed_origins: Sequence[str],
    status_page_url: str,
    announce_max_group_count: int = 20,
    announce_send_interval_seconds: float = 1.0,
    announce_request_cooldown_seconds: float = 30.0,
    audit_recorder: ManagementAuditRecorder | None = None,
    dispatch_repository: AnnouncementDispatchProtocol | None = None,
) -> None:
    """注册维护通知 API。"""
    if getattr(app.state, "komari_announce_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_announce_router(
            api_token=api_token,
            status_page_url=status_page_url,
            announce_max_group_count=announce_max_group_count,
            announce_send_interval_seconds=announce_send_interval_seconds,
            announce_request_cooldown_seconds=announce_request_cooldown_seconds,
            audit_recorder=audit_recorder,
            dispatch_repository=dispatch_repository,
        )
    )
    app.state.komari_announce_api_registered = True
