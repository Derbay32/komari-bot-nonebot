"""
用于 JSON 持久化的配置模型。

与 config.py 分离以避免循环导入。
"""
from datetime import datetime
from typing import List

from pydantic import BaseModel, Field, validator


class DynamicConfigSchema(BaseModel):
    """存储在 JSON 文件中的动态配置。

    此模型表示可在运行时修改并在机器人重启后持久化的配置。
    """

    # 元数据
    version: str = Field(default="1.0", description="配置架构版本")
    last_updated: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="最后更新时间戳"
    )

    # 插件控制
    jrhg_plugin_enable: bool = Field(default=False, description="JRHG 插件启用状态")

    # 白名单配置
    user_whitelist: List[str] = Field(
        default_factory=list,
        description="用户白名单，为空则允许所有用户"
    )
    group_whitelist: List[str] = Field(
        default_factory=list,
        description="群聊白名单，为空则允许所有群聊"
    )

    # DeepSeek API 配置
    deepseek_api_url: str = Field(
        default="https://api.deepseek.com/v1/chat/completions",
        description="DeepSeek API URL"
    )
    deepseek_api_token: str = Field(default="", description="API 令牌（敏感信息）")
    deepseek_model: str = Field(default="deepseek-chat", description="DeepSeek 模型名称")
    deepseek_temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
        description="API 温度参数"
    )
    deepseek_frequency_penalty: float = Field(
        default=0.0,
        ge=-2.0,
        le=2.0,
        description="API 频率惩罚参数"
    )
    deepseek_default_prompt: str = Field(
        default="你是小鞠，一个可爱的AI助手。请根据你对用户的好感度，用相应的态度和用户打招呼。好感度越高，你的语气应该越热情友好。",
        description="默认系统提示词"
    )

    @validator("user_whitelist", "group_whitelist", pre=True)
    @classmethod
    def parse_list_string(cls, v):
        """处理从 .env 格式解析列表。"""
        if isinstance(v, str):
            # 处理 .env 列表格式："[item1, item2]"
            import json

            try:
                parsed = json.loads(v)
                return [str(item) for item in parsed]
            except (json.JSONDecodeError, TypeError):
                # 回退：逗号分隔的字符串
                return [item.strip() for item in v.split(",") if item.strip()]
        return v
