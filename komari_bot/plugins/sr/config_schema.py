"""
配置 sr 插件的 Schema 实现。
"""

from typing import Any, ClassVar

from pydantic import field_validator
from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.common.content_budget import (
    CONTENT_TEXT_BUDGET,
    KEYWORD_TEXT_BUDGET,
    normalize_required_text,
    validate_text_budget,
)
from komari_bot.common.typed_config import Field, TypedConfigModel, typed_model_config

MAX_SR_LIST_ITEMS = 500


class DynamicConfigSchema(TypedConfigModel, table=True):
    """SR 插件的配置 Schema。

    此模型表示可在运行时修改并在机器人重启后持久化的配置。
    """

    plugin_name: ClassVar[str] = "sr"
    __tablename__ = "komari_sr_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="插件启用状态")

    # 白名单配置
    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户", sa_type=JSONB
    )

    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊", sa_type=JSONB
    )

    # sr 数据配置
    sr_list: list[str] = Field(
        default_factory=list,
        max_length=MAX_SR_LIST_ITEMS,
        sa_type=JSONB,
        description="安科神人榜列表",
    )
    list_chunk_size: int = Field(
        default=20, ge=5, le=50, description="list 命令每页显示条数"
    )

    # Redis 配置
    redis_db: int = Field(default=0, ge=0, le=15, description="Redis 数据库编号")

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

    @field_validator("sr_list", mode="before")
    @classmethod
    def validate_sr_list_count(cls, value: Any) -> Any:
        """在 Pydantic 内建长度校验前返回中文容量错误。"""
        if isinstance(value, list) and len(value) > MAX_SR_LIST_ITEMS:
            msg = f"神人榜最多允许 {MAX_SR_LIST_ITEMS} 项"
            raise ValueError(msg)
        return value

    @field_validator("sr_list")
    @classmethod
    def validate_sr_list_budget(cls, value: list[str]) -> list[str]:
        """限制榜单项目数、单项文本和总内容预算。"""
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            cleaned = normalize_required_text(
                item,
                label="神人榜单项",
                budget=KEYWORD_TEXT_BUDGET,
            )
            identity = cleaned.casefold()
            if identity in seen:
                msg = f"神人榜存在重复项目: {cleaned}"
                raise ValueError(msg)
            seen.add(identity)
            normalized.append(cleaned)
        validate_text_budget(
            "\n".join(normalized),
            label="神人榜总内容",
            budget=CONTENT_TEXT_BUDGET,
        )
        return normalized
