"""Komari Memory LLM 调用服务，封装 llm_provider 插件。"""

import json
import re
from logging import getLogger
from typing import Any

from nonebot.plugin import require
from pydantic import BaseModel, Field, field_validator

from ..config_schema import KomariMemoryConfigSchema
from ..core.retry import retry_async

# 依赖 llm_provider 插件
llm_provider = require("llm_provider")

logger = getLogger(__name__)


class ConversationSummarySchema(BaseModel):
    """对话总结结果的结构化输出 Schema。

    Attributes:
        summary: 对话的简明总结
        entities: 提取的关键实体列表
        importance: 重要性评分 (1-5分)
    """

    summary: str = Field(description="对话的简明总结")
    entities: list[str] = Field(default_factory=list, description="提取的关键实体列表")
    importance: int = Field(ge=1, le=5, description="重要性评分 (1-5分)")

    @field_validator("importance")
    @classmethod
    def validate_importance(cls, v: int) -> int:
        """确保 importance 在合理范围内。

        Args:
            v: 原始重要性值

        Returns:
            验证后的重要性值
        """
        return max(1, min(5, v))


def _extract_json_from_markdown(text: str) -> str:
    """从 markdown 代码块中提取 JSON（保留作为降级方案）。

    Args:
        text: 可能包含 markdown 代码块的文本

    Returns:
        纯 JSON 字符串
    """
    # 移除开头和结尾的空白
    text = text.strip()

    # 尝试直接解析（如果不是 markdown 格式）
    if not text.startswith("```"):
        return text

    # 提取 markdown 代码块中的内容
    # 匹配 ```json 或 ``` 后面的内容，直到下一个 ```
    pattern = r"```(?:json)?\s*\n([\s\S]*?)\n```"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    # 如果没有匹配到，尝试移除开头的 ``` 和结尾的 ```
    if text.startswith("```"):
        lines = text.split("\n", 1)
        if len(lines) > 1:
            text = lines[1]
        text = text.removesuffix("```")

    return text.strip()


@retry_async(max_attempts=3, base_delay=1.0)
async def generate_reply(
    user_message: str,
    system_prompt: str,
    config: KomariMemoryConfigSchema,
    contents_list: list[dict[str, Any]] | None = None,
) -> str:
    """生成回复（使用对话模型，带重试机制）。

    Args:
        user_message: 用户消息（兼容旧格式）
        system_prompt: 系统提示词
        config: 插件配置
        contents_list: contents 列表（可选，优先使用）

    Returns:
        生成的回复
    """
    # 如果提供了 contents_list，使用新的多轮对话格式
    if contents_list is not None:
        return await llm_provider.generate_text_with_contents(
            contents=contents_list,
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

    # 兼容旧的字符串格式
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
    """总结对话，提取实体，并评估重要性（使用结构化输出，带重试机制）。

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
- 5分：重要的决定、约定、或有价值的讨论"""

    # 首先尝试使用结构化输出
    try:
        response = await llm_provider.generate_text(
            prompt=prompt,
            provider=config.llm_provider,
            model=config.llm_model_summary,
            temperature=config.llm_temperature_summary,
            max_tokens=config.llm_max_tokens_summary,
            response_schema=ConversationSummarySchema,
        )

        # 解析结构化响应
        result = ConversationSummarySchema.model_validate_json(response)
        return result.model_dump()

    except Exception as e:
        # 结构化输出失败，使用传统方法作为降级方案
        logger.warning(f"结构化输出失败，使用传统方法: {e}")

        # 在 prompt 中添加 JSON 格式要求
        prompt_with_format = (
            prompt + '\n\n返回 JSON 格式：{"summary": "...", "entities": [...], "importance": 3}'
        )

        response = await llm_provider.generate_text(
            prompt=prompt_with_format,
            provider=config.llm_provider,
            model=config.llm_model_summary,
            temperature=config.llm_temperature_summary,
            max_tokens=config.llm_max_tokens_summary,
        )

        # 提取 JSON（处理 LLM 可能返回的 markdown 代码块格式）
        json_text = _extract_json_from_markdown(response)
        result = json.loads(json_text)

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
