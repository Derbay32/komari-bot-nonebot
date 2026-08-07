"""Komari Memory 动态提示词构建服务（5 段式 OpenAI messages）。"""

from __future__ import annotations

import html
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot.plugin import require
from zhdate import ZhDate

from komari_bot.llm.dsv4_instruct import inject_dsv4_instruct_to_first_user_message
from komari_bot.llm.untrusted_context import (
    LLM_SECURITY_SYSTEM_INSTRUCTION,
    UntrustedContext,
    render_untrusted_context,
)
from komari_bot.memory.profile_operations import profile_traits_to_list
from komari_bot.plugins.komari_memory.config_schema import (  # noqa: TC001
    KomariMemoryConfigSchema,
)

from .prompt_template import get_template
from .reply_context import ReplyContext  # noqa: TC001

if TYPE_CHECKING:
    from komari_bot.plugins.komari_memory.services.memory_service import MemoryService
    from komari_bot.plugins.user_data.models import UserFavorability

# 获取常识库插件
komari_knowledge = require("komari_knowledge")

# 获取角色绑定插件
character_binding = require("character_binding")


def _clean_yaml_text(value: object) -> str:
    """清理注入 prompt 的 YAML 风格文本，避免多余转义和空白。"""
    return _escape_prompt_text(str(value).replace("\r", " ").replace("\n", " ").strip())


def _escape_prompt_text(value: object) -> str:
    """转义外部文本，避免破坏 prompt 中的标签边界。"""
    return html.escape(str(value), quote=True)


