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
) -> str:
    """构建动态提示词（记忆 + 常识库）。

    Args:
        user_message: 用户消息
        memories: 检索到的对话记忆
        config: 插件配置

    Returns:
        构建好的提示词
    """
    context_parts = []

    # 1. 注入对话记忆
    if memories:
        memory_text = "\n".join([f"- {m['summary']}" for m in memories])
        context_parts.append(f"对话记忆:\n{memory_text}")

    # 2. 追加常识库检索
    if config.knowledge_enabled:
        try:
            knowledge_results = await komari_knowledge.search(
                query=user_message,
                limit=config.knowledge_limit,
            )
            if knowledge_results:
                knowledge_text = "\n".join(
                    [f"- {r.content}" for r in knowledge_results]
                )
                context_parts.append(f"常识库:\n{knowledge_text}")
        except Exception as e:
            logger.debug(f"[KomariMemory] 常识库检索失败: {e}")

    # 3. 组合上下文
    if context_parts:
        context = "\n\n".join(context_parts)
        prompt = config.memory_injection_template.replace("{{CONTEXT}}", context)
    else:
        prompt = ""

    # 4. 组合系统提示词
    return f"{config.system_prompt}\n\n{prompt}".strip()
