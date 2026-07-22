"""
llm provider 配置 Schema 实现。
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

ALLOWED_EXTRA_PARAM_KEYS = frozenset(
    {
        "logprobs",
        "min_p",
        "presence_penalty",
        "repetition_penalty",
        "seed",
        "stop",
        "top_k",
        "top_logprobs",
        "top_p",
    }
)


def get_unsupported_extra_param_keys(extra_params: dict[str, Any]) -> list[str]:
    """返回不在安全白名单中的额外请求参数键。"""
    return sorted(set(extra_params) - ALLOWED_EXTRA_PARAM_KEYS)


class DynamicConfigSchema(BaseModel):
    """
    llm provider 配置 Schema。
    """

    model_config = ConfigDict(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    # OpenAI 兼容 API 配置
    api_token: str = Field(
        default="",
        description="OpenAI 兼容 API Token",
        json_schema_extra={"secret": True},
    )
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
    summary_task_rpm_limit: int = Field(
        default=20,
        ge=1,
        le=600,
        description="总结任务 LLM 请求每分钟上限",
    )
    chat_rpm_limit: int = Field(
        default=60,
        ge=1,
        le=600,
        description="聊天 LLM 请求每分钟上限",
    )
    frequency_penalty: float = Field(
        default=0.0, description="OpenAI 兼容 API 重复内容惩罚"
    )
    extra_params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "OpenAI 兼容 API 请求的额外生成参数，仅接受安全白名单中的键。"
            "允许：logprobs、min_p、presence_penalty、repetition_penalty、seed、"
            "stop、top_k、top_logprobs、top_p。"
            "思考模式控制请使用各业务插件的 *_thinking_mode / *_reasoning_effort 字段，"
            "勿在此处配置 thinking/enable_thinking/reasoning_effort。"
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
    vision_thinking_mode: bool = Field(
        default=False,
        description="视觉模型是否处于思考模式（仅在聊天循环 has_vision_tool=True 切到 vision_model 时生效）。",
    )
    vision_reasoning_effort: str = Field(
        default="",
        description="视觉模型思考强度。语义同 komari_memory.llm_reasoning_effort_chat。",
    )

    @field_validator("extra_params")
    @classmethod
    def validate_extra_params(cls, v: dict[str, Any]) -> dict[str, Any]:
        """拒绝可能覆盖消息、模型、工具或传输控制的额外参数。"""
        unsupported = get_unsupported_extra_param_keys(v)
        if unsupported:
            msg = f"extra_params 包含不允许的键: {', '.join(unsupported)}"
            raise ValueError(msg)
        return v
