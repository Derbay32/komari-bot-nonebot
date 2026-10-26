"""Komari Chat 配置 Schema。

主动回复频控与回复履约 worker 的 11 个活字段从 ``komari_memory_config``
迁入本表（KOMARIBOT-7），死字段 ``proactive_score_threshold`` 随迁出
删除；回复履约另有独立的回复时效字段
``reply_fulfillment_freshness_seconds``（TSK-81）。TSK-87 contract 后
旧 outbox 时代的配置名已整体改名为 ``reply_fulfillment_*``，并新增
``reply_fulfillment_retry_max_seconds`` 退避上限字段。TSK-192 phase B 起
聊天生成槽位、常识库/上下文预算、表情反应、聊天开关与查询重写槽位也由
本表独立拥有。
"""

from typing import Any, ClassVar, Literal

from pydantic import field_validator, model_validator
from sqlalchemy import CheckConstraint, Column, String

from komari_bot.config.typed_config import Field, TypedConfigModel, typed_model_config
from komari_bot.llm.llm_protocol import RequestApi


class KomariChatConfigSchema(TypedConfigModel, table=True):
    """Komari Chat 插件配置。"""

    plugin_name: ClassVar[str] = "komari_chat"
    __tablename__ = "komari_chat_config"

    model_config = typed_model_config(
        json_schema_extra={"default_apply_mode": "immediate"},
    )

    # 时效字段的数据库级 CHECK 与 0007 迁移保持一致，避免 autogenerate 漂移；
    # 回复 Agent 预算的跨字段 CHECK 与 0013 迁移保持一致（TSK-192）；
    # 图片下载预算的跨字段 CHECK 与 0015 迁移保持一致（TSK-194）。
    __table_args__ = (
        CheckConstraint(
            "reply_fulfillment_freshness_seconds >= 30 "
            "AND reply_fulfillment_freshness_seconds <= 300",
            name="ck_komari_chat_config_reply_fulfillment_freshness",
        ),
        CheckConstraint(
            "agent_max_tool_calls_per_round <= agent_max_total_tool_calls "
            "AND agent_max_total_tool_calls <= agent_max_rounds * agent_max_tool_calls_per_round",
            name="ck_komari_chat_config_agent_budget",
        ),
        CheckConstraint(
            "vision_image_download_total_max_bytes >= vision_image_download_max_bytes",
            name="ck_komari_chat_config_image_budget_bytes",
        ),
        CheckConstraint(
            "vision_image_download_total_timeout_seconds >= "
            "vision_image_download_connect_timeout_seconds",
            name="ck_komari_chat_config_image_budget_timeout",
        ),
    )

    # 插件控制（TSK-192 phase B：chat 自己的业务开关）
    plugin_enable: bool = Field(
        default=False,
        description="插件启用状态",
        json_schema_extra={"apply_mode": "restart"},
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
    llm_request_api_chat: RequestApi = Field(
        default="chat_completions",
        sa_column=Column(String(32), nullable=False, default="chat_completions"),
        description=(
            "对话模型请求 API：chat_completions=OpenAI Chat Completions，"
            "responses=OpenAI Responses。修改后从下一个业务任务开始生效。"
        ),
    )
    llm_stream_enabled_chat: bool = Field(
        default=False,
        description=(
            "对话模型是否启用流式传输（仅在网关内部聚合，业务插件无感知）。"
            "修改后从下一个业务任务开始生效。"
        ),
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

    # 常识库与聊天检索配置
    knowledge_enabled: bool = Field(default=True, description="是否启用常识库集成")
    knowledge_limit: int = Field(
        default=3, ge=1, le=10, description="常识库检索数量限制"
    )
    context_messages_limit: int = Field(
        default=10, ge=5, le=50, description="获取最近消息上下文的最大数量"
    )
    context_max_utf8_bytes: int = Field(
        default=24_000,
        ge=1_024,
        le=131_072,
        description="单次聊天近期消息上下文的 UTF-8 字节预算",
    )
    context_max_estimated_tokens: int = Field(
        default=6_000,
        ge=256,
        le=32_000,
        description="单次聊天近期消息上下文的估算 token 预算",
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

    # 查询重写专用槽位（值一次性从 memory 总结槽位迁移，之后独立拥有）
    query_rewrite_model: str = Field(
        default="deepseek-v4-flash", description="查询重写使用模型"
    )
    query_rewrite_request_api: RequestApi = Field(
        default="chat_completions",
        sa_column=Column(String(32), nullable=False, default="chat_completions"),
        description="查询重写模型请求 API。语义同 llm_request_api_summary。",
    )
    query_rewrite_stream_enabled: bool = Field(
        default=False,
        description="查询重写模型是否启用流式传输。语义同 llm_stream_enabled_summary。",
    )
    query_rewrite_thinking_mode: bool = Field(
        default=False,
        description="查询重写模型是否处于思考模式。语义同 llm_thinking_mode_summary。",
    )
    query_rewrite_reasoning_effort: str = Field(
        default="",
        description="查询重写模型思考强度。语义同 llm_reasoning_effort_summary。",
    )

    # 主动回复配置
    proactive_enabled: bool = Field(
        default=False,
        description="是否启用主动回复",
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

    # 回复履约后台 worker
    reply_fulfillment_worker_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=300,
        description="回复履约后台 worker 的扫描间隔（秒）",
    )
    reply_fulfillment_batch_size: int = Field(
        default=20,
        ge=1,
        le=200,
        description="回复履约单轮最大领取数",
    )
    reply_fulfillment_lease_seconds: int = Field(
        default=120,
        ge=30,
        le=900,
        description="回复履约 worker 租约时长（秒）",
    )
    reply_fulfillment_max_attempts: int = Field(
        default=20,
        ge=1,
        le=100,
        description="送达后承诺自动重试上限，耗尽后保留 FAILED 对账记录",
    )
    reply_fulfillment_retry_base_seconds: int = Field(
        default=5,
        ge=1,
        le=300,
        description="送达后承诺指数退避基准秒数",
    )
    reply_fulfillment_retry_max_seconds: int = Field(
        default=3600,
        ge=1,
        le=86_400,
        description="送达后承诺指数退避上限秒数",
    )
    reply_fulfillment_tombstone_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="已完成或未送达的履约身份保护期天数",
    )
    reply_fulfillment_freshness_seconds: int = Field(
        default=120,
        ge=30,
        le=300,
        description="回复准备完成到开始发送的时效窗口（秒），满时效未发送即按未送达终止",
        json_schema_extra={"apply_mode": "immediate"},
    )

    # 回复 Agent 执行预算（TSK-192）：三项预算在任务起点冻结，
    # 中途配置变更只影响下一个任务。
    agent_max_rounds: int = Field(
        default=10,
        ge=2,
        le=20,
        description="回复 Agent 最大逻辑轮次（每轮至多一次模型调用）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    agent_max_tool_calls_per_round: int = Field(
        default=4,
        ge=1,
        le=8,
        description="回复 Agent 单轮最大工具调用数（超量整批拒绝，不执行半批）",
        json_schema_extra={"apply_mode": "immediate"},
    )
    agent_max_total_tool_calls: int = Field(
        default=20,
        ge=2,
        le=64,
        description="回复 Agent 整任务最大工具调用总数（模型提出的调用即计入）",
        json_schema_extra={"apply_mode": "immediate"},
    )

    # 工具调用约束模式（TSK-193）：在任务起点与三项预算一起冻结；
    # required=每轮强制模型提出工具调用（不兼容的思考模型明确失败），
    # prompt_guided=省略服务端强制参数、只经数据库 Prompt 引导，
    # 但裸文本仍不构成成功回复。无兼容值/宽松规范化。
    agent_tool_call_mode: Literal["required", "prompt_guided"] = Field(
        default="required",
        sa_column=Column(String(32), nullable=False, default="required"),
        description=(
            "回复 Agent 工具调用约束模式：required=每轮强制模型提出工具调用；"
            "prompt_guided=省略服务端强制工具参数，仅经数据库 Prompt 引导"
        ),
        json_schema_extra={"apply_mode": "immediate"},
    )

    # 图片理解模式与下载预算（TSK-194）：从 komari_memory_config 迁入，
    # 模式与 8 项预算在任务起点按 chat 配置冻结；旧 memory 字段不保留
    # alias / 双读 / fallback。
    image_understanding_mode: Literal["native", "delegated"] = Field(
        default="delegated",
        sa_column=Column(String(32), nullable=False, default="delegated"),
        description=(
            "图片理解模式：native=图片经安全下载与校验后作为多模态输入直接"
            "给聊天主模型（不声明 read_image 工具）；delegated=向回复 Agent "
            "暴露 read_image 工具，由独立视觉模型子调用读取图片描述。"
            "两种模式均不自动降级"
        ),
        json_schema_extra={"apply_mode": "immediate"},
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

    @field_validator("dsv4_roleplay_instruct_mode", mode="before")
    @classmethod
    def normalize_dsv4_roleplay_instruct_mode(cls, value: Any) -> str:
        """规范化 DeepSeek V4 角色扮演指令注入模式。"""
        normalized = str(value or "auto").strip().lower()
        if normalized not in {"disabled", "auto", "inner_os", "no_inner_os"}:
            return "auto"
        return normalized

    @model_validator(mode="after")
    def _validate_agent_budget(self) -> "KomariChatConfigSchema":
        """跨字段校验：单轮预算 <= 总预算 <= 轮次 x 单轮预算。"""
        if self.agent_max_tool_calls_per_round > self.agent_max_total_tool_calls:
            msg = "agent_max_tool_calls_per_round 不能大于 agent_max_total_tool_calls"
            raise ValueError(msg)
        if (
            self.agent_max_total_tool_calls
            > self.agent_max_rounds * self.agent_max_tool_calls_per_round
        ):
            msg = (
                "agent_max_total_tool_calls 不能大于 "
                "agent_max_rounds x agent_max_tool_calls_per_round"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_image_download_budget(self) -> "KomariChatConfigSchema":
        """跨字段校验：总字节上限 >= 单图字节上限，总时限 >= 连接超时。"""
        if self.vision_image_download_total_max_bytes < (
            self.vision_image_download_max_bytes
        ):
            raise ValueError("图片总字节上限不能小于单图字节上限")
        if self.vision_image_download_total_timeout_seconds < (
            self.vision_image_download_connect_timeout_seconds
        ):
            raise ValueError("图片下载总时限不能小于连接超时")
        return self
