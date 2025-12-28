"""
配置 sr 插件的 Schema 实现。
"""

from datetime import datetime
from typing import List

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """SR 插件的配置 Schema。

    此模型表示可在运行时修改并在机器人重启后持久化的配置。
    """

    # 元数据
    version: str = Field(
        default="1.0",
        description="配置架构版本"
        )
    
    last_updated: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="最后更新时间戳"
    )

    # 插件控制
    plugin_enable: bool = Field(
        default=False,
        description="插件启用状态"
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

    # sr 数据配置
    sr_list: List[str] = Field(
        default_factory=list,
        description="安科神人榜列表"
    )
    list_chunk_size: int = Field(
        default=20,
        ge=5,
        le=50,
        description="list 命令每页显示条数"
    )

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
