"""Komari Memory LLM 调用服务，封装 llm_provider 插件。"""

import asyncio
import json

from nonebot import logger
from nonebot.plugin import require

from ..config_schema import KomariMemoryConfigSchema

# 依赖 llm_provider 插件
llm_provider = require("llm_provider")


async def generate_reply(
    user_message: str,
    system_prompt: str,
    config: KomariMemoryConfigSchema,
) -> str | None:
    """生成回复（使用对话模型，带重试机制）。

    Args:
        user_message: 用户消息
        system_prompt: 系统提示词
        config: 插件配置

    Returns:
        生成的回复，失败时返回 None
    """
    last_error = None

    for attempt in range(3):  # 总共尝试 3 次
        try:
            return await llm_provider.generate_text(
                prompt=user_message,
                provider=config.llm_provider,
                model=config.llm_model_chat,  # 对话专用模型
                system_instruction=system_prompt,
                temperature=config.llm_temperature_chat,
                max_tokens=config.llm_max_tokens_chat,
            )
        except Exception as e:
            last_error = e
            if attempt < 2:  # 前 2 次失败后重试
                logger.warning(
                    f"[LLMService] 回复生成第 {attempt + 1} 次失败: {e}，重试中..."
                )
                await asyncio.sleep(1.0 * (attempt + 1))  # 指数退避
            else:
                logger.error(f"[LLMService] 回复生成 3 次全部失败: {last_error}")

    # 所有尝试失败，返回 None
    return None


async def summarize_conversation(
    messages: list[str],
    config: KomariMemoryConfigSchema,
) -> dict:
    """总结对话，提取实体，并评估重要性（使用总结模型，带重试机制）。

    Args:
        messages: 消息列表
        config: 插件配置

    Returns:
        总结结果，包含 summary, entities, importance
    """
    prompt = f"""请总结以下对话，提取实体信息，并评估对话的重要性：

{chr(10).join(messages)}

请按以下标准评估重要性（1-5分）：
- 1分：无意义的闲聊、表情、问候
- 2分：简单的日常对话
- 3分：一般的讨论交流
- 4分：有意义的话题讨论
- 5分：重要的决定、约定、或有价值的讨论

返回 JSON 格式：{{"summary": "...", "entities": [...], "importance": 3}}"""

    last_error = None

    for attempt in range(3):  # 总共尝试 3 次
        try:
            response = await llm_provider.generate_text(
                prompt=prompt,
                provider=config.llm_provider,
                model=config.llm_model_summary,  # 总结专用模型（不同于对话模型）
                temperature=config.llm_temperature_summary,
                max_tokens=config.llm_max_tokens_summary,
            )
            result = json.loads(response)
            logger.debug(f"[LLMService] 总结成功 (尝试 {attempt + 1}/3)")
        except (json.JSONDecodeError, Exception) as e:
            last_error = e
            if attempt < 2:  # 前 2 次失败后重试
                logger.warning(
                    f"[LLMService] 总结第 {attempt + 1} 次失败: {e}，重试中..."
                )
                await asyncio.sleep(1.0 * (attempt + 1))  # 指数退避
            else:
                logger.error(f"[LLMService] 总结 3 次全部失败: {last_error}")
        else:
            # 确保 importance 字段存在且在合理范围内
            if "importance" not in result:
                result["importance"] = 3  # 默认值
            else:
                try:
                    importance = int(result["importance"])
                    result["importance"] = max(1, min(5, importance))
                except (ValueError, TypeError):
                    result["importance"] = 3
            return result

    # 所有尝试失败，返回默认总结
    return {"summary": "对话总结暂时不可用", "entities": [], "importance": 3}
