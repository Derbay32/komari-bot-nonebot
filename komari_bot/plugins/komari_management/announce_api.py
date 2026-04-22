"""Komari Management 维护通知 REST API。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from komari_bot.common.management_api import (
    create_bearer_auth_dependency,
    ensure_management_cors,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

API_PREFIX = "/api/komari-announce/v1"


class GroupInfo(BaseModel):
    """群信息摘要。"""

    group_id: int
    group_name: str
    member_count: int


class GroupListResponse(BaseModel):
    """群列表响应。"""

    groups: list[GroupInfo]
    total: int


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
    error: str | None = None


class MaintenanceAnnounceResponse(BaseModel):
    """维护通知发送结果。"""

    results: list[AnnounceResult]
    total: int
    success_count: int
    failed_count: int


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


def _build_group_info(raw_group: dict[str, Any]) -> GroupInfo:
    group_id = int(raw_group["group_id"])
    return GroupInfo(
        group_id=group_id,
        group_name=str(raw_group.get("group_name") or group_id),
        member_count=int(raw_group.get("member_count", 0) or 0),
    )


def create_announce_router(*, api_token: str, status_page_url: str) -> APIRouter:
    """创建维护通知路由。"""
    auth_dependency = create_bearer_auth_dependency(
        api_token,
        detail="未授权访问维护通知接口",
    )
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

        bot = next(iter(bots.values()))
        raw_groups = await bot.call_api("get_group_list")
        groups = [_build_group_info(raw_group) for raw_group in raw_groups]
        return GroupListResponse(groups=groups, total=len(groups))

    @router.post("/maintenance", response_model=MaintenanceAnnounceResponse)
    async def send_maintenance_announce(
        payload: Annotated[MaintenanceAnnounceRequest, Body()],
    ) -> MaintenanceAnnounceResponse:
        """向指定群发送维护通知。"""
        from nonebot import get_bots

        bots = get_bots()
        if not bots:
            raise HTTPException(status_code=503, detail="Bot 不在线，无法发送消息")

        bot = next(iter(bots.values()))
        message_text = _build_maintenance_message(
            payload.title,
            payload.content,
            payload.scheduled_time,
            status_page_url,
        )
        results: list[AnnounceResult] = []
        for group_id in payload.group_ids:
            try:
                await bot.call_api(
                    "send_group_msg",
                    group_id=group_id,
                    message=message_text,
                )
                results.append(AnnounceResult(group_id=group_id, success=True))
            except Exception as exc:
                results.append(
                    AnnounceResult(
                        group_id=group_id,
                        success=False,
                        error=str(exc),
                    )
                )

        success_count = sum(1 for result in results if result.success)
        failed_count = len(results) - success_count
        return MaintenanceAnnounceResponse(
            results=results,
            total=len(results),
            success_count=success_count,
            failed_count=failed_count,
        )

    return router


def register_announce_api(
    app: FastAPI,
    *,
    api_token: str,
    allowed_origins: Sequence[str],
    status_page_url: str,
) -> None:
    """注册维护通知 API。"""
    if getattr(app.state, "komari_announce_api_registered", False):
        return

    ensure_management_cors(app, allowed_origins)
    app.include_router(
        create_announce_router(
            api_token=api_token,
            status_page_url=status_page_url,
        )
    )
    app.state.komari_announce_api_registered = True
