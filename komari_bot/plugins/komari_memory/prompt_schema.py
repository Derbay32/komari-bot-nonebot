"""Komari Memory 总结提示词强类型表 Schema。

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

RESOURCE_ID = "komari_memory_summary"
DISPLAY_NAME = "Komari Memory Summary Prompt"


class KomariMemorySummaryPromptSchema(TypedPromptModel, table=True):
    """Komari Memory 总结提示词强类型表（单行，由 PromptStorage 管理）。

    正文列统一为 TEXT；非空与内容预算校验继续由
    ``validate_prompt_values`` 在写入前承担，不下沉到模型。
    """

    prompt_resource_id: ClassVar[str] = RESOURCE_ID
    __tablename__ = "komari_prompt_memory_summary"

    memory_summary_common_system: str = Field(
        default="", sa_type=Text, description="记忆总结公共系统提示词"
    )
    profile_agent_workflow_system: str = Field(
        default="", sa_type=Text, description="画像维护 Agent 工作流系统提示词"
    )
    summary_workflow_system: str = Field(
        default="", sa_type=Text, description="对话总结工作流系统提示词"
    )
    json_response_example: str = Field(
        default="", sa_type=Text, description="总结输出 JSON 示例"
    )
