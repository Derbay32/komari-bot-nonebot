from pydantic import BaseModel, Field


class ScopedConfig(BaseModel):
    """Glitchtip 心跳检测插件配置"""

    # 插件开关
    enabled: bool = Field(default=True, description="是否启用 Glitchtip 心跳检测")
    url: str = Field(default="", description="Glitchtip 心跳检测 URL")
    interval: int = Field(default=60, description="心跳发送间隔（秒）")


class Config(BaseModel):
    """glitchtip_heartbeat 插件 scope 配置"""

    glitchtip_heartbeat: ScopedConfig = Field(default_factory=ScopedConfig)
