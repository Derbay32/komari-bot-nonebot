"""Komari Memory 动态提示词构建服务（5 段式 OpenAI messages）。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.plugin import require
from zhdate import ZhDate

from ..config_schema import KomariMemoryConfigSchema  # noqa: TC001
from .prompt_template import get_template

if TYPE_CHECKING:
    from .memory_service import MemoryService

# 获取常识库插件
komari_knowledge = require("komari_knowledge")

# 获取角色绑定插件
character_binding = require("character_binding")


def get_festival_info() -> str | None:
    """获取当前节日信息。

    Returns:
        节日信息字符串，无节日时返回 None
    """
    today = datetime.now().astimezone()
    # zhdate 不支持时区感知的 datetime，需要转换为 naive datetime
    today_naive = today.replace(tzinfo=None)
    lunar = ZhDate.from_datetime(today_naive)

    festivals = []

    # 传统节日（农历）
    traditional = {
        (1, 1): "春节",
        (1, 15): "元宵节",
        (2, 2): "龙抬头",
        (5, 5): "端午节",
        (7, 7): "七夕节",
        (7, 15): "中元节",
        (8, 15): "中秋节",
        (9, 9): "重阳节",
        (10, 1): "寒衣节",
        (10, 15): "下元节",
        (12, 8): "腊八节",
        (12, 23): "小年",
    }

    month, day = lunar.lunar_month, lunar.lunar_day
    if (month, day) in traditional:
        # chinese() 返回格式: "二零二五年腊月初八 乙巳年 (蛇年)"
        # 提取月份日部分（去掉年份前缀）
        chinese_full = lunar.chinese().split()[0]  # "二零二五年腊月初八"
        chinese_date = chinese_full[5:]  # 去掉年份，保留 "腊月初八"
        festivals.append(f"今天是{traditional[(month, day)]}（农历{chinese_date}）")

    # 公历节日
    public = {
        (1, 1): "元旦",
        (2, 14): "情人节",
        (3, 8): "妇女节",
        (3, 12): "植树节",
        (3, 29): "小鞠知花的生日",
        (4, 1): "愚人节",
        (5, 1): "劳动节",
        (5, 4): "青年节",
        (6, 1): "儿童节",
        (7, 1): "建党节",
        (8, 1): "建军节",
        (9, 10): "教师节",
        (10, 1): "国庆节",
        (12, 24): "平安夜",
        (12, 25): "圣诞节",
    }

    month, day = today.month, today.day
    if (month, day) in public:
        festivals.append(f"今天是{public[(month, day)]}")

    if festivals:
        return "，".join(festivals)
    return None  # 无节日时不注入


async def build_prompt(
    user_message: str,
    memories: list[dict],
    config: KomariMemoryConfigSchema,
    recent_messages: list | None = None,
    current_user_id: str | None = None,
    current_user_nickname: str | None = None,
    search_query: str | None = None,
    memory_service: MemoryService | None = None,
    group_id: str | None = None,
) -> list[dict[str, str]]:
    """构建 5 段式 OpenAI 格式消息数组。

    结构：
    ① system    — 角色设定 + 记忆 + 实体 + 知识库 + 时间
    ② user/asst — 对话历史（Redis buffer 交替构造）
    ③ assistant — 伪造记忆确认
    ④ system    — 输出格式指令
    ⑤ assistant — CoT 思维链前缀

    Args:
        user_message: 用户原始消息（用于生成回复）
        memories: 检索到的对话记忆
        config: 插件配置
        recent_messages: 最近的消息列表（可选）
        current_user_id: 当前用户 ID（可选）
        current_user_nickname: 当前用户昵称（可选）
        search_query: 重写后的搜索查询（用于知识库检索）
        memory_service: 记忆服务（用于检索用户实体，可选）
        group_id: 群组 ID（用于检索用户实体，可选）

    Returns:
        OpenAI 格式消息列表 [{role, content}]
    """
    template = get_template()
    messages: list[dict[str, str]] = []

    # ═══════════════════════════════════════
    # ① system — 角色设定 + 记忆 + 实体 + 知识库
    # ═══════════════════════════════════════
    system_parts: list[str] = []

    # 角色设定（来自 YAML 模板）
    system_parts.append(template["system_prompt"])

    # 当前时间
    system_parts.append(
        f"<current_time>{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}</current_time>"
    )

    # 节日信息
    festival_info = get_festival_info()
    if festival_info:
        system_parts.append(f"<festival_info>{festival_info}</festival_info>")

    # 对话记忆
    if memories:
        memory_items = "\n".join([f"- {m['summary']}" for m in memories])
        system_parts.append(f"<memory>\n{memory_items}\n</memory>")

    # 常识库
    if config.knowledge_enabled:
        try:
            # 优先使用重写后的查询进行检索
            query_for_search = search_query or user_message
            knowledge_results = await komari_knowledge.search_knowledge(
                query=query_for_search,
                limit=config.knowledge_limit,
            )
            if knowledge_results:
                # 根据 source 字段分组
                keyword_results = [
                    r for r in knowledge_results if r.source == "keyword"
                ]
                vector_results = [r for r in knowledge_results if r.source == "vector"]

                # 分别注入不同来源的知识
                if keyword_results:
                    keyword_items = "\n".join(
                        [f"- {r.content}" for r in keyword_results]
                    )
                    system_parts.append(
                        f"<keyword_knowledge>\n{keyword_items}\n</keyword_knowledge>"
                    )

                if vector_results:
                    vector_items = "\n".join([f"- {r.content}" for r in vector_results])
                    system_parts.append(
                        f"<vector_knowledge>\n{vector_items}\n</vector_knowledge>"
                    )
        except Exception:
            logger.debug("[KomariMemory] 常识库检索失败", exc_info=True)

    # 收集对话中的用户 ID（供常识检索和实体注入共用）
    all_user_ids: set[str] = set()
    if recent_messages:
        for msg in recent_messages:
            if not msg.is_bot:
                all_user_ids.add(msg.user_id)
    if current_user_id:
        all_user_ids.add(current_user_id)

    # 用户常识检索（基于对话中的用户 UID）
    if all_user_ids:
        user_profile_results: list[dict] = []
        for uid in all_user_ids:
            try:
                results = await komari_knowledge.search_by_keyword(uid)
                user_profile_results.extend(
                    [{"uid": uid, "content": r.content} for r in results]
                )
            except Exception:
                logger.debug(f"[KomariMemory] 用户 {uid} 的常识检索失败", exc_info=True)

        if user_profile_results:
            profile_items = "\n".join(
                [
                    f"- 用户({item['uid']}): {item['content']}"
                    for item in user_profile_results
                ]
            )
            system_parts.append(f"<user_profiles>\n{profile_items}\n</user_profiles>")

    # 用户实体注入（从对话总结中提取的结构化实体）
    if memory_service and group_id and all_user_ids:
        entity_parts: list[str] = []
        for uid in all_user_ids:
            try:
                entities = await memory_service.get_entities(
                    user_id=uid, group_id=group_id
                )
                entity_parts.extend(
                    f"- 用户({uid}): {e['key']}={e['value']} [{e.get('category', 'general')}]"
                    for e in entities
                )
            except Exception:
                logger.debug(f"[KomariMemory] 用户 {uid} 的实体检索失败", exc_info=True)

        if entity_parts:
            entity_text = "\n".join(entity_parts)
            system_parts.append(f"<user_entities>\n{entity_text}\n</user_entities>")

    messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    # ═══════════════════════════════════════
    # ② user/assistant — 对话历史
    # ═══════════════════════════════════════
    if recent_messages:
        current_block: list[str] = []
        current_side: str | None = None  # "user" 或 "assistant"

        for msg in recent_messages:
            this_side = "assistant" if msg.is_bot else "user"

            if msg.is_bot:
                # assistant 侧：直接使用原始回复内容
                msg_text = msg.content
            else:
                # user 侧：添加角色名前缀
                character_name = character_binding.get_character_name(
                    user_id=msg.user_id,
                    fallback_nickname=msg.user_nickname,
                )
                msg_text = f"- {character_name}: {msg.content}"

            # 切换侧时，保存当前块
            if current_side is not None and this_side != current_side:
                block_text = "\n".join(current_block)
                messages.append({"role": current_side, "content": block_text})
                current_block = []

            current_block.append(msg_text)
            current_side = this_side

        # 保存最后一个块
        if current_block and current_side:
            block_text = "\n".join(current_block)
            messages.append({"role": current_side, "content": block_text})

    # 当前用户消息（使用 <user_input> 标签防止提示词注入）
    current_character_name = (
        character_binding.get_character_name(
            user_id=current_user_id,
            fallback_nickname=current_user_nickname,
        )
        if current_user_id
        else "用户"
    )
    current_text = (
        f"- {current_character_name}: <user_input>{user_message}</user_input>"
    )
    messages.append({"role": "user", "content": current_text})

    # ═══════════════════════════════════════
    # ③ assistant — 伪造记忆确认
    # ═══════════════════════════════════════
    messages.append({"role": "assistant", "content": template["memory_ack"]})

    # ═══════════════════════════════════════
    # ④ system — 输出格式指令
    # ═══════════════════════════════════════
    messages.append({"role": "system", "content": template["output_instruction"]})

    # ═══════════════════════════════════════
    # ⑤ assistant — CoT 思维链前缀
    # ═══════════════════════════════════════
    messages.append({"role": "assistant", "content": template["cot_prefix"]})

    return messages
