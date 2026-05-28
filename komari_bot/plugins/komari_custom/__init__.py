"""群成员知识库提案插件。"""

from __future__ import annotations

from typing import Any, cast

from nonebot import get_driver, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message  # noqa: TC002
from nonebot.exception import FinishedException
from nonebot.params import Command, CommandArg
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.database_config import get_shared_database_config

from .config_schema import DynamicConfigSchema
from .models import Proposal, SessionData  # noqa: TC001
from .proposal_repository import ProposalRepository
from .session_manager import CustomSessionManager, split_replace_args
from .vote_handler import (
    KnowledgePlugin,
    approve_if_ready,
    fetch_and_update_votes,
    setup_vote_handler,
)

__plugin_meta__ = PluginMetadata(
    name="komari-custom",
    description="群成员知识库提案与投票采纳插件",
    usage="""
此命令用于发起追加写入知识库公投。
.custom new [标题] 发起知识库提案(可不输入标题，后续引导流程会要求再次输入)
直接回复引导消息 向当前提案的标题/内容追加内容，默认换行追加，标题不支持换行
.custom confirm 确认修改并推进标题、正文、最终发布三阶段
.custom replace <旧文本> <新文本> 替换当前字段，或只传一个参数全量替换
.custom undo 撤销上次编辑
.custom del [文本] 删除当前字段文本，不传则清空
.custom cancel 取消编辑
.custom status 查看当前编辑状态
.custom list [页码] 查看本群提案
.custom show <序号|标题关键词> 查看提案详情
""",
    config=DynamicConfigSchema,
)

config_manager_plugin = require("config_manager")
permission_manager_plugin = require("permission_manager")
knowledge_plugin = require("komari_knowledge")
character_binding = require("character_binding")

config_manager = config_manager_plugin.get_config_manager(
    "komari_custom", DynamicConfigSchema
)
repository = ProposalRepository()
custom_sessions = CustomSessionManager(config_manager)
setup_vote_handler(
    repository, config_manager, cast("KnowledgePlugin", knowledge_plugin)
)

driver = get_driver()

custom = on_command("custom", priority=10, block=True)
custom_action = on_command(
    ("custom", "new"),
    aliases={
        ("custom", "replace"),
        ("custom", "undo"),
        ("custom", "del"),
        ("custom", "confirm"),
        ("custom", "cancel"),
        ("custom", "list"),
        ("custom", "show"),
        ("custom", "status"),
    },
    priority=7,
    block=True,
)


@driver.on_startup
async def on_startup() -> None:
    """启用时预初始化数据库；未启用则保持零影响。"""
    config = config_manager.get()
    if not config.plugin_enable:
        logger.info("[KomariCustom] 插件未启用，跳过初始化")
        return
    db_config = get_shared_database_config()
    if not db_config.pg_user or not db_config.pg_password:
        logger.warning("[KomariCustom] PostgreSQL 未配置，跳过初始化")
        return
    try:
        await repository.initialize()
        await repository.cleanup_expired()
        logger.info("[KomariCustom] 插件启动完成")
    except Exception:
        logger.exception("[KomariCustom] 初始化失败")


@driver.on_shutdown
async def on_shutdown() -> None:
    """释放数据库与 Redis 资源。"""
    await repository.close()
    await custom_sessions.close()


async def _is_custom_prompt_reply(event: GroupMessageEvent) -> bool:
    reply = event.reply
    if reply is None:
        return False
    return await custom_sessions.is_prompt_reply(
        int(event.group_id),
        event.get_user_id(),
        int(reply.message_id),
    )


reply_append = on_message(rule=_is_custom_prompt_reply, priority=5, block=True)


@custom.handle()
async def handle_custom_help(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
) -> None:
    """显示 .custom 帮助。"""
    can_use, reason = await permission_manager_plugin.check_runtime_permission(
        bot, event, config_manager.get()
    )
    if not can_use:
        await custom.finish(f"❌ {reason}")
    arg_text = args.extract_plain_text().strip()
    if arg_text:
        await custom.finish(
            "❌ 未知子命令，请使用 .custom new/list/show/status/confirm"
        )
    await custom.finish(__plugin_meta__.usage.strip())


