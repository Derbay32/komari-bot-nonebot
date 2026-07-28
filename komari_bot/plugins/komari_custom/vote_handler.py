"""komari_custom 表情投票处理。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from nonebot import get_bots, logger, on_notice
from nonebot.adapters.onebot.v11 import Bot, NoticeEvent  # noqa: TC002

from komari_bot.common.onebot_messages import plain_text_message

from .proposal_repository import ProposalRepository  # noqa: TC001

if TYPE_CHECKING:
    from .models import Proposal


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
        *,
        source_key: str | None = None,
    ) -> int: ...


class VoteHandlerState:
    """投票处理依赖状态。"""

    def __init__(self) -> None:
        self.repository: ProposalRepository | None = None
        self.config_manager: ConfigManager | None = None
        self.knowledge_plugin: KnowledgePlugin | None = None


state = VoteHandlerState()
APPROVAL_LEASE_SECONDS = 300
APPROVAL_RECOVERY_BATCH_SIZE = 50


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
        if proposal is None or proposal.status not in {"voting", "approving"}:
            return

        if proposal.status == "voting":
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
    excluded_user_ids = {str(proposal.proposer_id), str(bot.self_id)}
    valid_users = sorted(
        {user_id for user_id in user_ids if user_id not in excluded_user_ids}
    )
    return await state.repository.replace_votes(proposal_id, valid_users)


async def approve_if_ready(bot: Bot, proposal_id: int) -> None:
    """票数达标时写入知识库并通知群聊。"""
    if state.repository is None or state.knowledge_plugin is None:
        return
    proposal = await state.repository.get_by_id(proposal_id)
    if proposal is None or proposal.status == "approved":
        return
    if proposal.status not in {"voting", "approving"}:
        return

    approval_token = uuid4().hex
    claimed = await state.repository.claim_for_approval(
        proposal_id,
        approval_token,
        lease_seconds=APPROVAL_LEASE_SECONDS,
    )
    if claimed is None:
        return

    try:
        keywords = extract_keywords(claimed.title)
        content = f"【{claimed.title}】\n{claimed.content}"
        knowledge_id = await state.knowledge_plugin.add_knowledge(
            content=content,
            keywords=keywords,
            category="custom",
            notes=f"由群成员(QQ:{claimed.proposer_id})提交，经投票通过加入",
            source_key=f"komari_custom:proposal:{claimed.id}",
        )
        approved = await state.repository.mark_approved(
            claimed.id,
            knowledge_id,
            approval_token,
        )
    except Exception:
        await state.repository.release_approval(claimed.id, approval_token)
        raise

    if approved is None:
        return

    await bot.call_api(
        "send_group_msg",
        group_id=approved.group_id,
        message=plain_text_message(
            f"✅ 提案 #{approved.id}《{approved.title}》投票通过！\n"
            f"已加入知识库，知识 ID：{knowledge_id}"
        ),
    )


async def recover_pending_approvals() -> int:
    """周期接管漏处理的达标提案与租约过期的 ``approving`` 提案。"""
    if (
        state.repository is None
        or state.config_manager is None
        or state.knowledge_plugin is None
    ):
        return 0
    if not state.config_manager.get().plugin_enable:
        return 0

    bots = get_bots()
    if not bots:
        logger.debug("[KomariCustom] 无在线 Bot，跳过采纳恢复")
        return 0

    await state.repository.initialize()
    proposal_ids = await state.repository.list_approval_candidates(
        lease_seconds=APPROVAL_LEASE_SECONDS,
        limit=APPROVAL_RECOVERY_BATCH_SIZE,
    )
    bot = cast("Bot", min(bots.items(), key=lambda item: str(item[0]))[1])
    completed = 0
    for proposal_id in proposal_ids:
        try:
            before = await state.repository.get_by_id(proposal_id)
            await approve_if_ready(bot, proposal_id)
            after = await state.repository.get_by_id(proposal_id)
        except Exception:
            logger.exception(
                "[KomariCustom] 周期恢复提案采纳失败: proposal_id={}",
                proposal_id,
            )
            continue
        if _became_approved(before, after):
            completed += 1
    return completed


def _became_approved(before: Proposal | None, after: Proposal | None) -> bool:
    """判断本轮是否把未完成提案推进到已采纳。"""
    return (
        before is not None
        and before.status != "approved"
        and after is not None
        and after.status == "approved"
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
