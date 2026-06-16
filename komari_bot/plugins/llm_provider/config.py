"""LLM Provider 配置 - 内部默认配置。"""

from pydantic import BaseModel, Field


class Config(BaseModel):
    """LLM Provider 配置"""

    # OpenAI 兼容 API 配置
    api_token: str = Field(default="", description="OpenAI 兼容 API Token")
    api_base: str = Field(
        default="https://api.deepseek.com/v1",
        description="OpenAI 兼容 API Base URL",
    )
    model: str = Field(
        default="deepseek-chat", description="OpenAI 兼容 API 使用模型"
    )
    temperature: float = Field(
        default=1.0, ge=0.0, le=2.0, description="OpenAI 兼容 API 调用温度参数"
    )
    max_tokens: int = Field(
        default=8192, ge=20, le=8192, description="OpenAI 兼容 API 最大 token 数量"
    )
    timeout_seconds: float = Field(
        default=300.0, gt=0.0, description="OpenAI 兼容 API 请求总超时时间（秒）"
    )
    frequency_penalty: float = Field(
        default=0.0, description="OpenAI 兼容 API 重复内容惩罚"
    )
