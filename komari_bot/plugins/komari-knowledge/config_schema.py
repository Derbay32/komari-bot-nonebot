"""
Komari Memory 常识库插件配置 Schema。

用于管理 PostgreSQL 数据库连接和检索参数配置。
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """
    Komari Memory 配置 Schema。
    """

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="最后更新时间戳"
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="插件启用状态")

    # 白名单配置
    user_whitelist: list[str] = Field(
        default_factory=list,
        description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list,
        description="群聊白名单，为空则允许所有群聊"
    )

    # PostgreSQL 数据库配置
    pg_host: str = Field(default="localhost", description="PostgreSQL 主机地址")
    pg_port: int = Field(default=5432, description="PostgreSQL 端口")
    pg_database: str = Field(default="komari_bot", description="数据库名称")
    pg_user: str = Field(default="", description="数据库用户名")
    pg_password: str = Field(default="", description="数据库密码")

    # 向量检索配置
    embedding_model: str = Field(
        default="BAAI/bge-small-zh-v1.5",
        description="向量嵌入模型名称"
    )
    vector_dimension: int = Field(
        default=512,
        description="向量维度（bge-small-zh-v1.5 为 512）"
    )
    similarity_threshold: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        description="向量相似度阈值，低于此值的结果将被过滤"
    )

    # 检索配置
    query_rewrite_rules: dict[str, str] = Field(
        default={"你": "小鞠", "您的": "小鞠的"},
        description="查询重写规则，key 为待替换词，value 为替换词"
    )
    layer1_limit: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Layer 1 关键词匹配最大返回数量"
    )
    layer2_limit: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Layer 2 向量检索最大返回数量"
    )
    total_limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="总返回结果数量上限"
    )

    # WebUI 配置
    webui_enabled: bool = Field(
        default=False,
        description="是否启动 WebUI 管理界面"
    )
    webui_port: int = Field(
        default=8502,
        ge=1024,
        le=65535,
        description="WebUI 端口"
    )

    @field_validator("user_whitelist", "group_whitelist", mode="before")
    @classmethod
    def parse_list_string(cls, v):
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
