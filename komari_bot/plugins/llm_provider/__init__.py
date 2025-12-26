"""LLM Provider 插件 - 提供统一的 LLM 调用接口。"""
import os
from typing import Optional

from nonebot import logger
from nonebot.plugin import PluginMetadata

from .config import Config
from .deepseek_client import DeepSeekClient
from .gemini_client import GeminiClient

__plugin_meta__ = PluginMetadata(
    name="llm_provider",
    description="通用 LLM API 提供者，支持 DeepSeek 和 Gemini",
    usage="""
    llm_provider = require("llm_provider")
    response = await llm_provider.generate_text(
        prompt="你好",
        provider="deepseek",
        system_instruction="你是一个助手",
    )
    """,
    config=Config,
)

# 从环境变量读取 API Token
_DEEPSEEK_TOKEN = os.getenv("DEEPSEEK_API_TOKEN", "")
_GEMINI_TOKEN = os.getenv("GEMINI_API_TOKEN", "")


async def generate_text(
    prompt: str,
    provider: str,
    system_instruction: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    **kwargs,
) -> str:
    """生成文本。

    Args:
        prompt: 用户提示词
        provider: API 提供商 ("deepseek" 或 "gemini")
        system_instruction: 系统指令
        temperature: 温度参数，None 使用默认值
        max_tokens: 最大 token 数，None 使用默认值
        **kwargs: 其他 provider 特定参数

    Returns:
        生成的文本
    """
    provider = provider.lower()

    if provider == "gemini":
        client = GeminiClient(_GEMINI_TOKEN)
    else:  # 默认 deepseek
        client = DeepSeekClient(_DEEPSEEK_TOKEN)

    try:
        result = await client.generate_text(
            prompt=prompt,
            system_instruction=system_instruction,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        return result
    except Exception as e:
        logger.error(f"LLM 调用失败 ({provider}): {e}")
        raise
    finally:
        await client.close()


async def test_connection(provider: str) -> bool:
    """测试指定 provider 的连接。

    Args:
        provider: API 提供商 ("deepseek" 或 "gemini")

    Returns:
        连接是否成功
    """
    provider = provider.lower()

    if provider == "gemini":
        client = GeminiClient(_GEMINI_TOKEN)
    else:
        client = DeepSeekClient(_DEEPSEEK_TOKEN)

    try:
        return await client.test_connection()
    finally:
        await client.close()
