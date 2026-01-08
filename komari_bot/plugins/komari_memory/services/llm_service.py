"""Komari Memory LLM 调用服务，封装 llm_provider 插件。"""

import json

from nonebot.plugin import require

from ..config_schema import KomariMemoryConfigSchema
from ..core.retry import retry_async

# 依赖 llm_provider 插件
llm_provider = require("llm_provider")


@retry_async(max_attempts=3, base_delay=1.0)
async def generate_reply(
    user_message: str,
    system_prompt: str,
    config: KomariMemoryConfigSchema,
) -> str:
    """生成回复（使用对话模型，带重试机制）。

    Args:
        user_message: 用户消息
        system_prompt: 系统提示词
        config: 插件配置

    Returns:
        生成的回复
    """
    return await llm_provider.generate_text(
        prompt=user_message,
        provider=config.llm_provider,
        model=config.llm_model_chat,  # 对话专用模型
        system_instruction=system_prompt,
        temperature=config.llm_temperature_chat,
        max_tokens=config.llm_max_tokens_chat,
        thinking_token=config.gemini_thinking_token  # 判断是不是 gemini3 或者以上的模型
        if config.llm_model_chat
        not in config.gemini_level_models  # deepseek 那边也会有参数但没定义，少写个判断
        else None,
        thinking_level=config.gemini_thinking_level
        if config.llm_model_chat in config.gemini_level_models
        else None,
    )


@retry_async(max_attempts=3, base_delay=1.0)
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

    # 总结没必要思考纯浪费钱
    response = await llm_provider.generate_text(
        prompt=prompt,
        provider=config.llm_provider,
        model=config.llm_model_summary,  # 总结专用模型（不同于对话模型）
        temperature=config.llm_temperature_summary,
        max_tokens=config.llm_max_tokens_summary,
    )

    result = json.loads(response)

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
