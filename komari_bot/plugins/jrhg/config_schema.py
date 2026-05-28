"""
JRHG 插件的动态配置 Schema。
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """JRHG 插件的动态配置。

    此模型表示可在运行时修改并在机器人重启后持久化的配置。
    """

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="JRHG 插件启用状态")

    # 白名单配置
    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊"
    )

    # LLM 配置
    llm_model: str = Field(default="deepseek-chat", description="JRHG 使用的模型名称")
    llm_temperature: float = Field(
        default=1.0,
        ge=0.0,
        le=2.0,
        description="JRHG 调用模型时的温度参数",
    )
    llm_max_tokens: int = Field(
        default=8192,
        ge=1,
        le=8192,
        description="JRHG 调用模型时的最大输出 token 数",
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
