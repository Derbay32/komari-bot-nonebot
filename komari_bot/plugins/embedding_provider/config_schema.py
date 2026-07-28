"""
Komari Bot Embedding Provider 配置 Schema。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class DynamicConfigSchema(BaseModel):
    """
    Embedding Provider 配置 Schema。
    """

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )
    # 嵌入配置
    embedding_source: str = Field(default="local", description="嵌入模式: local | api")
    embedding_model: str = Field(
        default="BAAI/bge-small-zh-v1.5", description="模型名称"
    )
    embedding_api_url: str = Field(
        default="",
        description="API 地址 (source=api 时), 例如 https://api.openai.com/v1/embeddings",
    )
    embedding_api_key: str = Field(default="", description="API 密钥")
    embedding_dimension: int = Field(default=512, description="向量维度")
    # Rerank 配置
    rerank_enabled: bool = Field(default=False, description="是否启用 rerank")
    rerank_model: str = Field(default="", description="Rerank 模型名称")
    rerank_api_url: str = Field(
        default="", description="Rerank API 地址 (Jina/Cohere 兼容格式)"
    )
    rerank_api_key: str = Field(default="", description="Rerank API 密钥")
    rerank_top_n: int = Field(default=5, ge=1, le=50, description="Rerank 默认返回数量")
