"""Komari Memory 配置 Schema。"""

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KomariMemoryConfigSchema(BaseModel):
    """Komari Memory 插件配置。"""

    model_config = ConfigDict(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    # 插件控制
    plugin_enable: bool = Field(
        default=False,
        description="插件启用状态",
        json_schema_extra={"apply_mode": "restart"},
    )

    # 白名单配置
    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊"
    )

    # Redis 配置
    redis_db: int = Field(
        default=1,
        description="Redis 数据库编号 (避免与其他插件冲突)",
        json_schema_extra={"apply_mode": "rebuild"},
    )

    # LLM 配置 - 对话模型（用于生成回复）
    llm_model_chat: str = Field(
        default="gemini-3-flash-preview", description="对话使用模型"
    )
    llm_temperature_chat: float = Field(
        default=1.0, ge=0.0, le=2.0, description="对话模型温度参数"
    )
    llm_max_tokens_chat: int = Field(
        default=4000, ge=20, le=8192, description="对话模型最大 token 数"
    )
    llm_thinking_mode_chat: bool = Field(
        default=False,
        description=(
            "聊天主模型是否处于思考模式。"
            "deepseek-v4 系模型默认开启思考，置 False 会注入 thinking:disabled 关闭；"
            "其他模型置 True 时按 llm_reasoning_effort_chat 开启思考。"
            "思考模式启用时将跳过 tool_choice 注入。"
        ),
    )
    llm_reasoning_effort_chat: str = Field(
        default="",
        description=(
            "聊天主模型思考强度（仅 thinking_mode=True 且非 deepseek-v4 系时生效）。"
            "可选：minimal/low/medium/high；为空时不发送 reasoning_effort。"
        ),
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
    vision_tool_enabled: bool = Field(
        default=True,
        description="是否启用 V4 工具调用读图模式",
    )
    vision_image_download_max_count: int = Field(
        default=4,
        ge=1,
        le=8,
        description="单条消息最多下载的当前消息与引用消息图片总数",
        json_schema_extra={"apply_mode": "immediate"},
    )
    vision_image_download_max_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=64 * 1024,
        le=16 * 1024 * 1024,
        description="单张图片响应体最大字节数",
        json_schema_extra={"apply_mode": "immediate"},
    )
    vision_image_download_total_max_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=64 * 1024,
        le=32 * 1024 * 1024,
        description="单条消息全部图片响应体累计最大字节数",
        json_schema_extra={"apply_mode": "immediate"},
    )
    vision_image_download_max_pixels: int = Field(
        default=40_000_000,
        ge=1_000_000,
        le=100_000_000,
        description="单张静态图片或动画全部帧的累计像素上限",
        json_schema_extra={"apply_mode": "immediate"},
    )
    vision_image_download_concurrency: int = Field(
        default=2,
        ge=1,
        le=4,
        description="单条消息图片下载最大并发数",
        json_schema_extra={"apply_mode": "immediate"},
    )
    vision_image_download_connect_timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=15.0,
        description="单次图片连接超时秒数",
        json_schema_extra={"apply_mode": "immediate"},
    )
    vision_image_download_read_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=60.0,
        description="单次图片响应读取停顿超时秒数",
        json_schema_extra={"apply_mode": "immediate"},
    )
    vision_image_download_total_timeout_seconds: float = Field(
        default=45.0,
        ge=5.0,
        le=90.0,
        description="单条消息全部图片下载总时限秒数",
        json_schema_extra={"apply_mode": "immediate"},
    )

    # LLM 配置 - 总结模型（用于总结对话，区别于对话模型）
    llm_model_summary: str = Field(
        default="deepseek-v4-flash", description="总结使用模型"
    )
    llm_temperature_summary: float = Field(
        default=0.3, ge=0.0, le=2.0, description="总结模型温度参数"
    )
    llm_max_tokens_summary: int = Field(
        default=2048, ge=20, le=8192, description="总结模型最大 token 数"
    )
    llm_thinking_mode_summary: bool = Field(
        default=False,
        description="总结/记忆/画像模型是否处于思考模式。语义同 llm_thinking_mode_chat。",
    )
    llm_reasoning_effort_summary: str = Field(
        default="",
        description="总结/记忆/画像模型思考强度。语义同 llm_reasoning_effort_chat。",
    )

    # 常识库集成配置
    knowledge_enabled: bool = Field(default=True, description="是否启用常识库集成")
    knowledge_limit: int = Field(
        default=3, ge=1, le=10, description="常识库检索数量限制"
    )

    # 记忆管理配置
    summary_idle_timeout: int = Field(
        default=1800,
        ge=300,
        le=7200,
        description="群组空闲超时触发总结的时间（秒）。自最后一条消息后超过该时间且消息数达标时才触发",
    )
    summary_min_messages: int = Field(
        default=100,
        ge=50,
        le=500,
        description="触发总结所需的最小消息条数。不足此数不总结（每日跨天清理除外）",
    )
    summary_max_buffer_size: int = Field(
        default=500,
        ge=100,
        le=2000,
        description="消息缓冲区的最大消息条数安全上限。达到后即使未空闲也会强制触发总结",
    )
    conversation_snapshot_ttl_seconds: int = Field(
        default=3600,
        ge=300,
        le=86400,
        description="对话缓冲 processing 快照 TTL（秒）",
    )
    profile_snapshot_ttl_seconds: int = Field(
        default=1800,
        ge=300,
        le=86400,
        description="画像 Agent 基线快照 TTL（秒）",
    )
    profile_snapshot_enable: bool = Field(
        default=True,
        description="是否启用画像 Agent 基线快照",
    )
    profile_trait_limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="每个用户画像允许保留的长期稳定 traits 最大数量",
    )
    memory_agent_max_rounds: int = Field(
        default=12,
        ge=1,
        le=50,
        description="记忆画像 Agent 最大工具调用轮数",
    )
    memory_agent_staging_ttl_seconds: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="记忆画像 Agent Redis 暂存区 TTL（秒）",
    )
    memory_agent_max_tool_calls: int = Field(
        default=24,
        ge=1,
        le=200,
        description="单次记忆画像 Agent 最大工具调用数",
    )
    memory_agent_max_read_profiles: int = Field(
        default=20,
        ge=0,
        le=200,
        description="单次记忆画像 Agent 最大读取用户画像次数",
    )
    memory_agent_max_write_operations: int = Field(
        default=40,
        ge=0,
        le=500,
        description="单次记忆画像 Agent 最大暂存画像操作数",
    )
    memory_agent_lock_timeout_seconds: int | None = Field(
        default=None,
        ge=1,
        le=3600,
        description="记忆 Agent 生命周期锁等待超时；为空表示一直等待",
    )
    memory_search_limit: int = Field(
        default=3, ge=1, le=10, description="检索相关记忆的最大数量"
    )
    context_messages_limit: int = Field(
        default=10, ge=5, le=50, description="获取最近消息上下文的最大数量"
    )
    global_interaction_enabled: bool = Field(
        default=True,
        description="是否启用跨群互动事件缓冲",
    )
    global_interaction_trigger_size: int = Field(
        default=20,
        ge=5,
        le=200,
        description="触发事件总结的 Redis 缓冲条数阈值，不是 LIST 最大长度",
    )
    global_interaction_summary_interval_minutes: int = Field(
        default=1,
        ge=1,
        le=10,
        description="互动事件 Worker 轮询间隔（分钟）",
        json_schema_extra={"apply_mode": "rebuild"},
    )
    global_interaction_processing_lease_seconds: int = Field(
        default=1800,
        ge=60,
        le=7200,
        description="互动事件 processing 租约的可见性超时（秒）",
        json_schema_extra={"apply_mode": "immediate"},
    )

    # 主动回复配置
    proactive_enabled: bool = Field(
        default=False,
        description="是否启用主动回复",
        json_schema_extra={"apply_mode": "immediate"},
    )
    proactive_score_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="触发主动回复的评分阈值",
        json_schema_extra={"apply_mode": "immediate"},
    )
    proactive_cooldown: int = Field(
        default=300,
        ge=5,
        le=3600,
        description="主动回复送达后的冷却时间（秒）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    proactive_max_per_hour: int = Field(
        default=400,
        ge=1,
        le=800,
        description="最近一小时最大主动回复次数（包含生成中的预占）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    proactive_reservation_ttl_seconds: int = Field(
        default=360,
        ge=30,
        le=900,
        description="主动回复生成与发送阶段的 Redis 预占有效期（秒）",
        json_schema_extra={"apply_mode": "immediate"},
    )

    # 提示词模板配置
    # 机器人昵称
    bot_nickname: str = Field(default="小鞠知花", description="机器人昵称")

    # 回复提取配置
    response_tag: str = Field(
        default="content",
        description="从 LLM 回复中提取的 XML 标签名（如 content 则提取 <content>...</content>）",
    )

    # 记忆忘却配置
    forgetting_enabled: bool = Field(default=True, description="是否启用记忆忘却")
    forgetting_importance_threshold: int = Field(
        default=3,
        ge=1,
        le=5,
        description="低价值记忆直接删除阈值（高于该值的记忆首次归零会先模糊化）",
    )
    forgetting_decay_factor: float = Field(
        default=0.95, ge=0.9, le=0.99, description="兼容旧配置，当前整数忘却模型未使用"
    )
    forgetting_access_boost: float = Field(
        default=1.2, ge=1.0, le=2.0, description="兼容旧配置，当前整数忘却模型未使用"
    )
    forgetting_min_age_days: int = Field(
        default=3, ge=1, le=30, description="记忆最小保留天数"
    )
    forgetting_fuzzify_concurrency: int = Field(
        default=3, ge=1, le=10, description="首次归零模糊化时的 LLM 最大并发数"
    )

    # 查询重写配置
    query_rewrite_history_limit: int = Field(
        default=5, ge=1, le=10, description="查询重写时使用的历史对话数量"
    )

    # 机器人身份配置
    bot_aliases: list[str] = Field(
        default_factory=lambda: ["小鞠", "小鞠知花", "komari"],
        description="机器人别名列表（用于机器人身份识别）",
    )

    # 表情反应配置
    face_reaction_enabled: bool = Field(
        default=False,
        description="是否在触发聊天回复后对用户消息添加表情反应，用于提示正在生成回复",
    )
    face_reaction_id: str = Field(
        default="76",
        description="表情反应的 Face ID（QQ 表情 ID），如 76=赞。仅 face_reaction_enabled=true 时生效",
    )

    @field_validator("user_whitelist", "group_whitelist", "bot_aliases", mode="before")
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

    @field_validator("dsv4_roleplay_instruct_mode", mode="before")
    @classmethod
    def normalize_dsv4_roleplay_instruct_mode(cls, value: Any) -> str:
        """规范化 DeepSeek V4 角色扮演指令注入模式。"""
        normalized = str(value or "auto").strip().lower()
        if normalized not in {"disabled", "auto", "inner_os", "no_inner_os"}:
            return "auto"
        return normalized

    @model_validator(mode="after")
    def validate_vision_image_download_budget(self) -> Self:
        """确保单图预算和连接阶段能被整批预算完整容纳。"""
        if self.vision_image_download_total_max_bytes < (
            self.vision_image_download_max_bytes
        ):
            raise ValueError("图片总字节上限不能小于单图字节上限")
        if self.vision_image_download_total_timeout_seconds < (
            self.vision_image_download_connect_timeout_seconds
        ):
            raise ValueError("图片下载总时限不能小于连接超时")
        return self
