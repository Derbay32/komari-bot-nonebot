"""临时封禁自然解封定时任务。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from .event_support import is_configured_superuser_id
from .notifications import get_first_available_bot, notify_expired_records
from .service import BanServiceUnavailableError, get_service

if TYPE_CHECKING:
    from .models import BanRecord

EXPIRATION_JOB_ID = "user_ban_expiration_worker"
EXPIRATION_INTERVAL_SECONDS = 30


async def run_expiration_sweep() -> None:
    """清理到期记录，并按用户合并发送自然解封通知。"""
    try:
        expired = await get_service().expire_due_bans()
    except BanServiceUnavailableError as error:
        logger.error("[UserBan] 自然解封任务执行失败：{}", error)
        return
    if not expired:
        return

    records_by_user: dict[str, list[BanRecord]] = {}
    for record in expired:
        records_by_user.setdefault(record.user_id, []).append(record)

    bot = get_first_available_bot()
    for user_id, records in records_by_user.items():
        await notify_expired_records(
            bot,
            user_id=user_id,
            records=tuple(records),
            superuser_bypass=is_configured_superuser_id(user_id),
        )


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
