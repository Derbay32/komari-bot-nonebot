from pydantic import BaseModel, Field
from typing import List

class Config(BaseModel):
    """sr 插件默认设置"""
    # 插件开关
    plugin_enable: bool = Field(
        default=False,
        description="SR 插件开关"
    )

    # 白名单配置
    user_whitelist: List[str] = Field(
        default_factory=list,
        description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: List[str] = Field(
        default_factory=list,
        description="群聊白名单，为空则允许所有群聊"
    )