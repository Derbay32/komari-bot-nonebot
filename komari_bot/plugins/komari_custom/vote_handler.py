"""komari_custom 表情投票处理。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from nonebot import get_bots, logger, on_notice
from nonebot.adapters.onebot.v11 import Bot, NoticeEvent  # noqa: TC002

from komari_bot.onebot.onebot_messages import plain_text_message

from .admission import business_admitted
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
    """主动拉取表情回应用户并覆盖本地投票计数。

    表情回应读取前先对目标群做业务裁决：受限群不触达平台读取（独立治理效果），
    并记下休眠标记（供恢复后换届），返回 ``None`` 保持静默。

    恢复准入后的首次业务效果（本读取）会触发换届轮换：休眠期平台累积的票整批
    记为旧轮 baseline，拉取照常发生、全量刷入；后续达标判定按「新轮有效票 =
    当前票 - baseline」执行。
    """
    if state.repository is None or state.config_manager is None:
        return None
    proposal = await state.repository.get_by_id(proposal_id)
    if proposal is None:
        return None
    # 平台读取：消费具体群业务内容，读取前按该群裁决。
    if not business_admitted(proposal.group_id):
        # 受限（休眠）：保存安全进度标记，不触达平台、不刷新投票。
        await state.repository.mark_dormant(proposal.id)
        return None

    was_dormant = bool(getattr(proposal, "dormant_seen", False))
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

    user_ids = _extract_vote_user_ids(result)
    excluded_user_ids = {str(proposal.proposer_id), str(bot.self_id)}
    valid_users = sorted(
        {user_id for user_id in user_ids if user_id not in excluded_user_ids}
    )
    updated = await state.repository.replace_votes(proposal_id, valid_users)
    if was_dormant and updated is not None:
        # 换届：旧轮票（休眠期攒）整批记入 baseline，轮次 +1 后达标只看新轮票。
        await state.repository.rotate_vote_epoch(proposal_id, valid_users)
        updated = await state.repository.get_by_id(proposal_id) or updated
    return updated


async def approve_if_ready(bot: Bot, proposal_id: int) -> None:
    """票数达标时写入知识库并通知群聊。

    knowledge commit（``add_knowledge`` 的不可逆全局提交）与采纳通知都属独立
    受治理业务效果：效果前对本群业务裁决。受限时跳过提交与通知、不补发；已认
    领采纳（``approving``）的历史读属已提交 knowledge 的既成事实，不做重复
    add/embedding。
    """
    if state.repository is None or state.knowledge_plugin is None:
        return
    proposal = await state.repository.get_by_id(proposal_id)
    if proposal is None or proposal.status not in {"voting", "approving"}:
        return

    # 采纳提交与通知前的统一业务裁决；受限则跳过且不补发采纳通知。
    # 已认领采纳（前次于 add 已提交 knowledge / 正在处理）：本轮不重复提交，
    # 避免同一 source_key 重复 add/embedding；唯一认领由租约与 claim 保证。
    if not business_admitted(proposal.group_id) or proposal.status == "approving":
        return

    # 沉眠/预活跃轮次（vote_epoch==0）须经新轮 fetch 刷新；换届后只按新轮有效票
    # 判达标（休眠期平台累积旧票已整批计入 baseline，不计入达标）。
    if getattr(proposal, "vote_epoch", 0) <= 0 or _effective_vote_count(
        proposal
    ) < getattr(proposal, "required_votes", 0):
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
        if _is_knowledge_source_conflict():
            # knowledge source_key 冲突：进入运维 closed hold，阻断后续合同，
            # 绝不停留在可重试的投票 / 认领状态。
            await state.repository.mark_hold(claimed.id, approval_token, "knowledge_source_conflict")
            raise
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


def _effective_vote_count(proposal: Proposal) -> int:
    """换届后的新轮有效票数 = 当前投票者 - baseline。

    无 baseline（未经历休眠轮换）时直接采用原始 ``vote_count``；有 baseline 时
    减去旧轮投票者快照，保证休眠期平台累积旧票不计入新轮达标。
    """
    baseline = set(getattr(proposal, "vote_baseline_voters", None) or [])
    if baseline:
        voted = set(getattr(proposal, "voted_users", None) or [])
        return len(voted - baseline)
    return int(getattr(proposal, "vote_count", 0) or 0)


def extract_keywords(title: str) -> list[str]:
    """从标题中提取用于知识库检索的关键词。"""
    words = [word for word in re.split(r"[\s,，。.!！?？、/\\|:：;；]+", title) if word]
    keywords = list(dict.fromkeys([title.strip(), *words]))
    return keywords[:8] if keywords else ["群友提案"]


def _is_knowledge_source_conflict() -> bool:
    """知识源键冲突哨兵：``add_knowledge`` 因 source_key 冲突不可恢复地失败。

    真实知识层对 ``source_key`` 幂等 upsert，正常情况下不会抛出冲突；此处只
    识别「运维/数据面显式标出的来源冲突」并收敛为 closed hold。其他知识写入
    失败按可重试处理（释放认领）。
    """
    import sys

    exc = sys.exc_info()[1]
    if exc is None:
        return False
    return "knowledge_source_conflict" in str(exc)


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
