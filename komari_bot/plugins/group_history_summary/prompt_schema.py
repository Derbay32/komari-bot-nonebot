"""群聊历史总结提示词强类型表 Schema。

本模块无副作用：只依赖 common 层 typed_config，不导入业务插件包、不访问
数据库，可被 Alembic 迁移环境与 ``typed_config`` 安全加载器直接加载。
``DEFAULTS`` 是该资源全部 Prompt 字段默认值的唯一定义来源，运行时模板
加载器（``prompt_template.py``）与管理 API 均从这里导入。
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import Text

from komari_bot.config.typed_config import Field, TypedPromptModel

RESOURCE_ID = "group_history_summary"
DISPLAY_NAME = "Group History Summary Prompt"

# 默认模板值（PG 配置缺失或读取失败时使用）
DEFAULTS: dict[str, str] = {
    "system_prompt": "你是一个专业的群聊总结助手，只基于聊天记录归纳事实。",
    "planning_system_prompt": (
        "你是一个群聊消息检索助手。"
        "你的任务是根据用户的总结请求，决定需要获取哪些聊天记录。"
        "你可以调用工具来获取群聊消息，请根据用户需求选择合适的工具和参数。"
        "获取到足够消息后，简短说明规划完成即可。"
    ),
    "memory_ack": "已收到聊天记录，我先梳理重点。",
    "memory_ack_role": "assistant",
    "output_instruction": (
        "请仅输出总结正文，使用 <content></content> 包裹。"
        "正文控制在 120-220 字，尽量清晰、紧凑、客观。"
    ),
    "cot_prefix": "<think>\n我先按时间梳理讨论脉络，再输出总结。\n",
    "cot_prefix_role": "assistant",
}


class GroupHistorySummaryPromptSchema(TypedPromptModel, table=True):
    """群聊历史总结提示词强类型表（单行，由 PromptStorage 管理）。

    正文列统一为 TEXT；非空与内容预算校验继续由
    ``validate_prompt_values`` 在写入前承担，不下沉到模型。
    """

    prompt_resource_id: ClassVar[str] = RESOURCE_ID
    __tablename__ = "komari_prompt_group_history_summary"

    system_prompt: str = Field(default="", sa_type=Text, description="总结系统提示词")
    planning_system_prompt: str = Field(
        default="", sa_type=Text, description="消息检索规划系统提示词"
    )
    memory_ack: str = Field(default="", sa_type=Text, description="记忆写入确认回复")
    memory_ack_role: str = Field(default="", sa_type=Text, description="记忆确认回复角色")
    output_instruction: str = Field(default="", sa_type=Text, description="输出格式要求")
    cot_prefix: str = Field(default="", sa_type=Text, description="思维链前缀")
    cot_prefix_role: str = Field(default="", sa_type=Text, description="思维链前缀角色")
