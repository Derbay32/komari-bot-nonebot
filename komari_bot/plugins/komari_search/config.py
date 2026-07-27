"""Komari Search 插件静态配置。"""

from pydantic import BaseModel, Field


class Config(BaseModel):
    """NoneBot 静态配置，仅用于从 .env 预置默认搜索 Key。"""

    search_api_key: str = Field(
        default="",
        description="联网搜索 API Key（对应 search_provider 填写）",
    )
