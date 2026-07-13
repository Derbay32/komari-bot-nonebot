""".debug 命令处理器。

每个处理器在任何 DB/manager/history/LLM 调用之前执行 `await SUPERUSER(bot, event)`；
SUPERUSER 不放进 matcher 的 permission=/rule=。
"""

from __future__ import annotations

import uuid

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.exception import FinishedException
from nonebot.params import CommandArg, Depends
from nonebot.permission import SUPERUSER

from komari_bot.plugins.character_binding import get_binding_manager
from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema as SummaryConfigSchema,
)
from komari_bot.plugins.group_history_summary.execution_service import (
    CapabilityNotSupportedError,
    SummaryBusyError,
    execute_group_summary,
)
from komari_bot.plugins.komari_chat import generate_debug_reply
from komari_bot.plugins.komari_chat.services.image_downloader import (
    extract_image_sources,
)
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext
from komari_bot.plugins.llm_provider.diagnostic import LLMDiagnosticCollector
from komari_bot.plugins.user_data import (
    get_user_favorability,
    set_user_favorability,
)

from .reporting import build_and_send_diagnostic_report

FAVOR_MIN = 0
FAVOR_MAX = 400

HELP_TEXT = """🔧 Komari Debug 子命令：

.debug favor get <用户ID> — 查询好感度
.debug favor set <用户ID> <0-400> — 设置好感度
.debug bind set <用户ID> <角色名> — 设置角色绑定
.debug bind del <用户ID> — 删除角色绑定
.debug bind list — 查看全部绑定
.debug reply <测试文本> — 群聊干跑回复（仅群聊）
.debug summary <总结要求> — 群聊诊断总结（仅群聊）"""


def _get_arg_text(args: Message = CommandArg()) -> str:
    return args.extract_plain_text().strip()


def _parse_user_id(raw: str) -> str | None:
    """解析用户 ID，必须为正整数。"""
    try:
        uid = int(raw.strip())
    except (ValueError, TypeError):
        return None
    if uid <= 0:
        return None
    return str(uid)


def _parse_favor_value(raw: str) -> int | None:
    """解析好感度值，必须为 0-400 的整数。"""
    try:
        val = int(raw.strip())
    except (ValueError, TypeError):
        return None
    if val < FAVOR_MIN or val > FAVOR_MAX:
        return None
    return val


def _gather_image_urls(event: GroupMessageEvent) -> list[str]:
    """从事件消息中提取图片 URL 列表。"""
    try:
        msg = getattr(event, "message", None)
        if msg is None:
            return []
        urls, _count = extract_image_sources(msg)
    except Exception:
        logger.debug("[KomariDebug] 提取图片 URL 失败", exc_info=True)
        return []
    else:
        return urls


def _build_reply_context_from_event(event: GroupMessageEvent) -> ReplyContext | None:
    """从事件中构造引用消息上下文（当 event.reply 存在时）。"""
    reply = getattr(event, "reply", None)
    if reply is None:
        return None

    try:
        reply_msg = reply.message
        text = ""
        image_sources: list[str] = []
        image_count = 0
        has_visible_image = False

        if isinstance(reply_msg, str):
            text = reply_msg.strip()
        elif hasattr(reply_msg, "extract_plain_text"):
            text = str(reply_msg.extract_plain_text()).strip()
            try:
                imgs, cnt = extract_image_sources(reply_msg)
                image_sources = imgs
                image_count = cnt
                has_visible_image = bool(imgs)
            except Exception:
                pass
        elif isinstance(reply_msg, list):
            parts: list[str] = []
            for seg in reply_msg:
                if isinstance(seg, dict) and seg.get("type") == "text":
                    parts.append(str(seg.get("data", {}).get("text", "")))
                elif isinstance(seg, dict) and seg.get("type") == "image":
                    url = str(seg.get("data", {}).get("url", ""))
                    if url:
                        image_sources.append(url)
                        image_count += 1
                        has_visible_image = True
            text = "".join(parts).strip()

        user_id = None
        user_nickname = None
        sender = getattr(reply, "sender", None)
        if sender is not None:
            uid = getattr(sender, "user_id", None)
            if uid is not None:
                user_id = str(uid)
            card = getattr(sender, "card", None)
            nick = getattr(sender, "nickname", None)
            user_nickname = (str(card or nick or "")).strip() or user_id

        message_id = str(getattr(reply, "message_id", "0"))

        bot_self_id = None
        if hasattr(event, "self_id"):
            bot_self_id = str(event.self_id)

        return ReplyContext(
            source_side="assistant" if (bot_self_id is not None and user_id == bot_self_id) else "user",
            message_id=message_id,
            user_id=user_id,
            user_nickname=user_nickname,
            text=text,
            image_sources=tuple(image_sources),
            image_count=image_count,
            has_visible_image=has_visible_image,
        )
    except Exception:
        logger.debug("[KomariDebug] 构造引用上下文失败", exc_info=True)
        return None