@reply_append.handle()
async def handle_reply_append(bot: Bot, event: GroupMessageEvent) -> None:
    """处理对 bot 引导消息的回复追加。"""
    can_use, reason = await permission_manager_plugin.check_runtime_permission(
        bot, event, config_manager.get()
    )
    if not can_use:
        await reply_append.finish(f"❌ {reason}")

    text = event.get_plaintext().strip()
    if not text:
        await reply_append.finish("❌ 追加内容不能为空")
    try:
        session = await custom_sessions.append_text(
            int(event.group_id), event.get_user_id(), text
        )
        response = _format_edit_state(session)
        sent = await reply_append.send(response)
        await custom_sessions.remember_prompt_message(
            int(event.group_id), event.get_user_id(), _extract_message_id(sent)
        )
        await reply_append.finish()
    except Exception as e:
        if not isinstance(e, FinishedException):
            logger.exception("[KomariCustom] 追加编辑内容失败")
            await reply_append.finish(f"❌ {e}")


@custom_action.handle()
async def handle_custom_action(
    bot: Bot,
    event: GroupMessageEvent,
    cmd: tuple[str, ...] = Command(),
    args: Message = CommandArg(),
) -> None:
    """分发 .custom 子命令。"""
    _, action = cmd
    can_use, reason = await permission_manager_plugin.check_runtime_permission(
        bot, event, config_manager.get()
    )
    if not can_use:
        await custom_action.finish(f"❌ {reason}")

    try:
        await repository.initialize()
        await repository.cleanup_expired()
        text = args.extract_plain_text().strip()
        user_id = event.get_user_id()
        group_id = int(event.group_id)

        match action:
            case "new":
                await _handle_new(group_id, user_id, text)
            case "replace":
                await _handle_replace(group_id, user_id, text)
            case "undo":
                await _handle_undo(group_id, user_id)
            case "del":
                await _handle_delete(group_id, user_id, text)
            case "confirm":
                await _handle_confirm(bot, event, group_id, user_id)
            case "cancel":
                await custom_sessions.delete_session(group_id, user_id)
                await custom_action.finish("已取消当前提案编辑会话")
            case "list":
                await _handle_list(bot, group_id, text)
            case "show":
                await _handle_show(bot, group_id, text)
            case "status":
                await _handle_status(group_id, user_id)
            case _:
                await custom_action.finish("❌ 未知子命令")
    except Exception as e:
        if not isinstance(e, FinishedException):
            logger.exception("[KomariCustom] 处理 custom 子命令失败")
            await custom_action.finish(f"❌ 处理请求失败：{e}")


async def _handle_new(group_id: int, user_id: str, title: str) -> None:
    existing = await custom_sessions.get_session(group_id, user_id)
    if existing is not None:
        await custom_action.finish(
            "❌ 你已经有正在编辑的提案啦，先 confirm/cancel 处理掉吧"
        )

    config = config_manager.get()
    active_count = await repository.count_active_by_user(group_id, int(user_id))
    if active_count >= config.max_proposals_per_user:
        await custom_action.finish(
            f"❌ 你同时进行中的提案已达到上限 {config.max_proposals_per_user} 个"
        )

    await custom_sessions.create_session(group_id, user_id, title=title)
    if title:
        response = (
            f"已记录标题：{title}\n"
            "继续回复这条消息补充标题，或使用 .custom confirm 进入正文阶段。"
        )
    else:
        response = "已开始新的知识库提案，请回复这条消息输入标题。"
    sent = await custom_action.send(response)
    await custom_sessions.remember_prompt_message(
        group_id, user_id, _extract_message_id(sent)
    )
    await custom_action.finish()


async def _handle_replace(group_id: int, user_id: str, text: str) -> None:
    old, new = split_replace_args(text)
    if not new:
        await custom_action.finish("❌ 请输入替换后的文本")
    session = await custom_sessions.replace_text(group_id, user_id, old, new)
    await custom_action.finish(_format_edit_state(session))


async def _handle_undo(group_id: int, user_id: str) -> None:
    session = await custom_sessions.undo(group_id, user_id)
    if session is None:
        await custom_action.finish("❌ 没有可撤销的编辑操作")
    await custom_action.finish(_format_edit_state(session))


