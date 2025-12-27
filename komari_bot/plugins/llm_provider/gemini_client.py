"""Gemini API 客户端。"""
from google import genai
from google.genai import types
from google.genai import errors
from nonebot import logger
from nonebot.plugin import require

from .base_client import BaseLLMClient
from .config_schema import DynamicConfigSchema

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager("llm_provider", DynamicConfigSchema)
config = config_manager.initialize()

class GeminiClient(BaseLLMClient):
    """Gemini API 客户端。"""

    def __init__(self, api_token: str):
        """初始化客户端。

        Args:
            api_token: Gemini API Token
        """
        self.api_token = api_token
        # SDK 客户端（同步客户端用于配置，异步操作使用 client.aio）
        self._client = genai.Client(api_key=api_token)

    async def generate_text(
        self,
        prompt: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ) -> str:
        """生成文本。

        Args:
            prompt: 用户提示词
            system_instruction: 系统指令
            temperature: 温度参数，None 使用默认值
            max_tokens: 最大 token 数，None 使用默认值
            **kwargs: 其他参数（当前未使用）

        Returns:
            生成的文本
        """
        try:
            # 构建 SDK 配置
            gen_config = types.GenerateContentConfig(
                temperature=temperature if temperature is not None else config.gemini_temperature,
                max_output_tokens=max_tokens if max_tokens is not None else int(config.gemini_max_tokens),
            )

            # 添加系统指令
            if system_instruction:
                gen_config.system_instruction = system_instruction

            # 使用 SDK 的异步接口
            response = await self._client.aio.models.generate_content(
                model=config.gemini_model,
                contents=prompt,
                config=gen_config,
            )
            if not response.text:
                logger.error(f"Gemini API 错误: 未返回文本，回复可能被拦截")
                raise
            else:
                return response.text
        except errors.APIError as e:
            logger.error(f"Gemini API 错误: {e.code} - {e.message}")
            raise
        except Exception as e:
            logger.error(f"Gemini API 未知错误: {e}")
            raise

    async def test_connection(self) -> bool:
        """测试 API 连接。

        Returns:
            连接是否成功
        """
        try:
            response = await self._client.aio.models.generate_content(
                model=config.gemini_model,
                contents="你好",
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=10,
                ),
            )
            return True
        except errors.APIError as e:
            logger.error(f"Gemini API 连接测试失败: {e.code} - {e.message}")
            return False
        except Exception as e:
            logger.error(f"Gemini API 连接测试失败: {e}")
            return False

    async def close(self):
        """关闭客户端。

        google-genai SDK 使用 httpx，会自动管理连接，无需手动关闭。
        """
        pass