# ─── 根命令 .debug ──────────────────────────────────────────────

debug_root = on_command("debug", priority=5, block=True)


@debug_root.handle()
async def handle_debug_root(bot: Bot, event: MessageEvent) -> None:
    if not await SUPERUSER(bot, event):
        await debug_root.finish("❌ 仅限 SUPERUSER 使用")
    await debug_root.finish(HELP_TEXT)


# ─── .debug favor get ──────────────────────────────────────────

debug_favor_get = on_command(("debug", "favor", "get"), priority=2, block=True)


@debug_favor_get.handle()
async def handle_favor_get(
    bot: Bot,
    event: MessageEvent,
    arg_text: str = Depends(_get_arg_text),
) -> None:
    if not await SUPERUSER(bot, event):
        await debug_favor_get.finish("❌ 仅限 SUPERUSER 使用")

    user_id = _parse_user_id(arg_text)
    if user_id is None:
        await debug_favor_get.finish("❌ 请提供有效的用户 ID（正整数）\n用法: .debug favor get <用户ID>")

    try:
        favor = await get_user_favorability(user_id)
    except Exception as e:
        logger.exception("[KomariDebug] favor get 失败")
        await debug_favor_get.finish(f"❌ 查询失败: {e}")

    await debug_favor_get.finish(
        f"📊 用户 {user_id} 好感度:\n"
        f"  数值: {favor.favorability}\n"
        f"  阶段: {favor.stage_name}（{favor.stage_index}/4）\n"
        f"  更新时间: {favor.updated_at}"
    )


# ─── .debug favor set ──────────────────────────────────────────

debug_favor_set = on_command(("debug", "favor", "set"), priority=2, block=True)


@debug_favor_set.handle()
async def handle_favor_set(
    bot: Bot,
    event: MessageEvent,
    arg_text: str = Depends(_get_arg_text),
) -> None:
    if not await SUPERUSER(bot, event):
        await debug_favor_set.finish("❌ 仅限 SUPERUSER 使用")

    parts = arg_text.split(maxsplit=1)
    if len(parts) < 2:
        await debug_favor_set.finish(
            "❌ 参数不足\n用法: .debug favor set <用户ID> <0-400>"
        )

    user_id = _parse_user_id(parts[0])
    if user_id is None:
        await debug_favor_set.finish(
            "❌ 用户 ID 必须为正整数\n用法: .debug favor set <用户ID> <0-400>"
        )

    value = _parse_favor_value(parts[1])
    if value is None:
        await debug_favor_set.finish(
            f"❌ 好感度值必须为 {FAVOR_MIN}-{FAVOR_MAX} 的整数\n用法: .debug favor set <用户ID> <0-{FAVOR_MAX}>"
        )

    try:
        result = await set_user_favorability(user_id, value)
    except Exception as e:
        logger.exception("[KomariDebug] favor set 失败")
        await debug_favor_set.finish(f"❌ 设置失败: {e}")

    await debug_favor_set.finish(
        f"✅ 用户 {user_id} 好感度已设置:\n"
        f"  before: {result.before}\n"
        f"  after:  {result.after}\n"
        f"  阶段:    {result.stage_name}（{result.stage_index}/4）\n"
        f"  更新时间: {result.updated_at}"
    )


# ─── .debug bind set ───────────────────────────────────────────

debug_bind_set = on_command(("debug", "bind", "set"), priority=2, block=True)


@debug_bind_set.handle()
async def handle_debug_bind_set(
    bot: Bot,
    event: MessageEvent,
    arg_text: str = Depends(_get_arg_text),
) -> None:
    if not await SUPERUSER(bot, event):
        await debug_bind_set.finish("❌ 仅限 SUPERUSER 使用")

    parts = arg_text.split(maxsplit=1)
    if len(parts) < 2:
        await debug_bind_set.finish(
            "❌ 参数不足\n用法: .debug bind set <用户ID> <角色名>"
        )

    user_id = _parse_user_id(parts[0])
    if user_id is None:
        await debug_bind_set.finish(
            "❌ 用户 ID 必须为正整数\n用法: .debug bind set <用户ID> <角色名>"
        )

    character_name = parts[1].strip()
    if not character_name:
        await debug_bind_set.finish(
            "❌ 角色名不能为空\n用法: .debug bind set <用户ID> <角色名>"
        )

    try:
        manager = get_binding_manager()
        await manager.set_character_name(user_id, character_name)
    except Exception as e:
        logger.exception("[KomariDebug] bind set 失败")
        await debug_bind_set.finish(f"❌ 设置绑定失败: {e}")

    await debug_bind_set.finish(f"✅ 已为用户 {user_id} 设置角色绑定: {character_name}")


