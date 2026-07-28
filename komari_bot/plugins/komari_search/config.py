"""Komari Search 插件静态配置。"""

from pydantic import BaseModel, Field


class Config(BaseModel):
    """NoneBot 静态配置，仅用于从 .env 预置默认 Tavily Key。"""

    tavily_api_key: str = Field(default="", description="Tavily API Key")
