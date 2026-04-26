"""
llm provider 配置 Schema 实现。
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """
    llm provider 配置 Schema。
    """

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="插件启用状态")

    # 白名单配置
    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊"
    )

    # DeepSeek 配置
    deepseek_api_token: str = Field(default="", description="DeepSeek API Token")
    deepseek_api_base: str = Field(
        default="https://api.deepseek.com/v1",
        description="DeepSeek OpenAI 兼容 API Base URL",
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
    deepseek_reasoning_effort: str = Field(
        default="",
        description=(
            "DeepSeek OpenAI 兼容请求的 reasoning_effort。"
            "可选：none/minimal/low/medium/high/xhigh；为空时不发送"
        ),
    )
    deepseek_frequency_penalty: float = Field(
        default=0.0, description="DeepSeek 重复内容惩罚"
    )
    deepseek_extra_params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "DeepSeek API 请求的额外自定义参数，"
            "会合并到每次请求体中。支持简单值和嵌套结构。"
            '例如：{"enable_thinking": false} 或 {"thinking": {"type": "disabled"}}'
        ),
    )
    vision_model: str = Field(
        default="gemini-2.0-flash-exp",
        description="多模态视觉模型名",
    )
    vision_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="视觉模型温度",
    )
    vision_max_tokens: int = Field(
        default=1024,
        ge=20,
        le=8192,
        description="视觉模型最大 token 数",
    )

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, v: Any) -> Any:
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