# ─── .debug bind del ───────────────────────────────────────────

debug_bind_del = on_command(("debug", "bind", "del"), priority=2, block=True)


@debug_bind_del.handle()
async def handle_debug_bind_del(
    bot: Bot,
    event: MessageEvent,
    arg_text: str = Depends(_get_arg_text),
) -> None:
    if not await SUPERUSER(bot, event):
        await debug_bind_del.finish("❌ 仅限 SUPERUSER 使用")

    user_id = _parse_user_id(arg_text)
    if user_id is None:
        await debug_bind_del.finish(
            "❌ 请提供有效的用户 ID（正整数）\n用法: .debug bind del <用户ID>"
        )

    try:
        manager = get_binding_manager()
        success = await manager.remove_character_name(user_id)
    except Exception as e:
        logger.exception("[KomariDebug] bind del 失败")
        await debug_bind_del.finish(f"❌ 删除绑定失败: {e}")

    if success:
        await debug_bind_del.finish(f"✅ 已删除用户 {user_id} 的角色绑定")
    else:
        await debug_bind_del.finish(f"⚠️ 用户 {user_id} 没有角色绑定")


# ─── .debug bind list ──────────────────────────────────────────

debug_bind_list = on_command(("debug", "bind", "list"), priority=2, block=True)


@debug_bind_list.handle()
async def handle_debug_bind_list(
    bot: Bot,
    event: MessageEvent,
    arg_text: str = Depends(_get_arg_text),
) -> None:
    if not await SUPERUSER(bot, event):
        await debug_bind_list.finish("❌ 仅限 SUPERUSER 使用")

    if arg_text:
        await debug_bind_list.finish("❌ 参数过多\n用法: .debug bind list")

    try:
        manager = get_binding_manager()
        bindings = manager.list_bindings()
    except Exception as e:
        logger.exception("[KomariDebug] bind list 失败")
        await debug_bind_list.finish(f"❌ 查询绑定列表失败: {e}")

    if not bindings:
        await debug_bind_list.finish("📋 当前没有任何角色绑定")

    lines = ["📋 全部角色绑定:"]
    for uid, name in sorted(bindings.items(), key=lambda item: item[0]):
        lines.append(f"  {uid}: {name}")

    await debug_bind_list.finish("\n".join(lines))


# ─── .debug reply ──────────────────────────────────────────────

debug_reply = on_command(("debug", "reply"), priority=2, block=True)


@debug_reply.handle()
async def handle_debug_reply(
    bot: Bot,
    event: MessageEvent,
    arg_text: str = Depends(_get_arg_text),
) -> None:
    if not await SUPERUSER(bot, event):
        await debug_reply.finish("❌ 仅限 SUPERUSER 使用")

    if not isinstance(event, GroupMessageEvent):
        await debug_reply.finish("❌ .debug reply 仅支持群聊")

    if not arg_text:
        await debug_reply.finish(
            "❌ 请提供测试文本\n用法: .debug reply <测试文本>"
        )

    group_id = str(event.group_id)
    user_id = str(event.user_id)
    user_nickname = (
        str(event.sender.card or event.sender.nickname).strip()
        if (event.sender.card or event.sender.nickname)
        else user_id
    )

    image_urls = _gather_image_urls(event)
    reply_context = _build_reply_context_from_event(event)

    request_trace_id = f"debug-reply-{uuid.uuid4().hex[:12]}"
    collector = LLMDiagnosticCollector(request_id=request_trace_id)

    try:
        result = await generate_debug_reply(
            group_id=group_id,
            user_id=user_id,
            user_nickname=user_nickname,
            content=arg_text,
            bot=bot,
            image_urls=image_urls if image_urls else None,
            reply_context=reply_context,
            collector=collector,
        )
    except FinishedException:
        raise
    except Exception as e:
        logger.exception("[KomariDebug] debug reply 执行失败")
        collector.add_error(
            phase="debug_reply",
            error_type=type(e).__name__,
            message=str(e),
        )
        await build_and_send_diagnostic_report(
            bot=bot,
            group_id=int(event.group_id),
            collector=collector,
            result_type="reply",
            succeeded=False,
            error=str(e),
            final_result_info={
                "user_id": user_id,
                "content": arg_text[:200],
            },
        )
        return

    reply_text = result.reply or "(无回复内容)"
    favor_delta_str = (
        f"好感度变化: {result.favorability_delta:+d}"
        if result.favorability_delta is not None
        else "好感度变化: 无"
    )
    if result.favorability_reason:
        favor_delta_str += f"（{result.favorability_reason}）"

    await build_and_send_diagnostic_report(
        bot=bot,
        group_id=int(event.group_id),
        collector=result.collector,
        result_type="reply",
        succeeded=True,
        final_result_info={
            "reply_text": reply_text,
            "favorability_delta": favor_delta_str,
            "reply_to_message_id": result.reply_to_message_id,
        },
        extra_info={
            "user_id": user_id,
            "content": arg_text[:200],
        },
    )


