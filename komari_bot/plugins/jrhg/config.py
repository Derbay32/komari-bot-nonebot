from typing import List

from pydantic import BaseModel, Field


class Config(BaseModel):
    """JRHG插件配置"""

    # DeepSeek API配置
    deepseek_api_url: str = Field(
        default="https://api.deepseek.com/v1/chat/completions",
        description="DeepSeek API URL"
    )
    deepseek_api_token: str = Field(
        description="DeepSeek API Token"
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        description="DeepSeek模型名称"
    )
    deepseek_temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="API调用温度参数"
    )
    deepseek_frequency_penalty: float = Field(
        default=0.0,
        ge=-2.0,
        le=2.0,
        description="API调用频率惩罚参数"
    )
    deepseek_default_prompt: str = Field(
        default="你是小鞠，一个可爱的AI助手。请根据你对用户的好感度，用相应的态度和用户打招呼。好感度越高，你的语气应该越热情友好。",
        description="默认的系统提示词"
    )

    # 插件开关
    jrhg_plugin_enable: bool = Field(
        default=False,
        description="JRHG插件开关"
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
