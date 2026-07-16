"""Komari Management 维护通知 REST API。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from nonebot import logger
from pydantic import BaseModel, ConfigDict, Field

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

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from komari_bot.common.management_api import ManagementTokenSource
    from komari_bot.common.management_audit import ManagementAuditRecorder

API_PREFIX = "/api/komari-announce/v1"


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
    group_ids: list[int] = Field(description="目标群号列表", min_length=1)


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
    return (
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
        raw_groups = await bot.call_api("get_group_list")
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


def create_announce_router(
    *,
    api_token: ManagementTokenSource,
    status_page_url: str,
    announce_max_group_count: int = 20,
    announce_send_interval_seconds: float = 1.0,
    announce_request_cooldown_seconds: float = 30.0,
    audit_recorder: ManagementAuditRecorder | None = None,
) -> APIRouter:
    """创建维护通知路由。"""
    cooldown_lock = asyncio.Lock()
    last_send_started_at: float | None = None
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
            if len(payload.group_ids) != len(set(payload.group_ids)):
                raise HTTPException(status_code=400, detail="目标群列表不允许重复")

            bots = get_bots()
            if not bots:
                raise HTTPException(status_code=503, detail="Bot 不在线，无法发送消息")

            nonlocal last_send_started_at
            async with cooldown_lock:
                now = time.monotonic()
                if last_send_started_at is not None:
                    elapsed = now - last_send_started_at
                    remaining_seconds = announce_request_cooldown_seconds - elapsed
                    if remaining_seconds > 0:
                        raise HTTPException(
                            status_code=429,
                            detail={
                                "message": "维护通知发送过于频繁，请稍后再试",
                                "remaining_seconds": round(remaining_seconds, 3),
                            },
                        )
                last_send_started_at = now

            snapshot = await _discover_bot_group_routes(bots)
            message_text = _build_maintenance_message(
                payload.title,
                payload.content,
                payload.scheduled_time,
                status_page_url,
            )
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
                    bot_id, bot = candidates[0]
                    try:
                        await bot.call_api(
                            "send_group_msg",
                            group_id=group_id,
                            message=message_text,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[Komari Management] 维护公告发送失败: "
                            "bot_id={} target_hash={} error_type={}",
                            bot_id,
                            hash_management_target(group_id),
                            type(exc).__name__,
                        )
                        results.append(
                            AnnounceResult(
                                group_id=group_id,
                                success=False,
                                status="failed",
                                bot_id=bot_id,
                                error_code="send_failed",
                                error="平台发送接口调用失败",
                            )
                        )
                    else:
                        results.append(
                            AnnounceResult(
                                group_id=group_id,
                                success=True,
                                status="success",
                                bot_id=bot_id,
                            )
                        )
                if (
                    announce_send_interval_seconds > 0
                    and index < len(payload.group_ids) - 1
                ):
                    await asyncio.sleep(announce_send_interval_seconds)

            success_count = sum(1 for result in results if result.status == "success")
            unreachable_count = sum(
                1 for result in results if result.status == "unreachable"
            )
            failed_count = len(results) - success_count
            audit.metadata.update(
                {
                    "target_count": len(results),
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "unreachable_count": unreachable_count,
                    "unavailable_bot_count": snapshot.unavailable_bot_count,
                }
            )
            return MaintenanceAnnounceResponse(
                results=results,
                total=len(results),
                success_count=success_count,
                failed_count=failed_count,
                unreachable_count=unreachable_count,
                unavailable_bot_count=snapshot.unavailable_bot_count,
            )

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
        )
    )
    app.state.komari_announce_api_registered = True
