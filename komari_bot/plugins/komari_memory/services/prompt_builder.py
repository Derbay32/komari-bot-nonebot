"""Komari Memory 动态提示词构建服务。"""

from typing import Any

from nonebot import logger
from nonebot.plugin import require

from ..config_schema import KomariMemoryConfigSchema

# 获取常识库插件
komari_knowledge = require("komari_knowledge")

# 获取角色绑定插件
character_binding = require("character_binding")


async def build_prompt(
    user_message: str,
    memories: list[dict],
    config: KomariMemoryConfigSchema,
    recent_messages: list | None = None,
    current_user_id: str | None = None,
    current_user_nickname: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """构建多轮对话提示词（记忆 + 常识库 + 最近消息）。

    Args:
        user_message: 用户消息
        memories: 检索到的对话记忆
        config: 插件配置
        recent_messages: 最近的消息列表（可选）
        current_user_id: 当前用户 ID（可选）
        current_user_nickname: 当前用户昵称（可选）

    Returns:
        (system_prompt, contents_list) 元组
        - system_prompt: 系统提示词
        - contents_list: contents 列表，每个元素为 {"role": "user"/"model", "parts": [{"text": "..."}]}
    """
    contents: list[dict[str, Any]] = []

    # 第一步：背景注入（User + Model 确认）
    background_parts = []

    # 对话记忆
    if memories:
        memory_items = "\n".join([f"- {m['summary']}" for m in memories])
        background_parts.append(f"<memory>\n{memory_items}\n</memory>")

    # 常识库
    if config.knowledge_enabled:
        try:
            knowledge_results = await komari_knowledge.search_knowledge(
                query=user_message,
                limit=config.knowledge_limit,
            )
            if knowledge_results:
                # 根据 source 字段分组
                keyword_results = [r for r in knowledge_results if r.source == "keyword"]
                vector_results = [r for r in knowledge_results if r.source == "vector"]

                # 分别注入不同来源的知识
                if keyword_results:
                    keyword_items = "\n".join([f"- {r.content}" for r in keyword_results])
                    background_parts.append(
                        f"<keyword_knowledge>\n{keyword_items}\n</keyword_knowledge>"
                    )

                if vector_results:
                    vector_items = "\n".join([f"- {r.content}" for r in vector_results])
                    background_parts.append(
                        f"<vector_knowledge>\n{vector_items}\n</vector_knowledge>"
                    )
        except Exception:
            logger.debug("[KomariMemory] 常识库检索失败", exc_info=True)

    # 如果有背景信息，添加到 contents 并加上确认块
    if background_parts:
        background_text = "\n\n".join(background_parts)
        background_text += f"\n\n{config.background_prompt}"

        contents.append({"role": "user", "parts": [{"text": background_text}]})
        contents.append({"role": "model", "parts": [{"text": config.background_confirmation}]})

    # 第二步：构造历史对话（按时间线，合并 User/Model 侧）
    if recent_messages:
        current_block: list[str] = []
        current_side: str | None = None  # "user" 或 "model"

        for msg in recent_messages:
            this_side = "model" if msg.is_bot else "user"

            # 获取角色名：机器人消息使用配置的 bot_nickname
            if msg.is_bot:
                character_name = config.bot_nickname
            else:
                character_name = character_binding.get_character_name(
                    user_id=msg.user_id,
                    fallback_nickname=msg.user_nickname,
                )

            msg_text = f"- {character_name}: {msg.content}"

            # 切换侧时，保存当前块
            if current_side is not None and this_side != current_side:
                block_text = "\n".join(current_block)
                role = "model" if current_side == "model" else "user"
                contents.append({"role": role, "parts": [{"text": block_text}]})
                current_block = []

            current_block.append(msg_text)
            current_side = this_side

        # 保存最后一个块
        if current_block:
            block_text = "\n".join(current_block)
            role = "model" if current_side == "model" else "user"
            contents.append({"role": role, "parts": [{"text": block_text}]})

    # 第三步：当前请求（User）+ 保持人设保险
    current_character_name = (
        character_binding.get_character_name(
            user_id=current_user_id,
            fallback_nickname=current_user_nickname,
        )
        if current_user_id
        else "用户"
    )

    current_text = f"- {current_character_name}: {user_message}"

    # 保持人设的保险（使用配置中的文本）
    current_text += f"\n\n{config.character_instruction}"

    contents.append({"role": "user", "parts": [{"text": current_text}]})

    # system_prompt 保持不变
    system_prompt = config.system_prompt

    return system_prompt, contents
