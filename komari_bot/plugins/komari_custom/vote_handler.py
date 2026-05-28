"""komari_custom 表情投票处理。"""

from __future__ import annotations

import re
from typing import Any, Protocol

from nonebot import logger, on_notice
from nonebot.adapters.onebot.v11 import Bot, NoticeEvent  # noqa: TC002

from .proposal_repository import ProposalRepository  # noqa: TC001


class ConfigManager(Protocol):
    """配置管理器最小协议。"""

    def get(self) -> Any: ...


class KnowledgePlugin(Protocol):
    """知识库插件最小协议。"""

    async def add_knowledge(
        self,
        content: str,
        keywords: list[str],
        category: str,
        notes: str | None = None,
    ) -> int: ...


class VoteHandlerState:
    """投票处理依赖状态。"""

    def __init__(self) -> None:
        self.repository: ProposalRepository | None = None
        self.config_manager: ConfigManager | None = None
        self.knowledge_plugin: KnowledgePlugin | None = None


state = VoteHandlerState()


def setup_vote_handler(
    repository: ProposalRepository,
    config_manager: ConfigManager,
    knowledge_plugin: KnowledgePlugin,
) -> None:
    """注入投票处理依赖。"""
    state.repository = repository
    state.config_manager = config_manager
    state.knowledge_plugin = knowledge_plugin


def _is_emoji_like_event(event: NoticeEvent) -> bool:
    return getattr(event, "notice_type", None) == "group_msg_emoji_like"


vote_notice = on_notice(rule=_is_emoji_like_event, priority=99, block=False)


@vote_notice.handle()
async def handle_emoji_like(bot: Bot, event: NoticeEvent) -> None:
    """监听群消息表情回应并更新提案票数。"""
    if state.repository is None or state.config_manager is None:
        return
    config = state.config_manager.get()
    if not config.plugin_enable:
        return

    message_id = _get_int_attr(event, "message_id")
    group_id = _get_int_attr(event, "group_id")
    user_id = getattr(event, "user_id", None)
    if message_id is None or group_id is None or user_id is None:
        return

    try:
        await state.repository.initialize()
        proposal = await state.repository.find_by_vote_message_id(message_id)
        if proposal is None or proposal.status != "voting":
            return

        if _event_contains_target_emoji(event, str(config.vote_emoji_id)):
            updated = await state.repository.add_vote(proposal.id, str(user_id))
            proposal = updated or proposal

        fetched = await fetch_and_update_votes(
            bot,
            message_id=message_id,
            proposal_id=proposal.id,
        )
        proposal = fetched or proposal
        await approve_if_ready(bot, proposal.id)
    except Exception:
        logger.exception("[KomariCustom] 处理提案投票事件失败")


async def fetch_and_update_votes(
    bot: Bot,
    *,
    message_id: int,
    proposal_id: int,
) -> Any | None:
    """主动拉取表情回应用户并覆盖本地投票计数。"""
    if state.repository is None or state.config_manager is None:
        return None
    config = state.config_manager.get()
    try:
        result = await bot.call_api(
            "fetch_emoji_like",
            message_id=message_id,
            emoji_id=str(config.vote_emoji_id),
        )
    except Exception as e:
        logger.debug("[KomariCustom] 主动拉取表情回应失败: {}", e)
        return None

    proposal = await state.repository.get_by_id(proposal_id)
    if proposal is None:
        return None
    user_ids = _extract_vote_user_ids(result)
    valid_users = sorted({uid for uid in user_ids if uid != str(proposal.proposer_id)})
    if not valid_users:
        return None
    return await state.repository.replace_votes(proposal_id, valid_users)


async def approve_if_ready(bot: Bot, proposal_id: int) -> None:
    """票数达标时写入知识库并通知群聊。"""
    if state.repository is None or state.knowledge_plugin is None:
        return
    proposal = await state.repository.get_by_id(proposal_id)
    if proposal is None or proposal.status != "voting":
        return
    if proposal.vote_count < proposal.required_votes:
        return

    keywords = extract_keywords(proposal.title)
    content = f"【{proposal.title}】\n{proposal.content}"
    knowledge_id = await state.knowledge_plugin.add_knowledge(
        content=content,
        keywords=keywords,
        category="custom",
        notes=f"由群成员(QQ:{proposal.proposer_id})提交，经投票通过加入",
    )
    approved = await state.repository.mark_approved(proposal.id, knowledge_id)
    if approved is None:
        return

    await bot.call_api(
        "send_group_msg",
        group_id=approved.group_id,
        message=(
            f"✅ 提案 #{approved.id}《{approved.title}》投票通过！\n"
            f"已加入知识库，知识 ID：{knowledge_id}"
        ),
    )


def extract_keywords(title: str) -> list[str]:
    """从标题中提取用于知识库检索的关键词。"""
    words = [word for word in re.split(r"[\s,，。.!！?？、/\\|:：;；]+", title) if word]
    keywords = list(dict.fromkeys([title.strip(), *words]))
    return keywords[:8] if keywords else ["群友提案"]


def _get_int_attr(event: NoticeEvent, name: str) -> int | None:
    value = getattr(event, name, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_contains_target_emoji(event: NoticeEvent, target_emoji_id: str) -> bool:
    emoji_id = getattr(event, "emoji_id", None)
    if emoji_id is not None:
        return str(emoji_id) == target_emoji_id
    likes = getattr(event, "likes", None)
    if not isinstance(likes, list):
        return True
    for item in likes:
        if isinstance(item, dict) and str(item.get("emoji_id")) == target_emoji_id:
            return True
    return False


def _extract_vote_user_ids(result: Any) -> list[str]:
    """兼容不同 NapCat 返回结构，提取投票用户 ID。"""
    if isinstance(result, dict):
        for key in ("users", "user_ids", "likes", "data"):
            value = result.get(key)
            extracted = _extract_vote_user_ids(value)
            if extracted:
                return extracted
        user_id = result.get("user_id") or result.get("uin") or result.get("qq")
        return [str(user_id)] if user_id is not None else []
    if isinstance(result, list):
        users: list[str] = []
        for item in result:
            if isinstance(item, dict):
                emoji_id = item.get("emoji_id")
                config = state.config_manager.get() if state.config_manager else None
                if (
                    config is not None
                    and emoji_id is not None
                    and str(emoji_id) != str(config.vote_emoji_id)
                ):
                    continue
                users.extend(_extract_vote_user_ids(item))
            elif item is not None:
                users.append(str(item))
        return users
    return []
