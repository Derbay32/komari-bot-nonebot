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
        default=500, ge=20, le=8192, description="对话模型最大 token 数"
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
        default=50, ge=10, le=500, description="触发总结的消息数量阈值（优先于 token 阈值）"
    )

    # 主动回复配置
    proactive_enabled: bool = Field(default=False, description="是否启用主动回复")
    proactive_score_threshold: float = Field(
        default=0.8, ge=0.0, le=1.0, description="触发主动回复的评分阈值"
    )
    proactive_cooldown: int = Field(
        default=300, ge=60, le=3600, description="主动回复冷却时间（秒）"
    )
    proactive_max_per_hour: int = Field(
        default=3, ge=1, le=10, description="每小时最大主动回复次数"
    )

    # 提示词模板配置
    system_prompt: str = Field(
        default="你是小鞠，一个友好的 AI 助手",
        description="系统提示词",
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
