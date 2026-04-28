"""Komari Memory 动态提示词构建服务（5 段式 OpenAI messages）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.plugin import require
from zhdate import ZhDate

from komari_bot.common.dsv4_instruct import inject_dsv4_instruct_to_first_user_message
from komari_bot.plugins.komari_memory.config_schema import (  # noqa: TC001
    KomariMemoryConfigSchema,
)

from .prompt_template import get_template
from .reply_context import ReplyContext  # noqa: TC001

if TYPE_CHECKING:
    from komari_bot.plugins.komari_memory.services.memory_service import MemoryService

# 获取常识库插件
komari_knowledge = require("komari_knowledge")

# 获取角色绑定插件
character_binding = require("character_binding")


def _resolve_favor_level(daily_favor: int) -> str:
    if daily_favor <= 25:
        return "冷淡"
    if daily_favor <= 50:
        return "中性"
    if daily_favor <= 75:
        return "友好"
    return "非常友好"


def _resolve_favor_description(daily_favor: int) -> str:
    if daily_favor <= 25:
        return (
            "今日小鞠对这位用户兴致不高，态度偏淡漠，不会有太多主动交流的意愿，"
            "回复保持礼貌但疏远"
        )
    if daily_favor <= 50:
        return (
            "今日小鞠对这位用户的态度正常，没有特别的亲近或疏远，"
            "按照平时的关系和印象来互动即可"
        )
    if daily_favor <= 75:
        return (
            "今日小鞠对这位用户心情不错，比平时更愿意多聊几句，"
            "语气中带着一些亲近感，偶尔会主动关心"
        )
    return (
        "今日小鞠格外亲近这位用户，说话时会不自觉地更加坦诚和温柔，"
        "甚至愿意分享一些平时不会说的事"
    )


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
    image_urls: list[str] | None = None,
    reply_context: ReplyContext | None = None,
    reply_image_urls: list[str] | None = None,
    query_embedding: list[float] | None = None,
    favor_daily: int | None = None,
    favor_user_id: str | None = None,
    *,
    vision_tool_mode: bool = False,
) -> list[dict[str, Any]]:
    """构建面向 DeepSeek KV Cache 优化的 OpenAI 格式消息数组。

    结构：
    ① system    — 静态角色设定
    ② system    — 静态输出格式指令
    ③ user/asst — 对话历史（Redis buffer 交替构造）
    ④ user      — 动态上下文（时间、记忆、知识库、实体、好感度）
    ⑤ user      — 当前用户消息
    ⑥ assistant — 旧版预填充（可选）

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
        image_urls: 用户消息中的图片 URL 列表（可选）
        reply_context: 当前消息引用的上下文（可选）
        reply_image_urls: 当前消息引用图片的可见 URL 列表（可选）
        query_embedding: 预先计算好的查询特征向量，用于知识库检索（可选）
        vision_tool_mode: 是否使用工具调用读图模式。开启时只注入图片索引说明，不嵌入 base64 图片块

    Returns:
        OpenAI 格式消息列表 [{role, content}]，当包含图片时 content 为数组格式
    """
    template = get_template()
    messages: list[dict[str, Any]] = []

    # ═══════════════════════════════════════
    # ①② 静态 system — 角色设定 + 输出格式指令
    # ═══════════════════════════════════════
    messages.append({"role": "system", "content": template["system_prompt"]})
    messages.append({"role": "system", "content": template["output_instruction"]})

    # ═══════════════════════════════════════
    # ③ user/assistant — 对话历史
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

    if reply_context is not None and reply_context.source_side == "assistant":
        assistant_reply_parts: list[str] = []
        if reply_context.text:
            assistant_reply_parts.append(reply_context.text)
        if reply_context.image_count > 0:
            if reply_image_urls:
                assistant_reply_parts.append(
                    f"（你上一条还发了 {reply_context.image_count} 张图片，下面附上用户正在回复的引用图片。）"
                )
            else:
                assistant_reply_parts.append(
                    f"（你上一条发了 {reply_context.image_count} 张图片，但当前引用图不可直接查看。）"
                )
        if assistant_reply_parts:
            messages.append(
                {"role": "assistant", "content": "\n".join(assistant_reply_parts)}
            )

    # ═══════════════════════════════════════
    # ④ 动态 user — 时间 + 记忆 + 实体 + 知识库
    # ═══════════════════════════════════════
    dynamic_parts: list[str] = []

    # 当前时间
    dynamic_parts.append(
        f"<current_time>{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}</current_time>"
    )

    # 节日信息
    festival_info = get_festival_info()
    if festival_info:
        dynamic_parts.append(f"<festival_info>{festival_info}</festival_info>")

    # 对话记忆
    if memories:
        memory_items = "\n".join([f"- {m['summary']}" for m in memories])
        dynamic_parts.append(
            f"<memory>\n以下是过往的对话记忆:\n{memory_items}\n</memory>"
        )

    # 常识库
    if config.knowledge_enabled:
        try:
            # 优先使用重写后的查询进行检索
            query_for_search = search_query or user_message
            knowledge_results = await komari_knowledge.search_knowledge(
                query=query_for_search,
                limit=config.knowledge_limit,
                query_embedding=query_embedding,
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
                    dynamic_parts.append(
                        f"<keyword_knowledge>\n以下是与当前话题相关的关键词知识:\n{keyword_items}\n</keyword_knowledge>"
                    )

                if vector_results:
                    vector_items = "\n".join([f"- {r.content}" for r in vector_results])
                    dynamic_parts.append(
                        f"<vector_knowledge>\n以下是语义检索到的相关知识:\n{vector_items}\n</vector_knowledge>"
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
    if (
        reply_context is not None
        and reply_context.source_side == "user"
        and reply_context.user_id
    ):
        all_user_ids.add(reply_context.user_id)

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
            dynamic_parts.append(
                f"<user_profiles>\n以下是对话中用户的已知信息:\n{profile_items}\n</user_profiles>"
            )

    # 用户实体注入（从对话总结中提取的结构化实体）
    if memory_service and group_id and all_user_ids:
        entity_parts: list[str] = []
        interaction_parts: list[str] = []

        for uid in all_user_ids:
            try:
                profile = await memory_service.get_user_profile(
                    user_id=uid, group_id=group_id
                )
                if profile:
                    display_name = str(profile.get("display_name") or uid)
                    traits = profile.get("traits")
                    if isinstance(traits, dict):
                        for key, payload in traits.items():
                            if not isinstance(payload, dict):
                                continue
                            value = str(payload.get("value", "")).strip()
                            if not value:
                                continue
                            category = str(payload.get("category", "general"))
                            entity_parts.append(
                                f"- 用户({uid}/{display_name}): {key}={value} [{category}]"
                            )
            except Exception:
                logger.debug(
                    "[KomariMemory] 用户 {} 的画像实体检索失败",
                    uid,
                    exc_info=True,
                )

            try:
                # 专门获取互动历史记录
                history_json = await memory_service.get_interaction_history(
                    user_id=uid, group_id=group_id
                )
                if history_json:
                    interaction_parts.append(
                        json.dumps(history_json, ensure_ascii=False)
                    )
            except Exception:
                logger.debug(
                    f"[KomariMemory] 用户 {uid} 的互动历史检索失败", exc_info=True
                )

        if entity_parts:
            entity_text = "\n".join(entity_parts)
            dynamic_parts.append(
                f"<user_entities>\n以下是从历史对话中提取的用户实体信息:\n{entity_text}\n</user_entities>"
            )

        if interaction_parts:
            interaction_text = "\n\n".join(interaction_parts)
            dynamic_parts.append(
                f"<user_interaction_history>\n{interaction_text}\n</user_interaction_history>"
            )

    if favor_daily is not None:
        favor_display_name = (
            character_binding.get_character_name(
                user_id=favor_user_id,
                fallback_nickname=current_user_nickname,
            )
            if favor_user_id
            else (current_user_nickname or "当前用户")
        )
        favor_level = _resolve_favor_level(favor_daily)
        favor_description = _resolve_favor_description(favor_daily)
        dynamic_parts.append(
            "\n".join(
                [
                    "<favorability_modifier>",
                    "[注意：此修饰为基于普通态度的加值，优先级低于<long_term_relation>和<user_interaction_history>中的实际关系描述]",
                    f"今日小鞠对用户({favor_display_name})的好感度：{favor_daily}（{favor_level}）",
                    f"影响描述：{favor_description}",
                    "</favorability_modifier>",
                ]
            )
        )

    if dynamic_parts:
        messages.append({"role": "user", "content": "\n\n".join(dynamic_parts)})

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

    reply_intro_lines: list[str] = []
    if reply_context is not None:
        if reply_context.source_side == "user":
            reply_name = (
                character_binding.get_character_name(
                    user_id=reply_context.user_id,
                    fallback_nickname=reply_context.user_nickname or "被回复用户",
                )
                if reply_context.user_id
                else (reply_context.user_nickname or "被回复用户")
            )
            if reply_context.text:
                reply_intro_lines.append(
                    f"- {reply_name}（被回复）: {reply_context.text}"
                )
            if reply_context.image_count > 0:
                if reply_image_urls:
                    reply_intro_lines.append(
                        f"- {reply_name}（被回复）发送了 {reply_context.image_count} 张图片。"
                    )
                else:
                    reply_intro_lines.append(
                        f"- {reply_name}（被回复）发送了 {reply_context.image_count} 张图片，但当前不可直接查看。"
                    )
        elif reply_context.image_count > 0:
            if reply_image_urls:
                reply_intro_lines.append(
                    f"（以下是你上一条被引用的 {reply_context.image_count} 张图片）"
                )
            else:
                reply_intro_lines.append(
                    f"（你上一条被引用的是 {reply_context.image_count} 张图片，但当前不可直接查看）"
                )

    reply_intro_text = "\n".join(reply_intro_lines)
    has_multimodal_content = bool(reply_image_urls or image_urls)
    if has_multimodal_content and vision_tool_mode:
        vision_lines: list[str] = []
        reply_image_count = len(reply_image_urls or [])
        current_image_count = len(image_urls or [])
        total_image_count = reply_image_count + current_image_count
        if total_image_count > 0:
            vision_lines.append(
                f"[系统提示：当前对话包含 {total_image_count} 张可读取图片，你可以使用 read_image 工具按索引查看它们。]"
            )
        if reply_image_count > 0:
            vision_lines.append(
                f"- 被回复消息有 {reply_image_count} 张图片，可用 read_image 查看，索引范围为 0 到 {reply_image_count - 1}。"
            )
        if current_image_count > 0:
            current_start_index = reply_image_count
            current_end_index = reply_image_count + current_image_count - 1
            vision_lines.append(
                f"- 当前用户发送了 {current_image_count} 张图片，可用 read_image 查看，索引范围为 {current_start_index} 到 {current_end_index}。"
            )
        text_parts = [part for part in [reply_intro_text, current_text, *vision_lines] if part]
        messages.append({"role": "user", "content": "\n".join(text_parts)})
    elif has_multimodal_content:
        content_parts: list[dict[str, Any]] = []
        if reply_intro_text:
            content_parts.append({"type": "text", "text": reply_intro_text})
        content_parts.extend(
            {
                "type": "image_url",
                "image_url": {"url": url},
            }
            for url in (reply_image_urls or [])
        )
        content_parts.append({"type": "text", "text": current_text})
        content_parts.extend(
            {
                "type": "image_url",
                "image_url": {"url": url},
            }
            for url in (image_urls or [])
        )
        messages.append({"role": "user", "content": content_parts})
    else:
        text_content = (
            "\n".join([reply_intro_text, current_text])
            if reply_intro_text
            else current_text
        )
        messages.append({"role": "user", "content": text_content})

    messages = inject_dsv4_instruct_to_first_user_message(
        messages,
        model=getattr(config, "llm_model_chat", ""),
        mode=getattr(config, "dsv4_roleplay_instruct_mode", "auto"),
    )

    if getattr(config, "assistant_prefill_enabled", False):
        # ═══════════════════════════════════════
        # ⑥ assistant — 旧版预填充（可选）
        # ═══════════════════════════════════════
        messages.append(
            {
                "role": template.get("memory_ack_role", "assistant"),
                "content": template["memory_ack"],
            }
        )
        messages.append(
            {
                "role": template.get("cot_prefix_role", "assistant"),
                "content": template["cot_prefix"],
            }
        )

    return messages
