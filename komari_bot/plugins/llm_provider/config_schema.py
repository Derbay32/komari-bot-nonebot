"""
llm provider 配置 Schema 实现。
"""

import re
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
    reasoning_effort: str = Field(
        default="",
        description=(
            "OpenAI 兼容 API 请求的 reasoning_effort。"
            "可选：none/minimal/low/medium/high/xhigh；为空时不发送"
        ),
    )
    frequency_penalty: float = Field(
        default=0.0, description="OpenAI 兼容 API 重复内容惩罚"
    )
    extra_params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "OpenAI 兼容 API 请求的额外自定义参数，"
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

    # LLM 调用日志配置
    llm_log_retention_days: int = Field(
        default=30,
        ge=1,
        le=90,
        description="logs/llm_provider/*.jsonl 自动清理的日志保留天数",
    )
    llm_log_dir_permission_mode: str = Field(
        default="0o700",
        description=(
            "LLM 日志目录首次创建时应用的八进制权限字符串，"
            "例如 0o700、0o750；为空字符串时禁用 chmod 权限收敛"
        ),
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

    @field_validator("llm_log_dir_permission_mode")
    @classmethod
    def validate_log_dir_permission_mode(cls, v: str) -> str:
        """校验 LLM 日志目录权限模式。"""
        if v == "":
            return v
        if not re.fullmatch(r"0o[0-7]{3,4}", v):
            msg = "llm_log_dir_permission_mode 必须为空或八进制权限字符串"
            raise ValueError(msg)
        return v
