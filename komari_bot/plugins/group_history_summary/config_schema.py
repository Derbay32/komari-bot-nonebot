"""群聊历史总结插件配置。"""

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from komari_bot.common.llm_protocol import RequestApi


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


class DynamicConfigSchema(BaseModel):
    """群聊历史总结插件动态配置。"""

    model_config = ConfigDict(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=True, description="插件启用状态")

    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊"
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
    summary_planning_model: str = Field(
        default="deepseek-chat", description="总结规划阶段模型"
    )
    summary_planning_max_tokens: int = Field(
        default=800, ge=128, le=8192, description="总结规划阶段最大 tokens"
    )
    summary_planning_round_limit: int = Field(
        default=3, ge=1, le=6, description="总结规划工具循环上限"
    )
    summary_planning_request_api: RequestApi = Field(
        default="chat_completions",
        description=(
            "总结规划阶段请求 API：chat_completions=OpenAI Chat Completions，"
            "responses=OpenAI Responses。修改后从下一个业务任务开始生效。"
        ),
    )
    summary_planning_stream_enabled: bool = Field(
        default=False,
        description=(
            "总结规划阶段是否启用流式传输（仅在网关内部聚合，业务插件无感知）。"
            "修改后从下一个业务任务开始生效。"
        ),
    )
    summary_planning_thinking_mode: bool = Field(
        default=False,
        description="群聊总结规划阶段模型是否处于思考模式。语义同 komari_memory.llm_thinking_mode_chat。",
    )
    summary_planning_reasoning_effort: str = Field(
        default="",
        description="群聊总结规划阶段模型思考强度。语义同 komari_memory.llm_reasoning_effort_chat。",
    )
    summary_tool_scan_limit: int = Field(
        default=300, ge=10, le=500, description="总结工具本地扫描历史硬上限"
    )

    summary_model: str = Field(default="deepseek-chat", description="总结模型")
    summary_temperature: float = Field(
        default=0.4, ge=0.0, le=2.0, description="总结温度参数"
    )
    summary_max_tokens: int = Field(
        default=1200, ge=128, le=8192, description="总结最大 tokens"
    )
    summary_request_api: RequestApi = Field(
        default="chat_completions",
        description="总结执行阶段请求 API。语义同 summary_planning_request_api。",
    )
    summary_stream_enabled: bool = Field(
        default=False,
        description="总结执行阶段是否启用流式传输。语义同 summary_planning_stream_enabled。",
    )
    summary_thinking_mode: bool = Field(
        default=False,
        description="群聊总结执行模型是否处于思考模式。语义同 komari_memory.llm_thinking_mode_chat。",
    )
    summary_reasoning_effort: str = Field(
        default="",
        description="群聊总结执行模型思考强度。语义同 komari_memory.llm_reasoning_effort_chat。",
    )
    assistant_prefill_enabled: bool = Field(
        default=False,
        description="是否启用旧版 assistant 预填充消息（memory_ack 与 cot_prefix）",
    )
    dsv4_roleplay_instruct_mode: str = Field(
        default="auto",
        description=(
            "DeepSeek V4 角色扮演思考指令注入模式："
            "disabled=关闭，auto=仅 deepseek-v4 模型注入角色沉浸指令，"
            "inner_os=强制角色沉浸，no_inner_os=强制纯分析"
        ),
    )

    layout_params: LayoutParamsSchema = Field(
        default_factory=LayoutParamsSchema,
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

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, value: Any) -> Any:
        """处理从 .env 格式解析列表。"""
        if isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("dsv4_roleplay_instruct_mode", mode="before")
    @classmethod
    def normalize_dsv4_roleplay_instruct_mode(cls, value: Any) -> str:
        """规范化 DeepSeek V4 角色扮演指令注入模式。"""
        normalized = str(value or "auto").strip().lower()
        if normalized not in {"disabled", "auto", "inner_os", "no_inner_os"}:
            return "auto"
        return normalized
