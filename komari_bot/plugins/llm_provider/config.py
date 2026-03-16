"""LLM Provider 配置 - 内部默认配置。"""

from pydantic import BaseModel, Field


class Config(BaseModel):
    """LLM Provider 配置"""

    # DeepSeek 配置
    deepseek_api_token: str = Field(default="", description="DeepSeek API Token")
    deepseek_api_base: str = Field(
        default="https://api.deepseek.com/v1/chat/completions",
        description="DeepSeek API 地址",
    )
    deepseek_model: str = Field(
        default="deepseek-chat", description="DeepSeek 使用模型"
    )
    deepseek_temperature: float = Field(
        default=1.0, ge=0.0, le=2.0, description="DeepSeek 调用温度参数"
    )
    deepseek_max_tokens: int = Field(
        default=8192, ge=20, le=8192, description="DeepSeek 最大token数量"
    )
    deepseek_timeout_seconds: float = Field(
        default=300.0, gt=0.0, description="DeepSeek 请求总超时时间（秒）"
    )
    deepseek_frequency_penalty: float = Field(
        default=0.0, description="DeepSeek 重复内容惩罚"
    )
