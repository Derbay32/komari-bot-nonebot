"""用户数据插件动态配置 Schema。"""

from datetime import datetime

from pydantic import BaseModel, Field


class DynamicConfigSchema(BaseModel):
    """用户数据插件配置（由 config_manager 管理）。"""

    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    plugin_enable: bool = Field(default=True, description="插件启用状态")

    pg_host: str = Field(default="localhost", description="PostgreSQL 主机地址")
    pg_port: int = Field(default=5432, description="PostgreSQL 端口")
    pg_database: str = Field(default="komari_bot", description="数据库名称")
    pg_user: str = Field(default="", description="数据库用户名")
    pg_password: str = Field(default="", description="数据库密码")
    pg_pool_min_size: int = Field(
        default=1, ge=1, le=10, description="PostgreSQL 连接池最小连接数"
    )
    pg_pool_max_size: int = Field(
        default=5, ge=1, le=50, description="PostgreSQL 连接池最大连接数"
    )

    data_retention_days: int = Field(default=30, ge=1, le=3650, description="数据保留天数")

