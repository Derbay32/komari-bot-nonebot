"""Komari Chat 提示词强类型表 Schema。

本模块无副作用：只依赖 common 层 typed_config，不导入业务插件包、不访问
数据库，可被 Alembic 迁移环境与 ``typed_config`` 安全加载器直接加载。

TSK-190：聊天 Prompt 的完整初始正文由版本化初始数据（``seed_bootstrap``
+ ``initial_data`` 资产）写入 PostgreSQL，本模块不再提供 Python 长文本
``DEFAULTS``。字段名即强类型 Schema 的自解释契约：

- ``tool_call_instruction`` / ``image_read_instruction`` 是 TSK-188 决策 26
  精确点名的行为字段；
- 画像读取、联网搜索、网页抓取、委托图片理解、视觉描述各自拥有互相独立
  的行为字段（``profile_read_instruction`` / ``search_web_instruction`` /
  ``fetch_page_instruction`` / ``delegated_vision_instruction`` /
  ``vision_description_prompt``）；
- 工具名称、工具 JSON Schema、参数错误、动态图片索引与短协议纠错仍由
  代码拥有，不进入数据库。

``output_instruction`` 已被删除：旧 XML 最终输出协议不再作为聊天 Prompt
的组成部分，被删除的自定义内容不并入任何新字段。
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy import Text

from komari_bot.config.typed_config import Field, TypedPromptModel

RESOURCE_ID = "komari_chat"
DISPLAY_NAME = "Komari Chat Prompt"


class KomariChatPromptSchema(TypedPromptModel, table=True):
    """Komari Chat 提示词强类型表（单行，由 PromptStorage 管理）。

    正文列统一为 TEXT；非空与内容预算校验继续由
    ``validate_prompt_values`` 在写入前承担，不下沉到模型。初始值由
    版本化初始数据播种，运行时绝不回退到 Python 长文本默认值。
    """

    prompt_resource_id: ClassVar[str] = RESOURCE_ID
    __tablename__ = "komari_prompt_komari_chat"

    system_prompt: str = Field(default="", sa_type=Text, description="聊天系统提示词（角色/文风）")
    memory_ack: str = Field(default="", sa_type=Text, description="记忆写入确认回复")
    memory_ack_role: str = Field(default="", sa_type=Text, description="记忆确认回复角色")
    tool_call_instruction: str = Field(default="", sa_type=Text, description="工具调用行为引导")
    image_read_instruction: str = Field(default="", sa_type=Text, description="图片读取行为引导")
    profile_read_instruction: str = Field(default="", sa_type=Text, description="画像读取行为引导")
    search_web_instruction: str = Field(default="", sa_type=Text, description="联网搜索行为引导")
    fetch_page_instruction: str = Field(default="", sa_type=Text, description="网页抓取行为引导")
    delegated_vision_instruction: str = Field(default="", sa_type=Text, description="委托图片理解行为引导")
    vision_description_prompt: str = Field(default="", sa_type=Text, description="视觉描述 Prompt")
    cot_prefix: str = Field(default="", sa_type=Text, description="思维链前缀")
    cot_prefix_role: str = Field(default="", sa_type=Text, description="思维链前缀角色")
