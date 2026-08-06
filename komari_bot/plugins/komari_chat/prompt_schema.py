"""Komari Chat 提示词强类型表 Schema。

本模块无副作用：只依赖 common 层 typed_config，不导入业务插件包、不访问
数据库，可被 Alembic 迁移环境与 ``typed_config`` 安全加载器直接加载。
``DEFAULTS`` 是该资源全部 Prompt 字段默认值的唯一定义来源，运行时模板
加载器（``services/prompt_template.py``）与管理 API 均从这里导入。
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import Text

from komari_bot.common.typed_config import Field, TypedPromptModel

RESOURCE_ID = "komari_chat"
DISPLAY_NAME = "Komari Chat Prompt"

# 默认模板值（PG 配置缺失或读取失败时使用）
DEFAULTS: dict[str, str] = {
    "system_prompt": "你是一个友善的助手。",
    "memory_ack": "好的，我了解了。",
    "memory_ack_role": "assistant",
    "output_instruction": "请将最终回复放在 <content></content> 标签中。",
    "cot_prefix": "<think>\n开始思考。\n",
    "cot_prefix_role": "assistant",
}


class KomariChatPromptSchema(TypedPromptModel, table=True):
    """Komari Chat 提示词强类型表（单行，由 PromptStorage 管理）。

    正文列统一为 TEXT；非空与内容预算校验继续由
    ``validate_prompt_values`` 在写入前承担，不下沉到模型。
    """

    prompt_resource_id: ClassVar[str] = RESOURCE_ID
    __tablename__ = "komari_prompt_komari_chat"

    system_prompt: str = Field(default="", sa_type=Text, description="聊天系统提示词")
    memory_ack: str = Field(default="", sa_type=Text, description="记忆写入确认回复")
    memory_ack_role: str = Field(default="", sa_type=Text, description="记忆确认回复角色")
    output_instruction: str = Field(default="", sa_type=Text, description="输出格式要求")
    cot_prefix: str = Field(default="", sa_type=Text, description="思维链前缀")
    cot_prefix_role: str = Field(default="", sa_type=Text, description="思维链前缀角色")