async def _handle_delete(group_id: int, user_id: str, text: str) -> None:
    session = await custom_sessions.delete_text(group_id, user_id, text)
    await custom_action.finish(_format_edit_state(session))


async def _handle_confirm(
    bot: Bot,
    event: GroupMessageEvent,
    group_id: int,
    user_id: str,
) -> None:
    session = await custom_sessions.get_session(group_id, user_id)
    if session is None:
        await custom_action.finish("❌ 没有正在编辑的提案，请先使用 .custom new")

    match session.phase:
        case "title":
            if not session.title:
                await custom_action.finish("❌ 标题不能为空，请回复引导消息输入标题")
            session = await custom_sessions.set_phase(group_id, user_id, "content")
            sent = await custom_action.send(
                f"标题已确认：{session.title}\n请回复这条消息输入提案正文。"
            )
            await custom_sessions.remember_prompt_message(
                group_id, user_id, _extract_message_id(sent)
            )
            await custom_action.finish()
        case "content":
            if not session.content:
                await custom_action.finish("❌ 正文不能为空，请回复引导消息输入内容")
            session = await custom_sessions.set_phase(group_id, user_id, "review")
            await custom_action.finish(_format_review(session))
        case "review":
            await _commit_proposal(bot, event, group_id, user_id, session)


async def _commit_proposal(
    bot: Bot,
    event: GroupMessageEvent,
    group_id: int,
    user_id: str,
    session: SessionData,
) -> None:
    config = config_manager.get()
    proposer_name = _resolve_proposer_name(event)
    proposal = await repository.create_proposal(
        group_id=group_id,
        proposer_id=int(user_id),
        proposer_name=proposer_name,
        title=session.title,
        content=session.content,
        required_votes=config.required_votes,
        expire_hours=config.proposal_expire_hours,
    )
    vote_message = _format_vote_message(proposal)
    sent = await bot.call_api("send_group_msg", group_id=group_id, message=vote_message)
    message_id = _extract_message_id(sent)
    if message_id is not None:
        await repository.set_vote_message_id(proposal.id, message_id)
        try:
            await bot.call_api(
                "set_msg_emoji_like",
                message_id=message_id,
                emoji_id=str(config.vote_emoji_id),
            )
        except Exception as e:
            logger.debug("[KomariCustom] 给投票消息添加默认表情失败: {}", e)
    await custom_sessions.delete_session(group_id, user_id)
    await custom_action.finish(
        f"提案 #{proposal.id} 已发布，达到 {proposal.required_votes} 票后会自动加入知识库。"
    )


