"""Komari Memory 配置 Schema。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class KomariMemoryConfigSchema(BaseModel):
    """Komari Memory 插件配置。"""

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

    # Redis 配置
    redis_host: str = Field(default="localhost", description="Redis 主机地址")
    redis_port: int = Field(default=6379, description="Redis 端口")
    redis_db: int = Field(
        default=1, description="Redis 数据库编号 (避免与其他插件冲突)"
    )
    redis_password: str = Field(
        default="", description="Redis 密码 (空字符串表示无密码)"
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

    # LLM 配置 - 总结模型（用于总结对话，区别于对话模型）
    llm_model_summary: str = Field(
        default="gemini-2.5-flash-lite", description="总结使用模型"
    )
    llm_temperature_summary: float = Field(
        default=0.3, ge=0.0, le=2.0, description="总结模型温度参数"
    )
    llm_max_tokens_summary: int = Field(
        default=2048, ge=20, le=8192, description="总结模型最大 token 数"
    )

    # 常识库集成配置
    knowledge_enabled: bool = Field(default=True, description="是否启用常识库集成")
    knowledge_limit: int = Field(
        default=3, ge=1, le=10, description="常识库检索数量限制"
    )

    # 记忆管理配置
    summary_token_threshold: int = Field(
        default=1000, ge=100, le=10000, description="触发总结的 Token 阈值"
    )
    summary_time_threshold: int = Field(
        default=3600, ge=300, le=86400, description="触发总结的时间阈值（秒）"
    )
    message_buffer_size: int = Field(
        default=200, ge=50, le=1000, description="Redis 消息缓存大小"
    )
    summary_message_threshold: int = Field(
        default=50,
        ge=10,
        le=500,
        description="触发总结的消息数量阈值（优先于 token 阈值）",
    )
    summary_max_messages: int = Field(
        default=200, ge=50, le=500, description="总结时从缓冲区获取的最大消息数"
    )
    summary_chunk_token_limit: int = Field(
        default=3000,
        ge=200,
        le=50000,
        description="总结前原文分段的估算 token 上限（按当前近似口径计算，不用于触发总结）",
    )
    profile_trait_limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="每个用户画像允许保留的长期稳定 traits 最大数量",
    )
    memory_search_limit: int = Field(
        default=3, ge=1, le=10, description="检索相关记忆的最大数量"
    )
    context_messages_limit: int = Field(
        default=10, ge=5, le=50, description="获取最近消息上下文的最大数量"
    )

    # 主动回复配置
    proactive_enabled: bool = Field(default=False, description="是否启用主动回复")
    proactive_score_threshold: float = Field(
        default=0.8, ge=0.0, le=1.0, description="触发主动回复的评分阈值"
    )
    proactive_cooldown: int = Field(
        default=300, ge=5, le=3600, description="主动回复冷却时间（秒）"
    )
    proactive_max_per_hour: int = Field(
        default=400, ge=1, le=800, description="每小时最大主动回复次数"
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
