from typing import List

from pydantic import BaseModel, Field


class Config(BaseModel):
    """JRHG 插件配置。"""

    # API 提供商选择
    api_provider: str = Field(
        default="deepseek",
        description="API 提供商: deepseek 或 gemini"
    )

    # 默认系统提示词（用于好感度问候）
    default_prompt: str = Field(
        default="你是小鞠，一个可爱的AI助手。请根据你对用户的好感度，用相应的态度和用户打招呼。好感度越高，你的语气应该越热情友好。",
        description="默认的系统提示词"
    )

    # 插件开关
    plugin_enable: bool = Field(
        default=False,
        description="JRHG 插件开关"
    )

    # 白名单配置
    user_whitelist: List[str] = Field(
        default_factory=list,
        description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: List[str] = Field(
        default_factory=list,
        description="群聊白名单，为空则允许所有群聊"
    )
