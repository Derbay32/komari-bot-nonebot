"""群聊历史总结提示词强类型表 Schema。

本模块无副作用：只依赖 common 层 typed_config，不导入业务插件包、不访问
数据库，可被 Alembic 迁移环境与 ``typed_config`` 安全加载器直接加载。
TSK-191：完整 Prompt 初始正文由版本化初始数据（``seed_bootstrap`` +
``initial_data`` 资产）写入 PostgreSQL，本模块不再定义/导出 Python 长文本
``DEFAULTS``；字段名即强类型 Schema 的自解释契约。
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import Text

from komari_bot.config.typed_config import Field, TypedPromptModel

RESOURCE_ID = "group_history_summary"
DISPLAY_NAME = "Group History Summary Prompt"


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