async def _handle_list(bot: Bot, group_id: int, text: str) -> None:
    config = config_manager.get()
    page = int(text) if text.isdigit() else 1
    if page < 1:
        await custom_action.finish("❌ 页码必须大于 0")
    limit = config.list_chunk_size
    proposals, total = await repository.list_proposals(
        group_id=group_id,
        limit=limit,
        offset=(page - 1) * limit,
    )
    if total == 0:
        await custom_action.finish("本群暂无有效提案")
    total_pages = max(1, (total + limit - 1) // limit)
    if page > total_pages:
        await custom_action.finish(f"❌ 只有 {total_pages} 页哦")

    for proposal in proposals:
        if proposal.vote_message_id is not None and proposal.status == "voting":
            await fetch_and_update_votes(
                bot,
                message_id=proposal.vote_message_id,
                proposal_id=proposal.id,
            )
            await approve_if_ready(bot, proposal.id)
    proposals, total = await repository.list_proposals(
        group_id=group_id,
        limit=limit,
        offset=(page - 1) * limit,
    )

    start = (page - 1) * limit
    lines = [f"📋 提案列表（第 {page}/{total_pages} 页，共 {total} 条）"]
    for index, proposal in enumerate(proposals, start=start + 1):
        status_icon = "✅" if proposal.status == "approved" else "🗳"
        lines.append(
            f"{index}. [{status_icon}] #{proposal.id} {proposal.title} "
            f"({proposal.vote_count}/{proposal.required_votes})"
        )
    lines.append("\n输入 .custom show <序号|标题关键词> 查看详情")
    await custom_action.finish("\n".join(lines))


async def _handle_show(bot: Bot, group_id: int, selector: str) -> None:
    if not selector:
        await custom_action.finish("❌ 请输入提案序号或标题关键词")
    config = config_manager.get()
    proposal = await repository.find_in_group_by_index_or_keyword(
        group_id=group_id,
        selector=selector,
        limit_for_index=config.list_chunk_size,
    )
    if proposal is None:
        await custom_action.finish("❌ 没找到对应提案")
    if proposal.vote_message_id is not None and proposal.status == "voting":
        updated = await fetch_and_update_votes(
            bot,
            message_id=proposal.vote_message_id,
            proposal_id=proposal.id,
        )
        if updated is not None:
            proposal = updated
        await approve_if_ready(bot, proposal.id)
        proposal = await repository.get_by_id(proposal.id, group_id) or proposal
    await custom_action.finish(_format_proposal_detail(proposal))


async def _handle_status(group_id: int, user_id: str) -> None:
    session = await custom_sessions.get_session(group_id, user_id)
    if session is None:
        await custom_action.finish("当前没有正在编辑的提案")
    await custom_action.finish(_format_edit_state(session))


def _format_edit_state(session: SessionData) -> str:
    field_name = "标题" if session.phase == "title" else "正文"
    current = session.title if session.phase == "title" else session.content
    return (
        f"当前阶段：{_phase_name(session.phase)}\n"
        f"当前{field_name}：\n{current or '（空）'}\n\n"
        "可继续回复引导消息追加，或使用 .custom replace / undo / del / confirm。"
    )


def _format_review(session: SessionData) -> str:
    return (
        "请确认提案内容：\n"
        f"标题：{session.title}\n"
        f"正文：\n{session.content}\n\n"
        "确认无误后再次发送 .custom confirm 发布投票；需要修改可用 replace/undo/del。"
    )


def _format_vote_message(proposal: Proposal) -> str:
    expire_text = (
        proposal.expired_at.astimezone().strftime("%Y-%m-%d %H:%M")
        if proposal.expired_at is not None
        else "未知"
    )
    return (
        f"🗳 知识库提案 #{proposal.id}\n"
        f"标题：{proposal.title}\n"
        f"提交者：{_format_proposer(proposal)}\n"
        f"需要票数：{proposal.required_votes}\n"
        f"截止时间：{expire_text}\n\n"
        f"{proposal.content}\n\n"
        "如果希望将该提案加入知识库的话，请给这条消息点表情投票同意。"
    )


def _format_proposal_detail(proposal: Proposal) -> str:
    status = "已通过" if proposal.status == "approved" else "投票中"
    lines = [
        f"提案 #{proposal.id}：{proposal.title}",
        f"状态：{status}",
        f"票数：{proposal.vote_count}/{proposal.required_votes}",
        f"提交者：{_format_proposer(proposal)}",
    ]
    if proposal.knowledge_id is not None:
        lines.append(f"知识 ID：{proposal.knowledge_id}")
    if proposal.expired_at is not None and proposal.status == "voting":
        lines.append(
            f"截止：{proposal.expired_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
        )
    lines.append(f"正文：\n{proposal.content}")
    return "\n".join(lines)


def _resolve_proposer_name(event: GroupMessageEvent) -> str:
    """按绑定名称 > 用户名 > 群昵称解析提交者显示名。"""
    user_id = event.get_user_id()
    username = event.sender.nickname.strip() if event.sender.nickname else ""
    group_card = event.sender.card.strip() if event.sender.card else ""
    fallback_name = username or group_card or user_id
    return character_binding.get_character_name(user_id, fallback_name)


def _format_proposer(proposal: Proposal) -> str:
    """格式化提交者，旧数据缺少名称时回退 QQ 号。"""
    proposer_id = str(proposal.proposer_id)
    if not proposal.proposer_name or proposal.proposer_name == proposer_id:
        return proposer_id
    return f"{proposal.proposer_name}（{proposer_id}）"


def _phase_name(phase: str) -> str:
    match phase:
        case "title":
            return "编辑标题"
        case "content":
            return "编辑正文"
        case "review":
            return "最终确认"
        case _:
            return phase


def _extract_message_id(sent: Any) -> int | None:
    if isinstance(sent, dict):
        value = sent.get("message_id")
    else:
        value = getattr(sent, "message_id", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
