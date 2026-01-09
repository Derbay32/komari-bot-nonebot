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

    # PostgreSQL 配置
    pg_host: str = Field(default="localhost", description="PostgreSQL 主机地址")
    pg_port: int = Field(default=5432, description="PostgreSQL 端口")
    pg_database: str = Field(default="komari_bot", description="数据库名称")
    pg_user: str = Field(default="", description="数据库用户名")
    pg_password: str = Field(default="", description="数据库密码")
    pg_pool_min_size: int = Field(
        default=2, ge=1, le=10, description="PostgreSQL 连接池最小连接数"
    )
    pg_pool_max_size: int = Field(
        default=5, ge=1, le=50, description="PostgreSQL 连接池最大连接数"
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

    # 向量嵌入配置
    embedding_model: str = Field(
        default="BAAI/bge-small-zh-v1.5",
        description="向量嵌入模型 (与 komari_knowledge 一致)",
    )

    # BERT 评分服务配置
    bert_service_url: str = Field(
        default="http://localhost:8000/api/v1/score",
        description="BERT 评分服务地址",
    )
    bert_timeout: float = Field(default=2.0, description="BERT 请求超时时间（秒）")

    # LLM 配置 - 对话模型（用于生成回复）
    llm_provider: str = Field(default="gemini", description="LLM 提供商")
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

    # Gemini API 专用思考配置
    gemini_thinking_token: int = Field(
        default=0,
        ge=0,
        le=8192,
        description="Gemini 2.5 及以前模型使用的思考 token 限额参数",
    )

    gemini_thinking_level: str = Field(
        default="minimal", description="Gemini 3 及以后模型使用的思考等级参数"
    )

    # 放进 config 里面，防止 Google 没事换模型名字还得改代码
    # 其实感觉不适配 2.5 也行但还是加上吧
    gemini_level_models: list[str] = Field(
        default=["gemini-3-pro-preview", "gemini-3-flash-preview"],
        description="Gemini 采用 thinking_level 参数的模型列表",
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
    system_prompt: str = Field(
        default="",
        description="系统提示词",
    )

    # 机器人昵称
    bot_nickname: str = Field(default="小鞠知花", description="机器人昵称")

    # 多轮对话提示词配置
    background_prompt: str = Field(
        default="请阅读以上背景信息，并保持小鞠知花的人设，准备开始对话。",
        description="背景知识注入后的提示文本",
    )

    background_confirmation: str = Field(
        default="（捏着衣角，轻轻点头）嗯……我、我知道了……",
        description="背景知识注入后的确认块文本",
    )

    character_instruction: str = Field(
        default="[System: Stay in Character]\n回复时请务必保持【小鞠知花】的害羞、结巴口吻，不要像个 AI 助手那样说话。",
        description="保持人设的保险文本",
    )

    # 记忆忘却配置
    forgetting_enabled: bool = Field(default=True, description="是否启用记忆忘却")
    forgetting_importance_threshold: int = Field(
        default=3, ge=1, le=5, description="删除低重要性记忆的阈值"
    )
    forgetting_decay_factor: float = Field(
        default=0.95, ge=0.9, le=0.99, description="重要性衰减系数"
    )
    forgetting_access_boost: float = Field(
        default=1.2, ge=1.0, le=2.0, description="访问时重要性提升系数"
    )
    forgetting_min_age_days: int = Field(
        default=3, ge=1, le=30, description="记忆最小保留天数"
    )

    # 消息过滤配置
    filter_min_length: int = Field(
        default=3, ge=1, le=20, description="最短消息长度阈值（字符数）"
    )
    filter_history_check_size: int = Field(
        default=50, ge=10, le=200, description="历史重复检测检查的最近消息数量"
    )

    # 查询重写配置
    query_rewrite_history_limit: int = Field(
        default=5, ge=1, le=10, description="查询重写时使用的历史对话数量"
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
