"""Komari Memory 动态提示词构建服务。"""

from nonebot import logger
from nonebot.plugin import require

from ..config_schema import KomariMemoryConfigSchema

# 获取常识库插件
komari_knowledge = require("komari_knowledge")


async def build_prompt(
    user_message: str,
    memories: list[dict],
    config: KomariMemoryConfigSchema,
    recent_messages: list | None = None,
) -> tuple[str, str]:
    """构建动态提示词（记忆 + 常识库 + 最近消息）。

    Args:
        user_message: 用户消息
        memories: 检索到的对话记忆
        config: 插件配置
        recent_messages: 最近的消息列表（可选）

    Returns:
        (system_prompt, user_context) 元组
        - system_prompt: 系统提示词
        - user_context: 用户上下文（包含最近消息、记忆、常识库、用户输入）
    """
    context_parts = []

    # 1. 注入最近消息（使用 XML 标签）
    if recent_messages:
        message_items = [
            f"- {msg.user_id}: {msg.content}" for msg in recent_messages[-10:]
        ]  # 只取最近 10 条
        if message_items:
            messages_text = "\n".join(message_items)
            context_parts.append(
                f"<recent_messages>\n{messages_text}\n</recent_messages>"
            )

    # 2. 注入对话记忆（使用 XML 标签）
    if memories:
        memory_items = "\n".join([f"- {m['summary']}" for m in memories])
        context_parts.append(f"<memory>\n{memory_items}\n</memory>")

    # 3. 追加常识库检索（按来源分别注入）
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
                    keyword_items = "\n".join(
                        [f"- {r.content}" for r in keyword_results]
                    )
                    context_parts.append(
                        f"<keyword_knowledge>\n{keyword_items}\n</keyword_knowledge>"
                    )

                if vector_results:
                    vector_items = "\n".join(
                        [f"- {r.content}" for r in vector_results]
                    )
                    context_parts.append(
                        f"<vector_knowledge>\n{vector_items}\n</vector_knowledge>"
                    )
        except Exception as e:
            logger.debug(f"[KomariMemory] 常识库检索失败: {e}")

    # 4. 构建用户上下文（包含最近消息、记忆、常识库、用户输入）
    user_context_parts = []

    if context_parts:
        user_context_parts.extend(context_parts)

    # 用户当前输入
    user_context_parts.append(f"<user_input>\n{user_message}\n</user_input>")

    user_context = "\n\n".join(user_context_parts)

    # 5. 构建系统提示词（添加标签说明）
    tag_instructions = """
回复时请参考以下上下文信息：
- <recent_messages>标签内是最近的聊天消息（用户ID: 消息内容）
- <memory>标签内是历史对话总结
- <keyword_knowledge>标签内是关键词精确匹配的常识库信息
- <vector_knowledge>标签内是向量语义检索的常识库信息
- <user_input>标签内是用户当前的消息
"""

    system_prompt = f"{config.system_prompt}\n{tag_instructions}".strip()

    return system_prompt, user_context
