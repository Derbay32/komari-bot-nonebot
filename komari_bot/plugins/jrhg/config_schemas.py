"""
JRHG 插件的动态配置 Schema。
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DynamicConfigSchema(BaseModel):
    """JRHG 插件的动态配置。

    此模型表示可在运行时修改并在机器人重启后持久化的配置。
    """

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().astimezone().isoformat(),
        description="最后更新时间戳",
    )

    # 插件控制
    plugin_enable: bool = Field(default=False, description="JRHG 插件启用状态")

    # API 提供商选择
    api_provider: str = Field(
        default="deepseek", description="API 提供商: deepseek 或 gemini"
    )

    api_model: str = Field(
        default="deepseek-chat",
        description="API 所要使用的模型，详情参考对应云服务商文档",
    )

    # Gemini API 专用思考配置
    gemini_thinking_token: int = Field(
        default=0,
        ge=0,
        le=8192,
        description="Gemini 2.5 及以前模型使用的思考 token 限额参数",
    )

    gemini_thinking_level: str = Field(
        default="minimal", description="Gemini 3 及以后模型使用的思考等级参数"
    )

    # 放进 config 里面，防止 Google 没事换模型名字还得改代码
    # 其实感觉不适配 2.5 也行但还是加上吧
    gemini_level_models: list[str] = Field(
        default=["gemini-3-pro-preview", "gemini-3-flash-preview"],
        description="Gemini 采用 thinking_level 参数的模型列表",
    )

    # 默认系统提示词（用于好感度问候）
    default_prompt: str = Field(
        default="你是小鞠，一个可爱的AI助手。请根据你对用户的好感度，用相应的态度和用户打招呼。好感度越高，你的语气应该越热情友好。",
        description="默认的系统提示词",
    )

    # 白名单配置
    user_whitelist: list[str] = Field(
        default_factory=list, description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: list[str] = Field(
        default_factory=list, description="群聊白名单，为空则允许所有群聊"
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
