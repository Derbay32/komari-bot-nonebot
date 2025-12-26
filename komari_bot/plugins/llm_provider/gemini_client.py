"""Gemini API 客户端。"""
import asyncio
import json
from typing import Optional

import aiohttp
from nonebot import logger

from .base_client import BaseLLMClient
from .config import (
    GEMINI_API_BASE,
    GEMINI_MAX_TOKENS,
    GEMINI_MODEL,
    GEMINI_TEMPERATURE,
)


class GeminiClient(BaseLLMClient):
    """Gemini API 客户端。"""

    def __init__(self, api_token: str):
        """初始化客户端。

        Args:
            api_token: Gemini API Token
        """
        self.api_token = api_token
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话。"""
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            timeout = aiohttp.ClientTimeout(total=30.0)
            self._session = aiohttp.ClientSession(headers=headers, timeout=timeout)
        return self._session

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
            **kwargs: 其他参数（当前未使用）

        Returns:
            生成的文本
        """
        try:
            session = await self._get_session()

            # 构建模型名称
            model_name = GEMINI_MODEL
            if not model_name.startswith("models/"):
                model_name = f"models/{model_name}"

            # 构建请求 URL
            url = f"{GEMINI_API_BASE}/{model_name}:generateContent?key={self.api_token}"

            # 构建请求数据
            request_data = {
                "contents": [
                    {
                        "parts": [{"text": prompt}]
                    }
                ],
                "generationConfig": {
                    "temperature": temperature if temperature is not None else GEMINI_TEMPERATURE,
                    "maxOutputTokens": max_tokens if max_tokens is not None else GEMINI_MAX_TOKENS,
                }
            }

            # 添加系统指令
            if system_instruction:
                request_data["systemInstruction"] = {
                    "parts": [{"text": system_instruction}]
                }

            # 发送 API 请求
            async with session.post(url, json=request_data) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Gemini API 请求失败: {response.status} - {error_text}")
                    raise Exception(f"Gemini API 请求失败: {response.status}")

                response_data = await response.json()

                # 解析响应
                if "candidates" in response_data and len(response_data["candidates"]) > 0:
                    content = response_data["candidates"][0]["content"]["parts"][0]["text"]
                    return content.strip()
                else:
                    logger.error(f"Gemini API 响应格式异常: {response_data}")
                    raise Exception("Gemini API 响应格式异常")

        except asyncio.TimeoutError:
            logger.error("Gemini API 请求超时")
            raise
        except aiohttp.ClientError as e:
            logger.error(f"Gemini API 网络错误: {e}")
            raise
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Gemini API 响应解析错误: {e}")
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
            session = await self._get_session()

            model_name = GEMINI_MODEL
            if not model_name.startswith("models/"):
                model_name = f"models/{model_name}"

            url = f"{GEMINI_API_BASE}/{model_name}:generateContent?key={self.api_token}"

            request_data = {
                "contents": [
                    {
                        "parts": [{"text": "你好"}]
                    }
                ],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 10,
                }
            }

            async with session.post(url, json=request_data) as response:
                return response.status == 200

        except Exception as e:
            logger.error(f"Gemini API 连接测试失败: {e}")
            return False

    async def close(self):
        """关闭客户端。"""
        if self._session and not self._session.closed:
            await self._session.close()
