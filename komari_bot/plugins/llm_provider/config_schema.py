"""
llm provider 配置 Schema 实现。
"""

from datetime import datetime
from typing import List

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """
    示例配置 Schema。
    """

    # 元数据∂
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="最后更新时间戳"
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="插件启用状态")

    # 白名单配置
    user_whitelist: List[str] = Field(
        default_factory=list,
        description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: List[str] = Field(
        default_factory=list,
        description="群聊白名单，为空则允许所有群聊"
    )

# DeepSeek 默认配置
    deepseek_api_base: str = Field(
        default="https://api.deepseek.com/v1/chat/completions",
        description="Deepseek"
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        description="deepseek 使用模型"
    )
    deepseek_temperature: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="deepseek 调用温度参数"
    )
    deepseek_max_tokens: float = Field(
        default=200,
        ge=20,
        le=500,
        description="deepseek 最大token数量"
    )
    deepseek_frequency_penalty: float = Field(
        default=0.0,
        description="deepseek 重复内容惩罚"
    )


    # Gemini 默认配置
    gemini_api_base: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        description=""
    )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description=""
    )
    gemini_temperature: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="deepseek 调用温度参数"
    )
    gemini_max_tokens: float = Field(
        default=200,
        ge=20,
        le=500,
        description="deepseek 最大token数量"
    )

    # 你的自定义字段
    # custom_field: str = Field(default="", description="自定义配置")

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, v):
        """处理从 .env 格式解析列表。

        Args:
            v: 输入值，可能是字符串或列表

        Returns:
            解析后的字符串列表
        """
        if isinstance(v, str):
            import json

            try:
                parsed = json.loads(v)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in v.split(",") if item.strip()]
        return v
