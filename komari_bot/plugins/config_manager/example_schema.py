"""
**这仅仅是一个参考文件，它没有任何作用！插件也不会使用到这个文件！**

配置 Schema 参考实现。

这是一个示例文件，展示如何定义一个插件配置 Schema。
请将此代码复制到你的插件中，而不是直接导入。

由于 NoneBot 插件系统的限制，本地插件之间不能直接互相导入。
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ExampleConfigSchema(BaseModel):
    """示例配置 Schema。

    展示一个完整的插件配置应该包含哪些字段。
    可以将此代码复制到你的插件中并修改字段名称。
    """

    # 元数据
    # 这是一个必要项，使用时必须完整复制
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="最后更新时间戳"
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="插件启用状态")

    # 白名单配置
    user_whitelist: List[str] = Field(
        default_factory=list,
        description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: List[str] = Field(
        default_factory=list,
        description="群聊白名单，为空则允许所有群聊"
    )

    # 你的自定义字段
    # custom_field: str = Field(default="", description="自定义配置")

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
