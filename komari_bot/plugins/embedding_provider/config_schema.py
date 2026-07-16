"""
Komari Bot Embedding Provider 配置 Schema。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DynamicConfigSchema(BaseModel):
    """
    Embedding Provider 配置 Schema。
    """

    model_config = ConfigDict(
        json_schema_extra={"default_apply_mode": "restart"},
    )

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )
    # 嵌入配置
    embedding_model: str = Field(
        default="BAAI/bge-small-zh-v1.5", description="模型名称"
    )
    embedding_api_url: str = Field(
        default="",
        description="API 地址，例如 https://api.openai.com/v1/embeddings",
    )
    embedding_api_key: str = Field(
        default="",
        description="API 密钥",
        json_schema_extra={"secret": True},
    )
    embedding_dimension: int = Field(
        default=512,
        ge=1,
        le=65_536,
        description="向量维度",
    )
    request_connect_timeout_seconds: float = Field(
        default=5.0,
        ge=0.1,
        le=60.0,
        description="Embedding/Rerank API 连接超时（秒）",
    )
    request_read_timeout_seconds: float = Field(
        default=30.0,
        ge=0.1,
        le=300.0,
        description="Embedding/Rerank API 单次读取超时（秒）",
    )
    request_total_timeout_seconds: float = Field(
        default=45.0,
        ge=0.1,
        le=600.0,
        description="Embedding/Rerank API 单次请求总超时（秒）",
    )
    request_retry_attempts: int = Field(
        default=2,
        ge=1,
        le=3,
        description="瞬时网络、限流和服务端故障的最大尝试次数",
    )
    request_retry_backoff_seconds: float = Field(
        default=0.25,
        ge=0.0,
        le=5.0,
        description="远程请求指数退避基准秒数",
    )
    # Rerank 配置
    rerank_enabled: bool = Field(default=False, description="是否启用 rerank")
    rerank_model: str = Field(default="", description="Rerank 模型名称")
    rerank_api_url: str = Field(
        default="", description="Rerank API 地址 (Jina/Cohere 兼容格式)"
    )
    rerank_api_key: str = Field(
        default="",
        description="Rerank API 密钥",
        json_schema_extra={"secret": True},
    )
    rerank_top_n: int = Field(default=5, ge=1, le=50, description="Rerank 默认返回数量")
