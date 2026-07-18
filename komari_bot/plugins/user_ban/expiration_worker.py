"""临时封禁自然解封定时任务。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from .event_support import is_configured_superuser_id
from .notifications import get_first_available_bot, notify_expired_records
from .service import BanServiceUnavailableError, get_service

EXPIRATION_JOB_ID = "user_ban_expiration_worker"
EXPIRATION_INTERVAL_SECONDS = 30
NOTIFICATION_LEASE_SECONDS = 60
NOTIFICATION_SEND_TIMEOUT_SECONDS = 20
NOTIFICATION_BATCH_SIZE = 100


async def run_expiration_sweep() -> None:
    """清理到期记录，并用持久 outbox 领取、发送和确认自然解封通知。"""
    service = get_service()
    try:
        await service.expire_due_bans()
    except BanServiceUnavailableError:
        logger.exception("[UserBan] 自然解封清理失败")
        return

    bot = get_first_available_bot()
    if bot is None:
        logger.info("[UserBan] 当前无在线 Bot，自然解封通知保留在 outbox")
        return

    owner_token = f"expiration-{uuid4().hex}"
    for _ in range(NOTIFICATION_BATCH_SIZE):
        try:
            notification = await service.claim_expired_notification(
                owner_token=owner_token,
                lease_seconds=NOTIFICATION_LEASE_SECONDS,
            )
        except BanServiceUnavailableError:
            logger.exception("[UserBan] 自然解封通知领取失败")
            return
        if notification is None:
            return

        try:
            async with asyncio.timeout(NOTIFICATION_SEND_TIMEOUT_SECONDS):
                delivery = await notify_expired_records(
                    bot,
                    user_id=notification.user_id,
                    records=notification.records,
                    superuser_bypass=is_configured_superuser_id(
                        notification.user_id
                    ),
                )
        except TimeoutError:
            delivery_sent = False
            error_code = "private_message_timeout"
        else:
            delivery_sent = delivery.sent
            error_code = (
                "private_message_send_failed" if not delivery.sent else None
            )

        try:
            if delivery_sent:
                acknowledged = await service.acknowledge_expired_notification(
                    notification_id=notification.notification_id,
                    owner_token=owner_token,
                )
                if not acknowledged:
                    logger.critical(
                        "[UserBan] 自然解封通知已发送但 ACK 失败: notification_id={}",
                        notification.notification_id,
                    )
                    return
                continue

            retry_delay = min(
                300.0,
                5.0 * (2 ** min(notification.attempt_count - 1, 6)),
            )
            await service.retry_expired_notification(
                notification_id=notification.notification_id,
                owner_token=owner_token,
                error_code=error_code or "private_message_send_failed",
                retry_delay_seconds=retry_delay,
            )
        except BanServiceUnavailableError:
            logger.exception("[UserBan] 自然解封通知状态提交失败")
            return


def register_expiration_job() -> None:
    """注册每 30 秒执行一次的自然解封任务。"""
    scheduler.add_job(
        run_expiration_sweep,
        "interval",
        seconds=EXPIRATION_INTERVAL_SECONDS,
        id=EXPIRATION_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[UserBan] 自然解封任务已注册（每 30 秒）")


def unregister_expiration_job() -> None:
    """注销自然解封任务。"""
    try:
        scheduler.remove_job(EXPIRATION_JOB_ID)
    except JobLookupError:
        logger.debug("[UserBan] 自然解封任务不存在，无需注销")
    except Exception:
        logger.exception("[UserBan] 自然解封任务注销失败")
