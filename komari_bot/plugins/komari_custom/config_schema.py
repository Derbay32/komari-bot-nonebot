"""komari_custom 插件动态配置 Schema。"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import field_validator
from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.config.typed_config import Field, TypedConfigModel


class DynamicConfigSchema(TypedConfigModel, table=True):
    """群成员知识库提案插件的运行时配置。"""

    plugin_name: ClassVar[str] = "komari_custom"
    __tablename__ = "komari_custom_config"

    plugin_enable: bool = Field(default=False, description="插件启用状态")
    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户", sa_type=JSONB
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊", sa_type=JSONB
    )

    required_votes: int = Field(default=5, ge=2, le=50, description="提案通过所需票数")
    vote_emoji_id: str = Field(default="76", description="投票使用的 QQ 表情 ID")
    proposal_expire_hours: int = Field(
        default=2, ge=1, le=720, description="投票提案过期时间（小时）"
    )
    list_chunk_size: int = Field(default=20, ge=5, le=50, description="列表每页数量")
    max_proposals_per_user: int = Field(
        default=5, ge=1, le=20, description="单个用户同时进行的最大提案数"
    )
    redis_db: int = Field(default=0, ge=0, le=15, description="Redis 数据库编号")

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, value: Any) -> Any:
        """兼容 JSON 字符串和逗号分隔字符串形式的白名单。"""
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in value.split(",") if item.strip()]
        return value
