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
        user_interactions: 用户互动历史（小鞠的主观视角记录）
        importance: 重要性评分 (1-5分)
    """

    summary: str = Field(description="对话的简明总结")
    entities: list[EntitySchema] = Field(
        default_factory=list, description="提取的关键实体列表"
    )
    user_interactions: list[dict] = Field(
        default_factory=list, description="用户互动历史（小鞠的主观视角备忘录）"
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
    messages: list[dict] | None = None,
    user_message: str = "",
    system_prompt: str = "",
) -> str:
    """生成回复（使用 OpenAI messages 格式，带重试机制，支持多模态）。

    Args:
        config: 插件配置
        messages: OpenAI 格式消息列表 [{role, content}]（优先使用），content 可以是字符串或数组
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
    existing_entities: list[dict] | None = None,
    existing_interactions: list[dict] | None = None,
) -> dict:
    """总结对话，提取实体，并评估重要性（使用结构化输出，带重试机制）。

    Args:
        messages: MessageSchema 消息列表（包含 user_id 和 user_nickname）
        config: 插件配置
        existing_entities: 已存储的实体列表，用于 LLM 感知已有信息并执行更新
        existing_interactions: 已存储的互动历史列表，用于 LLM 在已有记录上追加

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

    # 格式化已知实体信息
    existing_context = ""
    if existing_entities:
        entity_lines = []
        for e in existing_entities:
            uid = e.get("user_id", "unknown")
            key = e.get("key", "")
            value = e.get("value", "")
            category = e.get("category", "general")
            entity_lines.append(f"- [user_id:{uid}] {key} = {value} ({category})")
        existing_context += "【已知实体信息（数据库中已有记录）】\n"
        existing_context += "以下是目前已存储的用户实体：\n"
        existing_context += "\n".join(entity_lines) + "\n\n"

    if existing_interactions:
        interaction_lines = []
        for i in existing_interactions:
            uid = i.get("user_id", "unknown")
            value = i.get("value", "{}")
            interaction_lines.append(f"- [user_id:{uid}] interaction_history: {value}")
        existing_context += "以下是目前已存储的用户互动历史：\n"
        existing_context += "\n".join(interaction_lines) + "\n\n"

    if existing_context:
        existing_context += (
            "【重要指示】\n"
            "- 如果对话中发现与已有实体矛盾的新信息，请用新信息覆盖旧值（使用相同的 key）\n"
            "- 如果对话中没有提到某个已有实体，不要在输出中重复它\n"
            "- 只输出需要新增或更新的实体\n"
            "- 对于互动历史，请在已有记录的基础上追加新的 records（注意：如果 records 总数超过6条，请只保留最近的6条记录）\n\n"
        )

    prompt = f"""请总结以下群聊或私聊对话，提取关键实体信息（如偏好、事实、关系等），并评估对话的重要性。输出必须使用简体中文。

每条消息格式为 [user_id:xxx] 昵称: 内容。请你在提取时将 user_id 准确关联。

{chr(10).join(formatted_messages)}

{existing_context}【任务一：客观信息提取】
- 提取对话的核心内容，形成 summary（简短总结）。
- 提取用户提到的偏好（喜欢的食物、音乐等）、个人事实（职业、年龄等）、关系（朋友、同事等），作为 entities。（category 选：preference/fact/relation/general）

【任务二：主观互动备忘录提取】
- 你必须基于《败犬女主太多了！》中"小鞠知花"的人设视角，为有明显互动行为的用户，提取出在互动期间该用户的行为记录。这将被作为"小鞠在心里对近期互动过的用户的悄悄记录"。
- 数据格式要求如下：必须包含 user_id, file_type, description, records(包括 event[行为], result[反应], emotion[感受]), summary。

【任务三：评估重要性】
请按以下标准评估重要性（1-5分）：
- 1分：无意义的闲聊、表情包测试、简短问候
- 2分：简单的日常对话
- 3分：一般的讨论交流
- 4分：有意义的话题讨论或较深的互动
- 5分：重要的决定、约定、深度的设定或情感交流"""

    # 在 prompt 中添加 JSON 格式要求
    fallback_example = (
        '{"summary": "...", "entities": '
        '[{"user_id": "12345", "key": "喜欢的食物", "value": "拉面", "category": "preference"}], '
        '"user_interactions": [{"user_id": "12345", "file_type": "用户的近期对鞠行为备忘录", '
        '"description": "这是我在心里对这个用户近期行为的悄悄记录。用来提醒自己这个人平时是怎么对我的，下次和他说话时应该保持什么态度。", '
        '"records": [{"event": "用好吃的诱惑我", "result": "咽了口水，稍微凑近了过去", "emotion": "有点警惕但很想吃"}], '
        '"summary": "是个经常用食物钓我的骗子先生……但也不是坏人。"}], '
        '"importance": 3}'
    )
    prompt_with_format = prompt + f"\n\n请严格返回以下 JSON 格式：\n{fallback_example}"

    response = await llm_provider.generate_text(
        prompt=prompt_with_format,
        model=config.llm_model_summary,
        temperature=config.llm_temperature_summary,
        max_tokens=config.llm_max_tokens_summary,
    )

    # 提取 JSON
    json_text = _extract_json_from_markdown(response)
    result = json.loads(json_text)

    # 限制互动历史（records）最多保留最近6条，防止上下文无限追加
    if "user_interactions" in result and isinstance(result["user_interactions"], list):
        for interaction in result["user_interactions"]:
            if (
                "records" in interaction
                and isinstance(interaction["records"], list)
                and len(interaction["records"]) > 6
            ):
                interaction["records"] = interaction["records"][-6:]

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
