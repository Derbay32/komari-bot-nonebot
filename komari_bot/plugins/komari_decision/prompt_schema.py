"""Jev 判定提示词强类型表 Schema。

本模块无副作用：只依赖 common 层 typed_config，不导入业务插件包、不访问
数据库，可被 Alembic 迁移环境与 ``typed_config`` 安全加载器直接加载。

TSK-182：四个正文段落（角色描述、回复资格、保留目的、总结识别）由既有
强类型管理接口整体读写，字段名即自解释契约。版本化初始数据资产负责正文
初始内容，本模块只声明空默认值，不内嵌任何长文本默认内容。
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import Text

from komari_bot.config.typed_config import Field, TypedPromptModel

RESOURCE_ID = "komari_decision"
DISPLAY_NAME = "Komari Decision Prompt"


class KomariDecisionPromptSchema(TypedPromptModel, table=True):
    """Jev 判定提示词强类型表（单行，由 PromptStorage 管理）。

    正文列统一为 TEXT；非空与内容预算校验继续由
    ``validate_prompt_values`` 在写入前承担，不下沉到模型。
    """

    prompt_resource_id: ClassVar[str] = RESOURCE_ID
    __tablename__ = "komari_prompt_komari_decision"

    role_description: str = Field(
        default="",
        sa_type=Text,
        description="判定角色描述：模型如何理解自己在群内的身份与职责",
    )
    reply_eligibility: str = Field(
        default="",
        sa_type=Text,
        description="回应资格说明：哪些情形必须回应、可选参与或保持沉默",
    )
    retention_purpose: str = Field(
        default="",
        sa_type=Text,
        description="保留用途说明：消息是否具备后续记忆整理的候选资格",
    )
    summary_recognition: str = Field(
        default="",
        sa_type=Text,
        description="群总结识别说明：当前消息是否要求总结本群聊天",
    )
