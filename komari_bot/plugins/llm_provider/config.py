"""LLM Provider 配置 - 内部默认配置。"""
from pydantic import BaseModel, Field


class Config(BaseModel):
    """LLM Provider 配置"""

    # DeepSeek 配置
    deepseek_api_token: str = Field(
        default="",
        description="DeepSeek API Token"
        )
    deepseek_api_base: str = Field(
        default="https://api.deepseek.com/v1/chat/completions",
        description="DeepSeek API 地址"
    )
    deepseek_model: str = Field(
        default="deepseek-chat",
        description="DeepSeek 使用模型"
    )
    deepseek_temperature: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="DeepSeek 调用温度参数"
    )
    deepseek_max_tokens: float = Field(
        default=200,
        ge=20,
        le=500,
        description="DeepSeek 最大token数量"
    )
    deepseek_frequency_penalty: float = Field(
        default=0.0,
        description="DeepSeek 重复内容惩罚"
    )

    # Gemini 配置
    gemini_api_token: str = Field(
        default="",
        description="Gemini API Token"
        )
    gemini_model: str = Field(
        default="gemini-2.5-flash",
        description="Gemini 使用模型"
    )
    gemini_temperature: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="Gemini 调用温度参数"
    )
    gemini_max_tokens: float = Field(
        default=200,
        ge=20,
        le=500,
        description="Gemini 最大token数量"
    )
