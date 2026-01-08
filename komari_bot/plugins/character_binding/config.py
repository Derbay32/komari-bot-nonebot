"""角色绑定插件配置。"""

from pydantic import BaseModel, Field


class CharacterBindingConfig(BaseModel):
    """角色绑定插件配置。"""

    # 未来可以扩展的配置项
    max_bind_length: int = Field(default=20, description="角色名最大长度")
    allow_duplicate_names: bool = Field(default=True, description="是否允许重复的角色名")
