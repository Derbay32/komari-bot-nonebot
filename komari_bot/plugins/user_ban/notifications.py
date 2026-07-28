"""用户封禁生命周期私信通知。"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from nonebot import get_bots, logger

from komari_bot.common.management_audit import hash_management_target
from komari_bot.common.onebot_messages import plain_text_message

from .models import BanMutationResult, BanRecord, NotificationResult

if TYPE_CHECKING:
    from nonebot.adapters import Bot


_SCOPE_LABELS = {
    "chat": "聊天回复",
    "command": "其他功能",
}


def get_first_available_bot() -> Bot | None:
    """获取首个在线 Bot；没有在线连接时返回空。"""
    bots = get_bots()
    if not bots:
        return None
    return cast("Bot", next(iter(bots.values())))


def _format_record(record: BanRecord, *, include_expiry: bool) -> str:
    scope_label = _SCOPE_LABELS[record.ban_scope]
    details: list[str] = []
    if include_expiry:
        expiry = (
            "永久"
            if record.expires_at is None
            else record.expires_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        )
        details.append(f"期限：{expiry}")
    details.append(f"理由：{record.reason or '未填写'}")
    return f"- {scope_label}（{'；'.join(details)}）"


def build_ban_message(
    result: BanMutationResult,
    *,
    superuser_bypass: bool,
) -> str:
    """构建新增或覆盖封禁通知。"""
    action = "封禁设置已被管理员更新" if result.mutation_kind == "updated" else "已被管理员封禁"
    lines = ["【Komari Bot 封禁通知】", f"你的机器人权限{action}："]
    lines.extend(
        _format_record(record, include_expiry=True)
        for record in result.affected_records
    )
    if superuser_bypass:
        lines.append("提示：你当前被配置为 SUPERUSER，运行时会绕过上述封禁。")
    lines.append("如有疑问，请联系机器人管理员。")
    return "\n".join(lines)


def build_unban_message(
    records: tuple[BanRecord, ...],
    *,
    natural: bool,
    superuser_bypass: bool,
) -> str:
    """构建手动或自然解封通知。"""
    title = "封禁已自然到期" if natural else "封禁已被管理员解除"
    lines = ["【Komari Bot 解封通知】", f"你的下列机器人权限{title}："]
    lines.extend(
        _format_record(record, include_expiry=False) for record in records
    )
    if superuser_bypass:
        lines.append("提示：你当前被配置为 SUPERUSER，运行时原本就会绕过封禁。")
    return "\n".join(lines)


async def send_private_notification(
    bot: Bot | None,
    *,
    user_id: str,
    message: str,
) -> NotificationResult:
    """尝试发送一次普通文本私信，失败时不抛出异常。"""
    if bot is None:
        error = "Bot 不在线，无法发送私信"
        logger.warning(
            "[UserBan] 私信通知待重试: target_hash={} error_code=bot_offline",
            hash_management_target(user_id),
        )
        return NotificationResult(attempted=False, sent=False, error=error)

    try:
        await bot.call_api(
            "send_private_msg",
            user_id=int(user_id),
            message=plain_text_message(message),
        )
    except Exception as error:
        logger.warning(
            "[UserBan] 私信通知失败: target_hash={} error_type={}",
            hash_management_target(user_id),
            type(error).__name__,
        )
        return NotificationResult(
            attempted=True,
            sent=False,
            error="平台私信发送失败",
        )
    logger.info(
        "[UserBan] 封禁生命周期私信已发送: target_hash={}",
        hash_management_target(user_id),
    )
    return NotificationResult(attempted=True, sent=True)


async def notify_ban_result(
    bot: Bot | None,
    result: BanMutationResult,
    *,
    superuser_bypass: bool,
) -> NotificationResult:
    """在封禁新增或覆盖后尝试发送私信。"""
    if not result.changed or not result.affected_records:
        return NotificationResult(attempted=False, sent=False)
    return await send_private_notification(
        bot,
        user_id=result.status.user_id,
        message=build_ban_message(result, superuser_bypass=superuser_bypass),
    )


async def notify_unban_result(
    bot: Bot | None,
    result: BanMutationResult,
    *,
    superuser_bypass: bool,
) -> NotificationResult:
    """在手动解封后尝试发送私信。"""
    if not result.changed or not result.affected_records:
        return NotificationResult(attempted=False, sent=False)
    return await send_private_notification(
        bot,
        user_id=result.status.user_id,
        message=build_unban_message(
            result.affected_records,
            natural=False,
            superuser_bypass=superuser_bypass,
        ),
    )


async def notify_expired_records(
    bot: Bot | None,
    *,
    user_id: str,
    records: tuple[BanRecord, ...],
    superuser_bypass: bool,
) -> NotificationResult:
    """在自然解封后尝试发送私信。"""
    if not records:
        return NotificationResult(attempted=False, sent=False)
    return await send_private_notification(
        bot,
        user_id=user_id,
        message=build_unban_message(
            records,
            natural=True,
            superuser_bypass=superuser_bypass,
        ),
    )
