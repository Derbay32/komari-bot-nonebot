"""DeepSeek API 客户端。"""
import asyncio
import json
from typing import Optional

import aiohttp
from nonebot import logger
from nonebot.plugin import require

from .base_client import BaseLLMClient
from .config_schema import DynamicConfigSchema

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager("llm_provider", DynamicConfigSchema)
config = config_manager.initialize()



class DeepSeekClient(BaseLLMClient):
    """DeepSeek API 客户端。"""

    def __init__(self, api_token: str):
        """初始化客户端。

        Args:
            api_token: DeepSeek API Token
        """
        self.api_token = api_token
        self.session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话。"""
        if self.session is None or self.session.closed:
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=30.0)
            self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        return self.session

    async def generate_text(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str:
        """生成文本。

        Args:
            prompt: 用户提示词
            system_instruction: 系统指令
            temperature: 温度参数，None 使用默认值
            max_tokens: 最大 token 数，None 使用默认值
            **kwargs: 其他参数 (如 frequency_penalty)

        Returns:
            生成的文本
        """
        try:
            session = await self._get_session()

            # 构建消息
            messages = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            messages.append({"role": "user", "content": prompt})

            # 构建请求数据
            request_data = {
                "model": config.deepseek_model,
                "messages": messages,
                "temperature": temperature if temperature is not None else config.deepseek_temperature,
                "max_tokens": max_tokens if max_tokens is not None else config.deepseek_max_tokens,
                "frequency_penalty": kwargs.get("frequency_penalty", config.deepseek_frequency_penalty),
                "stream": False,
            }

            # 发送 API 请求
            async with session.post(config.deepseek_api_base, json=request_data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"DeepSeek API 请求失败: {response.status} - {error_text}")
                    raise Exception(f"DeepSeek API 请求失败: {response.status}")

                response_data = await response.json()

                # 解析响应
                if "choices" in response_data and len(response_data["choices"]) > 0:
                    content = response_data["choices"][0]["message"]["content"]
                    return content.strip()
                else:
                    logger.error(f"DeepSeek API 响应格式异常: {response_data}")
                    raise Exception("DeepSeek API 响应格式异常")

        except asyncio.TimeoutError:
            logger.error("DeepSeek API 请求超时")
            raise
        except aiohttp.ClientError as e:
            logger.error(f"DeepSeek API 网络错误: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"DeepSeek API 响应解析错误: {e}")
            raise
        except Exception as e:
            logger.error(f"DeepSeek API 未知错误: {e}")
            raise

    async def test_connection(self) -> bool:
        """测试 API 连接。

        Returns:
            连接是否成功
        """
        try:
            session = await self._get_session()

            request_data = {
                "model": config.deepseek_model,
                "messages": [{"role": "user", "content": "你好"}],
                "temperature": 0.1,
                "max_tokens": 10,
            }

            async with session.post(config.deepseek_api_base, json=request_data) as response:
                return response.status == 200

        except Exception as e:
            logger.error(f"DeepSeek API 连接测试失败: {e}")
            return False

    async def close(self):
        """关闭客户端。"""
        if self.session and not self.session.closed:
            await self.session.close()