# ─── .debug summary ────────────────────────────────────────────

debug_summary = on_command(("debug", "summary"), priority=2, block=True)

# 复用正常的配置管理器
from komari_bot.plugins.config_manager import get_config_manager

_summary_config_mgr = get_config_manager("group_history_summary", SummaryConfigSchema)


def _cast_summary_config(config: object) -> SummaryConfigSchema:
    """类型窄化：校验并返回正确的 SummaryConfigSchema 实例。"""
    if isinstance(config, SummaryConfigSchema):
        return config
    raise TypeError("group_history_summary 配置类型不匹配")  # noqa: TRY003


@debug_summary.handle()
async def handle_debug_summary(
    bot: Bot,
    event: MessageEvent,
    arg_text: str = Depends(_get_arg_text),
) -> None:
    if not await SUPERUSER(bot, event):
        await debug_summary.finish("❌ 仅限 SUPERUSER 使用")

    if not isinstance(event, GroupMessageEvent):
        await debug_summary.finish("❌ .debug summary 仅支持群聊")

    if not arg_text:
        await debug_summary.finish(
            "❌ 请提供总结要求\n用法: .debug summary <总结要求>"
        )

    request_trace_id = f"debug-summary-{uuid.uuid4().hex[:12]}"
    collector = LLMDiagnosticCollector(request_id=request_trace_id)
    group_id = str(event.group_id)

    try:
        config = _summary_config_mgr.get()
        summary_config = _cast_summary_config(config)
    except Exception as e:
        collector.add_error(
            phase="debug_summary_config",
            error_type=type(e).__name__,
            message=str(e),
        )
        await build_and_send_diagnostic_report(
            bot=bot,
            group_id=int(event.group_id),
            collector=collector,
            result_type="summary",
            succeeded=False,
            error=str(e),
            extra_info={"user_id": str(event.user_id)},
        )
        return

    try:
        result = await execute_group_summary(
            bot=bot,
            group_id=group_id,
            bot_self_id=str(bot.self_id),
            user_request=arg_text,
            config=summary_config,
            collector=collector,
        )
    except SummaryBusyError as e:
        collector.add_error(
            phase="debug_summary",
            error_type="SummaryBusyError",
            message=str(e),
        )
        await build_and_send_diagnostic_report(
            bot=bot,
            group_id=int(event.group_id),
            collector=collector,
            result_type="summary",
            succeeded=False,
            error=str(e),
            extra_info={"user_id": str(event.user_id), "request": arg_text[:200]},
        )
        return
    except CapabilityNotSupportedError:
        collector.add_error(
            phase="debug_summary",
            error_type="CapabilityNotSupportedError",
            message="当前 OneBot 实现不支持获取群聊记录",
        )
        await build_and_send_diagnostic_report(
            bot=bot,
            group_id=int(event.group_id),
            collector=collector,
            result_type="summary",
            succeeded=False,
            error="当前 OneBot 实现不支持获取群聊记录",
            extra_info={"user_id": str(event.user_id), "request": arg_text[:200]},
        )
        return
    except FinishedException:
        raise
    except Exception as e:
        logger.exception("[KomariDebug] debug summary 执行失败")
        collector.add_error(
            phase="debug_summary",
            error_type=type(e).__name__,
            message=str(e),
        )
        await build_and_send_diagnostic_report(
            bot=bot,
            group_id=int(event.group_id),
            collector=collector,
            result_type="summary",
            succeeded=False,
            error=str(e),
            extra_info={"user_id": str(event.user_id), "request": arg_text[:200]},
        )
        return

    if result.image_base64:
        try:
            await bot.send(
                event,
                MessageSegment.image(file=f"base64://{result.image_base64}"),
            )
        except Exception as e:
            logger.exception("[KomariDebug] 总结图片发送失败")
            collector.add_error(
                phase="debug_summary_image_send",
                error_type=type(e).__name__,
                message=str(e),
            )

    await build_and_send_diagnostic_report(
        bot=bot,
        group_id=int(event.group_id),
        collector=collector,
        result_type="summary",
        succeeded=True,
        extra_info={
            "user_id": str(event.user_id),
            "request": arg_text[:200],
            "filtered_message_count": str(result.filtered_message_count),
            "filter_label": result.filter_label,
            "time_range": result.time_range,
        },
    )
