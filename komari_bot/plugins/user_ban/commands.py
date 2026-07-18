"""用户封禁 SUPERUSER 管理命令。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent  # noqa: TC002
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from komari_bot.common.onebot_messages import plain_text_message

from .event_support import is_superuser_id
from .models import (
    BanRecord,
    BanScope,
    BanTargetScope,
    NotificationResult,
    UserBanStatus,
    normalize_ban_reason,
    normalize_qq_user_id,
    parse_ban_duration,
)
from .notifications import notify_ban_result, notify_unban_result
from .service import BanServiceUnavailableError, get_service

if TYPE_CHECKING:
    from datetime import datetime

LIST_PAGE_SIZE = 20
USAGE = (
    "用法：\n"
    ".ban chat|command|all <user_id> [permanent|Nm|Nh|Nd|Nw] [理由...]\n"
    ".unban chat|command|all <user_id>\n"
    ".ban status <user_id>\n"
    ".ban list [chat|command|all] [page]"
)

ban_matcher = on_command("ban", priority=1, block=True)
unban_matcher = on_command("unban", priority=1, block=True)


def _parse_target_scope(value: str) -> BanTargetScope | None:
    match value:
        case "chat":
            return "chat"
        case "command":
            return "command"
        case "all":
            return "all"
        case _:
            return None


def _parse_list_scope(value: str) -> tuple[bool, BanScope | None]:
    match value:
        case "chat":
            return True, "chat"
        case "command":
            return True, "command"
        case "all":
            return True, None
        case _:
            return False, None


def _scope_label(scope: BanTargetScope) -> str:
    match scope:
        case "chat":
            return "聊天回复"
        case "command":
            return "其他功能"
        case "all":
            return "聊天回复与其他功能"


def _format_active_scopes(status: UserBanStatus) -> str:
    scopes = status.active_scopes
    if scopes == {"chat", "command"}:
        return "all"
    if "chat" in scopes:
        return "chat"
    if "command" in scopes:
        return "command"
    return "无"


def _format_expiry(expires_at: datetime | None) -> str:
    if expires_at is None:
        return "永久"
    return expires_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_status_record(record: BanRecord) -> str:
    updated_at = record.updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"- {record.ban_scope}：期限 {_format_expiry(record.expires_at)}，"
        f"理由 {record.reason or '未填写'}，操作者 {record.operator_id}，"
        f"更新于 {updated_at}"
    )


def _compact_reason(reason: str | None, *, max_length: int = 40) -> str:
    if not reason:
        return "未填写"
    if len(reason) <= max_length:
        return reason
    return reason[:max_length] + "…"


def _format_list_record(record: BanRecord) -> str:
    return (
        f"{record.ban_scope}（{_format_expiry(record.expires_at)}；"
        f"理由：{_compact_reason(record.reason)}）"
    )


def _superuser_note(bot: Bot, user_id: str) -> str:
    if not is_superuser_id(bot, user_id):
        return ""
    return "\n⚠️ 目标当前为 SUPERUSER，运行时会绕过封禁。"


def _notification_suffix(result: NotificationResult, *, changed: bool) -> str:
    if not changed:
        return "\n私信：未发送（封禁状态未变化）"
    if result.sent:
        return "\n私信：已发送"
    return f"\n⚠️ 私信发送失败：{result.error or '未知错误'}"


def _parse_ban_args(
    tokens: list[str],
) -> tuple[BanTargetScope, str, datetime | None, str | None]:
    if len(tokens) < 2:
        msg = "封禁参数不足"
        raise ValueError(msg)
    target_scope = _parse_target_scope(tokens[0])
    if target_scope is None:
        msg = "作用域必须是 chat、command 或 all"
        raise ValueError(msg)
    user_id = normalize_qq_user_id(tokens[1])
    if user_id is None:
        msg = "user_id 必须是不带前导零的正整数"
        raise ValueError(msg)

    duration_text = tokens[2] if len(tokens) >= 3 else None
    expires_at = parse_ban_duration(duration_text)
    reason = normalize_ban_reason(" ".join(tokens[3:]) if len(tokens) >= 4 else None)
    return target_scope, user_id, expires_at, reason


async def _handle_status(bot: Bot, user_id_text: str) -> None:
    user_id = normalize_qq_user_id(user_id_text)
    if user_id is None:
        await ban_matcher.finish("❌ user_id 必须是不带前导零的正整数")

    try:
        status = await get_service().get_status(user_id)
    except BanServiceUnavailableError as error:
        await ban_matcher.finish(plain_text_message(f"❌ {error}"))

    if not status.records:
        await ban_matcher.finish(
            plain_text_message(f"提示：用户 {user_id} 当前未被封禁")
        )

    lines = [f"🚫 用户 {user_id} 的封禁状态：{_format_active_scopes(status)}"]
    lines.extend(_format_status_record(record) for record in status.records)
    superuser_note = _superuser_note(bot, user_id).lstrip("\n")
    if superuser_note:
        lines.append(superuser_note)
    await ban_matcher.finish(plain_text_message("\n".join(lines)))


def _parse_list_args(tokens: list[str]) -> tuple[BanScope | None, int] | None:
    if not tokens:
        return None, 1
    if len(tokens) > 2:
        return None

    valid_scope, parsed_scope = _parse_list_scope(tokens[0])
    if not valid_scope:
        if len(tokens) == 1 and tokens[0].isdigit():
            return None, int(tokens[0])
        return None

    page_text = tokens[1] if len(tokens) == 2 else "1"
    if not page_text.isdigit():
        return None
    return parsed_scope, int(page_text)


async def _handle_list(bot: Bot, tokens: list[str]) -> None:
    parsed = _parse_list_args(tokens)
    if parsed is None:
        await ban_matcher.finish("❌ 列表参数无效\n" + USAGE)
    scope, page = parsed
    if page < 1:
        await ban_matcher.finish("❌ 页码必须是正整数")

    try:
        result = await get_service().list_bans(
            scope=scope,
            limit=LIST_PAGE_SIZE,
            offset=(page - 1) * LIST_PAGE_SIZE,
        )
    except BanServiceUnavailableError as error:
        await ban_matcher.finish(plain_text_message(f"❌ {error}"))

    if not result.items:
        if result.total:
            total_pages = (result.total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
            await ban_matcher.finish(
                plain_text_message(f"提示：第 {page} 页不存在，当前共 {total_pages} 页")
            )
        await ban_matcher.finish("提示：当前没有符合条件的封禁记录")

    total_pages = (result.total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE
    scope_label = scope or "all"
    lines = [f"🚫 封禁列表（{scope_label}）第 {page}/{total_pages} 页"]
    for index, status in enumerate(result.items, start=result.offset + 1):
        note = "（SUPERUSER，实际绕过）" if is_superuser_id(bot, status.user_id) else ""
        record_text = " / ".join(
            _format_list_record(record) for record in status.records
        )
        lines.append(f"{index}. {status.user_id} — {record_text}{note}")
    await ban_matcher.finish(plain_text_message("\n".join(lines)))


@ban_matcher.handle()
async def handle_ban(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    """处理封禁、状态查询和列表命令。"""
    if not await SUPERUSER(bot, event):
        await ban_matcher.finish("❌ 仅限 SUPERUSER 使用")
    tokens = args.extract_plain_text().strip().split()
    if not tokens:
        await ban_matcher.finish(USAGE)

    if tokens[0] == "status":
        if len(tokens) != 2:
            await ban_matcher.finish("❌ status 参数无效\n" + USAGE)
        await _handle_status(bot, tokens[1])

    if tokens[0] == "list":
        await _handle_list(bot, tokens[1:])

    try:
        target_scope, user_id, expires_at, reason = _parse_ban_args(tokens)
        result = await get_service().ban_user(
            user_id=user_id,
            target_scope=target_scope,
            operator_id=event.get_user_id(),
            expires_at=expires_at,
            reason=reason,
        )
    except ValueError as error:
        await ban_matcher.finish(plain_text_message(f"❌ {error}\n{USAGE}"))
    except BanServiceUnavailableError as error:
        await ban_matcher.finish(plain_text_message(f"❌ {error}"))

    superuser_bypass = is_superuser_id(bot, user_id)
    notification = await notify_ban_result(
        bot,
        result,
        superuser_bypass=superuser_bypass,
    )
    match result.mutation_kind:
        case "created":
            action = "已封禁"
        case "updated":
            action = "封禁设置已更新"
        case _:
            action = "封禁设置未变化"
    await ban_matcher.finish(
        plain_text_message(
            f"✅ 用户 {user_id} 的{_scope_label(target_scope)}权限{action}。"
            f"\n当前状态：{_format_active_scopes(result.status)}"
            f"{_superuser_note(bot, user_id)}"
            f"{_notification_suffix(notification, changed=result.changed)}"
        )
    )


@unban_matcher.handle()
async def handle_unban(
    bot: Bot,
    event: MessageEvent,
    args: Message = CommandArg(),
) -> None:
    """处理解封命令。"""
    if not await SUPERUSER(bot, event):
        await unban_matcher.finish("❌ 仅限 SUPERUSER 使用")
    tokens = args.extract_plain_text().strip().split()
    if len(tokens) != 2:
        await unban_matcher.finish("❌ 解封参数无效\n" + USAGE)
    target_scope = _parse_target_scope(tokens[0])
    user_id = normalize_qq_user_id(tokens[1])
    if target_scope is None:
        await unban_matcher.finish("❌ 作用域必须是 chat、command 或 all")
    if user_id is None:
        await unban_matcher.finish("❌ user_id 必须是不带前导零的正整数")

    try:
        result = await get_service().unban_user(
            user_id=user_id,
            target_scope=target_scope,
        )
    except BanServiceUnavailableError as error:
        await unban_matcher.finish(plain_text_message(f"❌ {error}"))

    notification = await notify_unban_result(
        bot,
        result,
        superuser_bypass=is_superuser_id(bot, user_id),
    )
    action = "已解封" if result.changed else "原本未封禁"
    await unban_matcher.finish(
        plain_text_message(
            f"✅ 用户 {user_id} 的{_scope_label(target_scope)}权限{action}。"
            f"\n当前状态：{_format_active_scopes(result.status)}"
            f"{_superuser_note(bot, user_id)}"
            f"{_notification_suffix(notification, changed=result.changed)}"
        )
    )
