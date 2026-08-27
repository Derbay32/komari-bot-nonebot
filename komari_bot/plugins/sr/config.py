from pydantic import BaseModel, Field


class Config(BaseModel):
    """sr 插件默认设置"""

    # 插件开关
    plugin_enable: bool = Field(default=False, description="SR 插件开关")

