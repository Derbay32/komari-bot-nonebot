"""Komari Memory LLM 调用服务，封装 llm_provider 插件。"""

from __future__ import annotations

import json
import re
from logging import getLogger
from typing import TYPE_CHECKING

from nonebot.plugin import require
from pydantic import BaseModel, Field, field_validator

from ..config_schema import KomariMemoryConfigSchema  # noqa: TC001
from ..core.retry import retry_async

if TYPE_CHECKING:
    from .redis_manager import MessageSchema

# 依赖 llm_provider 插件
llm_provider = require("llm_provider")

logger = getLogger(__name__)


class EntitySchema(BaseModel):
    """实体结构化输出 Schema。

    Attributes:
        user_id: 实体关联的用户 ID
        key: 实体名称/键
        value: 实体的值或描述
        category: 分类
    """

    user_id: str = Field(description="实体关联的用户ID（从对话中识别）")
    key: str = Field(description="实体名称/键，如'喜欢的食物'、'职业'")
    value: str = Field(description="实体的值，如'拉面'、'程序员'")
    category: str = Field(
        default="general",
        description="分类：preference(偏好)/fact(事实)/relation(关系)/general(一般)",
    )


class ConversationSummarySchema(BaseModel):
    """对话总结结果的结构化输出 Schema。

    Attributes:
        summary: 对话的简明总结
        entities: 提取的关键实体列表
        importance: 重要性评分 (1-5分)
    """

    summary: str = Field(description="对话的简明总结")
    entities: list[EntitySchema] = Field(
        default_factory=list, description="提取的关键实体列表"
    )
    importance: int = Field(ge=1, le=5, description="重要性评分 (1-5分)")

    @field_validator("importance")
    @classmethod
    def validate_importance(cls, v: int) -> int:
        """确保 importance 在合理范围内。"""
        return max(1, min(5, v))


def _extract_json_from_markdown(text: str) -> str:
    """从 markdown 代码块中提取 JSON（保留作为降级方案）。"""
    text = text.strip()

    if not text.startswith("```"):
        return text

    pattern = r"```(?:json)?\s*\n([\s\S]*?)\n```"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    if text.startswith("```"):
        lines = text.split("\n", 1)
        if len(lines) > 1:
            text = lines[1]
        text = text.removesuffix("```")

    return text.strip()


def _extract_tag_content(text: str, tag: str) -> str:
    """从 LLM 回复中提取指定 XML 标签内的内容。

    Args:
        text: LLM 完整回复文本
        tag: 要提取的标签名（如 "content"）

    Returns:
        标签内的文本，未找到标签则返回原始文本（降级）
    """
    pattern = rf"<{tag}>([\s\S]*)</{tag}>"
    match = re.search(pattern, text)
    if match:
        return match.group(1).strip()

    # 降级：未找到标签，返回原文（去掉 <think> 块）
    logger.warning(f"[KomariMemory] 未找到 <{tag}> 标签，使用原始回复")
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


@retry_async(max_attempts=3, base_delay=1.0)
async def generate_reply(
    config: KomariMemoryConfigSchema,
    messages: list[dict[str, str]] | None = None,
    user_message: str = "",
    system_prompt: str = "",
) -> str:
    """生成回复（使用 OpenAI messages 格式，带重试机制）。

    Args:
        config: 插件配置
        messages: OpenAI 格式消息列表 [{role, content}]（优先使用）
        user_message: 用户消息（兼容旧格式）
        system_prompt: 系统提示词（兼容旧格式）

    Returns:
        提取 XML 标签后的最终回复
    """
    if messages is not None:
        raw_response = await llm_provider.generate_text_with_messages(
            messages=messages,
            model=config.llm_model_chat,
            temperature=config.llm_temperature_chat,
            max_tokens=config.llm_max_tokens_chat,
        )
    else:
        # 兼容旧格式
        raw_response = await llm_provider.generate_text(
            prompt=user_message,
            model=config.llm_model_chat,
            system_instruction=system_prompt,
            temperature=config.llm_temperature_chat,
            max_tokens=config.llm_max_tokens_chat,
        )

    # 提取 XML 标签内容
    return _extract_tag_content(raw_response, config.response_tag)


@retry_async(max_attempts=3, base_delay=1.0)
async def summarize_conversation(
    messages: list[MessageSchema],
    config: KomariMemoryConfigSchema,
) -> dict:
    """总结对话，提取实体，并评估重要性（使用结构化输出，带重试机制）。

    Args:
        messages: MessageSchema 消息列表（包含 user_id 和 user_nickname）
        config: 插件配置

    Returns:
        总结结果，包含 summary, entities, importance
    """
    # 格式化消息，包含 user_id 以便 LLM 关联实体到用户
    formatted_messages = []
    for msg in messages:
        if msg.is_bot:
            formatted_messages.append(f"[bot] {config.bot_nickname}: {msg.content}")
        else:
            formatted_messages.append(
                f"[user_id:{msg.user_id}] {msg.user_nickname}: {msg.content}"
            )

    prompt = f"""请总结以下对话，提取关键实体信息（如偏好、事实、关系等），并评估对话的重要性，输出必须使用简体中文。

每条消息格式为 [user_id:xxx] 昵称: 内容，请在提取实体时将 user_id 字段与对应用户关联。

{chr(10).join(formatted_messages)}

实体提取说明：
- 提取用户提到的偏好（喜欢的食物、音乐等）、个人事实（职业、年龄等）、关系（朋友、同事等）
- 每个实体的 user_id 必须来自对话中的 [user_id:xxx]
- category 可选值：preference(偏好)、fact(事实)、relation(关系)、general(一般)

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
        fallback_example = (
            '{"summary": "...", "entities": '
            '[{"user_id": "12345", "key": "喜欢的食物", "value": "拉面", "category": "preference"}], '
            '"importance": 3}'
        )
        prompt_with_format = prompt + f"\n\n返回 JSON 格式：{fallback_example}"

        response = await llm_provider.generate_text(
            prompt=prompt_with_format,
            model=config.llm_model_summary,
            temperature=config.llm_temperature_summary,
            max_tokens=config.llm_max_tokens_summary,
        )

        # 提取 JSON
        json_text = _extract_json_from_markdown(response)
        result = json.loads(json_text)

        # 确保 importance 字段存在且在合理范围内
        if "importance" not in result:
            result["importance"] = 3
        else:
            try:
                importance = int(result["importance"])
                result["importance"] = max(1, min(5, importance))
            except (ValueError, TypeError):
                result["importance"] = 3

        return result
