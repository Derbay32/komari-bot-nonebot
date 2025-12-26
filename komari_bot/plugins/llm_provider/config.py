"""LLM Provider 配置 - 内部默认配置。"""
from pydantic import BaseModel, Field


class Config(BaseModel):
    """LLM Provider 配置（仅用于 NoneBot 加载，实际配置在代码中管理）。"""

    # 这个类仅用于 NoneBot 插件加载，实际配置通过环境变量和代码常量管理
    pass


# DeepSeek 默认配置
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TEMPERATURE = 0.7
DEEPSEEK_MAX_TOKENS = 200
DEEPSEEK_FREQUENCY_PENALTY = 0.0


# Gemini 默认配置
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_TEMPERATURE = 0.7
GEMINI_MAX_TOKENS = 200
