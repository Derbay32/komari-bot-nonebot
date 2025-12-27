"""LLM Provider 配置 - 内部默认配置。"""
from pydantic import BaseModel, Field


class Config(BaseModel):
    """LLM Provider 配置"""
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