"""群聊历史总结插件配置。"""

from typing import ClassVar, Self

from pydantic import BaseModel, model_validator
from sqlalchemy.dialects.postgresql import JSONB

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config


class LayoutParamsSchema(BaseModel):
    """图片布局参数。"""

    canvas_width: int = Field(default=1365, ge=600, le=3000)
    canvas_height: int = Field(default=645, ge=300, le=2000)
    bg_color: str = Field(default="#444444")

    title_x: int = Field(default=110, ge=0, le=5000)
    title_y: int = Field(default=80, ge=0, le=5000)
    title_size: int = Field(default=64, ge=10, le=300)
    title_color: str = Field(default="#FFFFFF")

    body_x: int = Field(default=112, ge=0, le=5000)
    body_y: int = Field(default=185, ge=0, le=5000)
    body_size: int = Field(default=30, ge=10, le=300)
    body_color: str = Field(default="#F3F3F3")
    body_line_gap: int = Field(default=10, ge=0, le=100)
    body_max_width: int = Field(default=750, ge=100, le=5000)

    char_enabled: bool = Field(default=True)
    char_scale: float = Field(default=0.3, ge=0.01, le=1.0)
    char_max_height_ratio: float = Field(default=0.82, ge=0.01, le=1.0)
    char_x_offset: int = Field(default=-10, ge=-5000, le=5000)
    char_y_offset: int = Field(default=0, ge=-5000, le=5000)

    @model_validator(mode="after")
    def validate_canvas_bounds(self) -> Self:
        """拒绝会把主要文本或角色图整体放到画布外的组合。"""
        if self.title_x >= self.canvas_width or self.title_y >= self.canvas_height:
            msg = "标题起点必须位于画布内"
            raise ValueError(msg)
        if self.body_x + self.body_max_width > self.canvas_width:
            msg = "正文横向区域超出画布"
            raise ValueError(msg)
        if self.body_y + self.body_size > self.canvas_height:
            msg = "正文起点与字号组合超出画布"
            raise ValueError(msg)
        if abs(self.char_x_offset) >= self.canvas_width:
            msg = "角色图横向偏移会使图片完全离开画布"
            raise ValueError(msg)
        if abs(self.char_y_offset) >= self.canvas_height:
            msg = "角色图纵向偏移会使图片完全离开画布"
            raise ValueError(msg)
        return self


class DynamicConfigSchema(TypedConfigModel, table=True):
    """群聊历史总结插件动态配置。"""

    plugin_name: ClassVar[str] = "group_history_summary"
    __tablename__ = "komari_group_history_summary_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    summary_enabled: bool = Field(
        default=True, description="是否处理新的群聊总结请求"
    )

    redis_db: int = Field(
        default=0,
        ge=0,
        le=15,
        description="群总结分布式锁使用的 Redis 数据库编号",
        json_schema_extra={"apply_mode": "restart"},
    )
    summary_lock_ttl_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="群总结分布式锁租约时长（秒），运行中会自动续租",
    )
    history_min_coverage_ratio: float = Field(
        default=0.8,
        ge=0.1,
        le=1.0,
        description="历史分页失败时允许继续总结的最低已覆盖比例",
    )

    min_summary_count: int = Field(
        default=10, ge=1, le=1000, description="最少总结条数"
    )
    max_summary_count: int = Field(
        default=200, ge=1, le=1000, description="最多总结条数"
    )
    fetch_batch_size: int = Field(default=50, ge=1, le=200, description="单次拉取条数")
    summary_default_count: int = Field(
        default=50, ge=1, le=200, description="LLM 未指定时的默认总结条数"
    )
    summary_planning_round_limit: int = Field(
        default=3, ge=1, le=6, description="总结规划工具循环上限"
    )
    summary_tool_scan_limit: int = Field(
        default=300, ge=10, le=500, description="总结工具本地扫描历史硬上限"
    )

    layout_params: LayoutParamsSchema = Field(
        default_factory=LayoutParamsSchema,
        sa_type=JSONB,
        description="总结图片布局参数",
    )

    @model_validator(mode="after")
    def validate_summary_count_bounds(self) -> Self:
        """校验总结计数与扫描硬上限的组合关系。"""
        if not (
            self.min_summary_count
            <= self.summary_default_count
            <= self.max_summary_count
        ):
            msg = "总结条数必须满足 min_summary_count <= default <= max_summary_count"
            raise ValueError(msg)
        if self.summary_tool_scan_limit < self.max_summary_count:
            msg = "summary_tool_scan_limit 不能小于 max_summary_count"
            raise ValueError(msg)
        return self
