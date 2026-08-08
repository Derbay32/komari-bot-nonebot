"""回复失败后的群内错误提示与 SUPERUSER 极简诊断通知。"""

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from komari_bot.plugins.komari_memory.services.redis_manager import RedisManager

from .config_interface import get_memory_config

GROUP_ERROR_TEXT = "啊、啊呜……对不起，脑袋里刚才突然乱成一团了……"

_NOTIFY_COOLDOWN_SECONDS = 300
_SUMMARY_MAX_CHARS = 120


def one_line_summary(error: BaseException) -> str:
    """提取异常的一行摘要（截断，不含换行）。"""
    first_line = str(error).splitlines()[0] if str(error) else ""
    return first_line[:_SUMMARY_MAX_CHARS]


async def send_group_reply_error_text(bot: Bot, event: GroupMessageEvent) -> None:
    """以 reply 形式引用原消息发送固定的群内错误文本；失败仅记日志。"""
    try:
        await bot.call_api(
            "send_group_msg",
            group_id=int(event.group_id),
            message=[
                {"type": "reply", "data": {"id": str(event.message_id)}},
                {"type": "text", "data": {"text": GROUP_ERROR_TEXT}},
            ],
        )
    except Exception:
        logger.warning(
            "[KomariChat] 群内错误提示发送失败: group={}",
            event.group_id,
            exc_info=True,
        )


async def _acquire_notify_cooldown(
    redis: RedisManager | None,
    *,
    group_id: str,
    error_type: str,
) -> bool:
    """尝试获取通知冷却名额；Redis 读写异常时降级为照常通知。"""
    if redis is None:
        return True
    try:
        key = f"komari_chat:error_notify:{group_id}:{error_type}"
        acquired = await redis.redis.set(
            key,
            "1",
            nx=True,
            ex=_NOTIFY_COOLDOWN_SECONDS,
        )
    except Exception:
        logger.debug(
            "[KomariChat] 错误通知冷却读写失败，降级为照常通知: group={}",
            group_id,
            exc_info=True,
        )
        return True
    if not acquired:
        logger.debug(
            "[KomariChat] 错误通知处于冷却期，跳过: group={} error_type={}",
            group_id,
            error_type,
        )
        return False
    return True


async def notify_superusers_reply_failure(
    *,
    bot: Bot,
    redis: RedisManager | None,
    group_id: str,
    reason: str | None,
    stage: str,
    error_type: str,
    summary: str | None = None,
    request_trace_id: str | None = None,
) -> None:
    """向 SUPERUSERS 逐个私聊发送极简失败诊断卡。

    内容仅含 request_trace_id、群号、触发原因、失败阶段、异常类型与一行摘要，
    不含消息正文、prompt 或回复正文。同群 + 同异常类型 5 分钟 Redis 冷却去重；
    `error_notify_enabled=false` 时整体静默跳过。
    """
    config = get_memory_config()
    if not config.error_notify_enabled:
        return

    if not await _acquire_notify_cooldown(
        redis,
        group_id=group_id,
        error_type=error_type,
    ):
        return

    error_line = f"异常: {error_type}"
    if summary:
        error_line += f"（{summary}）"
    text = "\n".join(
        [
            "⚠️ 小鞠回复生成失败",
            f"trace: {request_trace_id or '-'}",
            f"群: {group_id}",
            f"触发: {reason or '-'}",
            f"阶段: {stage}",
            error_line,
        ]
    )

    superusers = get_driver().config.superusers
    for superuser in sorted(superusers):
        raw = str(superuser).strip()
        try:
            user_id = int(raw)
        except ValueError:
            logger.debug("[KomariChat] 跳过非数字 SUPERUSER 条目: {}", raw)
            continue
        try:
            await bot.send_private_msg(user_id=user_id, message=text)
        except Exception:
            logger.warning(
                "[KomariChat] SUPERUSER 失败通知发送失败: user={}",
                raw,
                exc_info=True,
            )
