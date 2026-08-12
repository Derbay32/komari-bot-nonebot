"""回复履约去重告警的窄服务（TSK-85）。

当新父子回复履约首次进入 ``pending_confirmation``，或某个固定送达后
承诺首次耗尽进入 ``needs_disposition``，本服务从仓库原子领取转换并
产生一次跨进程/重启可去重、脱敏的运维告警：先写一条结构化日志，再
使用任一在线 Bot 尽力私聊全部合法 SUPERUSERS。

告警只携带四个白名单字段（``fulfillment_id``、派生 ``status``、
``commitment_type``、稳定 ``error_code``），绝不携带回复正文、群历史、
画像、互动载荷、提示词、推理、URL、凭据或原始异常。跨进程去重完全
由仓库的持久化 claim 事实负责，本服务不维护任何进程内去重状态；
普通 ``RETRY_WAIT``/自动瞬态重试不进入任何告警 claim，不在此生成
告警。本服务是窄 seam，未被接到生产正常路径，也不触碰旧 outbox。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from nonebot import logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from ..repositories.reply_fulfillment_repository import (
        ReplyFulfillmentRepository,
    )

# 每轮恢复默认的告警批量上限；claim 只影响去重事实，不会重发。
_DEFAULT_CLAIM_LIMIT = 100

# 私聊卡正文固定模板：只渲染四个白名单字段，不拼接任何其他内容。
_ALERT_MESSAGE_TEMPLATE = (
    "回复履约告警，请及时处置。\n"
    "fulfillment_id: {fulfillment_id}\n"
    "status: {status}\n"
    "commitment_type: {commitment_type}\n"
    "error_code: {error_code}"
)


class ReplyFulfillmentAlertBot(Protocol):
    """告警私聊投递的最小 Bot 边界。"""

    async def send_private_msg(self, *, user_id: int, message: str) -> object: ...


class ReplyFulfillmentAlertService:
    """消费仓库告警 claim 并生成脱敏运维告警的窄服务。

    所有外部依赖（仓库、Bot 提供者、SUPERUSERS 提供者、日志器）以
    构造参数注入，便于验收替身与生产实现互换。本服务不接线承诺
    worker，也不维护进程内去重集合。
    """

    def __init__(
        self,
        repository: ReplyFulfillmentRepository,
        bots_provider: Callable[[], Iterable[ReplyFulfillmentAlertBot]],
        superusers_provider: Callable[[], Iterable[str]],
        logger: Any = logger,
    ) -> None:
        self._repository = repository
        self._bots_provider = bots_provider
        self._superusers_provider = superusers_provider
        self._logger = logger

    async def recover_alerts(self, *, limit: int = _DEFAULT_CLAIM_LIMIT) -> int:
        """恢复并发送本轮待告警转换，返回本次告警条数。

        先领取待确认告警，再领取承诺待处置告警；两类转换各自只被
        原子领取一次，服务重建、重复观察或并发 worker 都不会重发。
        私聊投递全程尽力而为：无 Bot、配置枚举失败或单个收件人发送
        失败都不冒泡，也不阻断其他收件人。
        """
        recovered = 0
        pending = await self._repository.claim_pending_confirmation_alerts(limit=limit)
        for row in pending:
            await self._dispatch_alert(row)
            recovered += 1
        dispositions = await self._repository.claim_commitment_disposition_alerts(
            limit=limit
        )
        for row in dispositions:
            await self._dispatch_alert(row)
            recovered += 1
        return recovered

    async def _dispatch_alert(self, row: Mapping[str, Any]) -> None:
        """对单个已领取转换写结构化告警并尽力私聊全部合法 SUPERUSERS。

        白名单字段在此收敛：无论仓库行还携带多少其他内容，日志与
        私聊卡都只透出这四个字段。
        """
        fields = self._project_allowlisted_fields(row)
        self._logger.bind(**fields).warning("回复履约告警，请及时处置")
        await self._notify_superusers(fields)

    def _project_allowlisted_fields(
        self,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        """把仓库行投影为告警白名单字段，丢弃其余一切内容。"""
        return {
            "fulfillment_id": str(row["fulfillment_id"]),
            "status": str(row["status"]),
            "commitment_type": row.get("commitment_type"),
            "error_code": row.get("error_code"),
        }

    async def _notify_superusers(self, fields: Mapping[str, Any]) -> None:
        """尽力私聊全部合法 SUPERUSERS；任何失败都只跳过不冒泡。"""
        bots = self._safe_bots()
        if not bots:
            return
        message = _ALERT_MESSAGE_TEMPLATE.format(**fields)
        for user_id in self._legal_superuser_ids():
            for bot in bots:
                try:
                    await bot.send_private_msg(user_id=user_id, message=message)
                except Exception:
                    # 单一收件人/单个 Bot 投递失败不冒泡，尝试下一个 Bot
                    continue
                else:
                    break

    def _safe_bots(self) -> list[ReplyFulfillmentAlertBot]:
        """枚举在线 Bot；配置枚举失败视同无 Bot，不冒泡。"""
        try:
            return list(self._bots_provider())
        except Exception:
            return []

    def _legal_superuser_ids(self) -> list[int]:
        """解析全部合法 SUPERUSERS；非法项静默跳过，顺序按 ID 稳定排列。"""
        try:
            raw = self._superusers_provider()
        except Exception:
            return []
        ids: list[int] = []
        for value in raw:
            try:
                ids.append(int(str(value)))
            except (TypeError, ValueError):
                continue
        return sorted(ids)


__all__ = [
    "ReplyFulfillmentAlertBot",
    "ReplyFulfillmentAlertService",
]