def _clean_untrusted_line(value: object) -> str:
    """清理结构化不可信数据中的单行字段，标签转义交给统一渲染器。"""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _format_time(value: object) -> str | None:
    """把时间戳或 ISO 时间转换为人类可读短时间。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            normalized = text.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return text[:16]
    return None


def _format_profile_traits_yaml(
    profile: dict[str, Any],
    *,
    fallback_user_id: str | None,
    fallback_name: str | None,
) -> str:
    """将当前用户画像格式化为精简 YAML 风格文本。"""
    name = _clean_yaml_text(
        profile.get("display_name") or profile.get("name") or fallback_name or fallback_user_id or "当前用户"
    )
    traits = profile_traits_to_list(profile.get("traits"))
    lines = [f"name: {name}"]
    if traits:
        lines.append("traits:")
        for item in traits:
            key = _clean_yaml_text(item["key"])
            value = _clean_yaml_text(item["value"])
            lines.append(f"  - {key}: {value}")
    return "\n".join(lines)


def _format_user_keyword_knowledge_yaml(items: list[dict[str, Any]]) -> str:
    lines = ["items:"]
    for item in items:
        uid = _clean_untrusted_line(item.get("uid", ""))
        content = _clean_untrusted_line(item.get("content", ""))
        if not uid or not content:
            continue
        lines.append(f"  - user_id: {uid}")
        lines.append(f"    content: {content}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _format_visible_users_yaml(users: dict[str, str]) -> str:
    lines = ["users:"]
    for user_id, display_name in users.items():
        lines.append(f"  - user_id: {_clean_yaml_text(user_id)}")
        lines.append(f"    name: {_clean_yaml_text(display_name)}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _format_interaction_records_yaml(records: list[dict[str, Any]]) -> str:
    lines = ["records:"]
    for record in records:
        parts = [
            _clean_yaml_text(record.get("event", "")),
            _clean_yaml_text(record.get("result", "")),
            _clean_yaml_text(record.get("emotion", "")),
        ]
        content = "；".join(part for part in parts if part)
        if not content:
            continue
        lines.append(f"  - content: {content}")
        if time_text := _format_time(record.get("timestamp")):
            lines.append(f"    time: {time_text}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _format_interaction_memories_yaml(memories: list[dict[str, Any]]) -> str:
    lines = ["memories:"]
    for memory in memories:
        content = _clean_yaml_text(memory.get("event_summary", ""))
        if not content:
            continue
        lines.append(f"  - content: {content}")
        if time_text := _format_time(memory.get("last_seen_at")):
            lines.append(f"    time: {time_text}")
    return "\n".join(lines) if len(lines) > 1 else ""


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
    favorability: UserFavorability | None = None,
    current_user_profile: dict[str, Any] | None = None,
    interaction_records: list[dict[str, Any]] | None = None,
    interaction_memories: list[dict[str, Any]] | None = None,
    *,
    vision_tool_mode: bool = False,
    search_tool_mode: bool = False,
    fetch_tool_mode: bool = False,
) -> list[dict[str, Any]]:
    """构建面向 DeepSeek KV Cache 优化的 OpenAI 格式消息数组。

    结构：
    ① system    — 静态角色设定
    ② system    — 静态输出格式指令
    ③ user/asst — 对话历史（Redis buffer 交替构造）
    ④ user      — 动态上下文（时间、记忆、知识库、实体、当前好感度阶段）
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
        memory_service: 记忆服务（保留兼容参数，prompt_builder 不再主动读取全部用户画像）
        group_id: 群组 ID（保留兼容参数）
        image_urls: 用户消息中的图片 URL 列表（可选）
        reply_context: 当前消息引用的上下文（可选）
        reply_image_urls: 当前消息引用图片的可见 URL 列表（可选）
        query_embedding: 预先计算好的查询特征向量，用于知识库检索（可选）
        vision_tool_mode: 是否使用工具调用读图模式。开启时只注入图片索引说明，不嵌入 base64 图片块
        search_tool_mode: 是否启用联网搜索工具声明
        fetch_tool_mode: 是否启用网页抓取工具声明

    Returns:
        OpenAI 格式消息列表 [{role, content}]，当包含图片时 content 为数组格式
    """
    template = await get_template()
    messages: list[dict[str, Any]] = []

    # ═══════════════════════════════════════
    # ①② 静态 system — 角色设定 + 输出格式指令
    # ═══════════════════════════════════════
    messages.append({"role": "system", "content": template["system_prompt"]})
    messages.append({"role": "system", "content": template["output_instruction"]})
    messages.append(
        {
            "role": "system",
            "content": (
                "<profile_tool_hint>\n"
                "当前触发用户画像会在 <current_user_profile> 中给出；"
                "需要其他用户画像或缺失字段时调用 read_profile(user_id)，"
                "不要猜测未提供的长期事实。\n"
                "</profile_tool_hint>"
            ),
        }
    )
    messages.append({"role": "system", "content": LLM_SECURITY_SYSTEM_INSTRUCTION})
    if search_tool_mode:
        messages.append(
            {
                "role": "system",
                "content": (
                    "[系统提示：当前对话启用了联网搜索工具 search_web。"
                    "当用户明确要求搜索、询问最新资讯/数据、或涉及你不确定的事实时，"
                    "请先调用 search_web 查询互联网；回答时要基于搜索结果如实说明，"
                    "不要编造搜索结果中没有的信息。]"
                ),
            }
        )
    if fetch_tool_mode:
        messages.append(
            {
                "role": "system",
                "content": (
                    "[系统提示：当前对话启用了网页抓取工具 fetch_page。"
                    "当搜索结果摘要不够详细、或用户提供了具体链接时，"
                    "可调用 fetch_page 获取网页正文。"
                    "一次调用可传入多个 URL，只传入你确实需要阅读的页面，"
                    "不要批量抓取所有搜索结果。]"
                ),
            }
        )

    # ═══════════════════════════════════════
    # ③ user/assistant — 对话历史
    # ═══════════════════════════════════════
    if recent_messages:
        current_block: list[str] = []
        current_side: str | None = None  # "user" 或 "assistant"

        for msg in recent_messages:
            this_side = "assistant" if msg.is_bot else "user"

            if msg.is_bot:
                msg_text = (
                    '<history_message side="assistant">\n'
                    f"{_escape_prompt_text(msg.content)}\n"
                    "</history_message>"
                )
            else:
                character_name = character_binding.get_character_name(
                    user_id=msg.user_id,
                    fallback_nickname=msg.user_nickname,
                )
                msg_text = (
                    "<history_message "
                    f'side="user" user_id="{_escape_prompt_text(msg.user_id)}" '
                    f'display_name="{_escape_prompt_text(character_name)}">\n'
                    f"{_escape_prompt_text(msg.content)}\n"
                    "</history_message>"
                )

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
            assistant_reply_parts.append(
                '<quoted_message side="assistant">\n'
                f"{_escape_prompt_text(reply_context.text)}\n"
                "</quoted_message>"
            )
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
        memory_items = "\n".join(
            [f"- {_escape_prompt_text(m['summary'])}" for m in memories]
        )
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
                dynamic_parts.extend(
                    render_untrusted_context(
                        UntrustedContext(
                            source_type="knowledge",
                            source_id=f"chat:{result.source}:{result.id}",
                            content=result.content,
                            trust_level="low",
                        ),
                        max_chars=4_000,
                    )
                    for result in knowledge_results
                )
        except Exception:
            logger.debug("[KomariMemory] 常识库检索失败", exc_info=True)

    # 收集对话中的用户 ID（供常识检索和 read_profile 工具提示使用）
    all_user_ids: set[str] = set()
    visible_users: dict[str, str] = {}
    if recent_messages:
        for msg in recent_messages:
            if not msg.is_bot:
                all_user_ids.add(msg.user_id)
                visible_users[msg.user_id] = character_binding.get_character_name(
                    user_id=msg.user_id,
                    fallback_nickname=msg.user_nickname,
                )
    if current_user_id:
        all_user_ids.add(current_user_id)
        visible_users[current_user_id] = character_binding.get_character_name(
            user_id=current_user_id,
            fallback_nickname=current_user_nickname,
        )
    if (
        reply_context is not None
        and reply_context.source_side == "user"
        and reply_context.user_id
    ):
        all_user_ids.add(reply_context.user_id)
        visible_users[reply_context.user_id] = character_binding.get_character_name(
            user_id=reply_context.user_id,
            fallback_nickname=reply_context.user_nickname or reply_context.user_id,
        )

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
            profile_items = _format_user_keyword_knowledge_yaml(user_profile_results)
            if profile_items:
                dynamic_parts.append(
                    render_untrusted_context(
                        UntrustedContext(
                            source_type="knowledge",
                            source_id="chat:user-keyword-knowledge",
                            content=profile_items,
                            trust_level="low",
                        ),
                        max_chars=4_000,
                    )
                )

    # 当前触发用户画像：只注入当前用户，其他用户由 read_profile 工具按需读取。
    if current_user_profile and profile_traits_to_list(current_user_profile.get("traits")):
        profile_text = _format_profile_traits_yaml(
            current_user_profile,
            fallback_user_id=current_user_id,
            fallback_name=current_user_nickname,
        )
        dynamic_parts.append(
            f"<current_user_profile>\n{profile_text}\n</current_user_profile>"
        )

    visible_users_text = _format_visible_users_yaml(visible_users)
    if visible_users_text:
        dynamic_parts.append(f"<visible_users>\n{visible_users_text}\n</visible_users>")

    interaction_memory_text = _format_interaction_memories_yaml(interaction_memories or [])
    if interaction_memory_text:
        dynamic_parts.append(
            f"<interaction_memory>\n{interaction_memory_text}\n</interaction_memory>"
        )

    recent_interaction_text = _format_interaction_records_yaml(interaction_records or [])
    if recent_interaction_text:
        dynamic_parts.append(
            f"<recent_interaction_history>\n{recent_interaction_text}\n</recent_interaction_history>"
        )

    del memory_service, group_id

    if favorability is not None:
        favor_display_name = (
            character_binding.get_character_name(
                user_id=favorability.user_id,
                fallback_nickname=current_user_nickname,
            )
        )
        dynamic_parts.append(
            "\n".join(
                [
                    "<favorability_stage>",
                    f"当前用户：{_escape_prompt_text(favor_display_name)}",
                    f"好感度：{favorability.favorability}/400",
                    f"阶段：{favorability.stage_index}/4 {_escape_prompt_text(favorability.stage_name)}",
                    f"阶段提示：{_escape_prompt_text(favorability.stage_prompt)}",
                    "</favorability_stage>",
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
    current_text = f"- {_escape_prompt_text(current_character_name)}: <user_input>{_escape_prompt_text(user_message)}</user_input>"

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
                    "<quoted_message "
                    f'side="user" user_id="{_escape_prompt_text(reply_context.user_id or "")}" '
                    f'display_name="{_escape_prompt_text(reply_name)}">\n'
                    f"{_escape_prompt_text(reply_context.text)}\n"
                    "</quoted_message>"
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
