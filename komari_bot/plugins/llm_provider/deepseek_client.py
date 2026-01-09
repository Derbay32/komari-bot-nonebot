"""DeepSeek API 客户端。"""

import json

import aiohttp
from nonebot import logger
from nonebot.plugin import require

from .base_client import BaseLLMClient
from .config_schema import DynamicConfigSchema
from .types import StructuredOutputSchema

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "llm_provider", DynamicConfigSchema
)


class DeepSeekClient(BaseLLMClient):
    """DeepSeek API 客户端。"""

    def __init__(self, api_token: str) -> None:
        """初始化客户端。

        Args:
            api_token: DeepSeek API Token
        """
        self.api_token = api_token
        self.session: aiohttp.ClientSession | None = None

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
        model: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        # 结构化输出参数
        response_schema: StructuredOutputSchema | None = None,
        response_json_schema: object | None = None,  # noqa: ARG002 - DeepSeek 不使用
        response_format: dict | None = None,
        **kwargs,  # noqa: ANN003
    ) -> str:
        """生成文本（支持 JSON 模式）。

        Args:
            prompt: 用户提示词
            model: 模型名称
            system_instruction: 系统指令
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_schema: Pydantic 模型或 JSON Schema（触发 JSON 模式）
            response_json_schema: JSON Schema 字典（DeepSeek 不使用）
            response_format: Response format dict
            **kwargs: 其他参数（如 frequency_penalty）

        Returns:
            生成的文本（使用 JSON 模式时为 JSON 字符串）
        """
        config = config_manager.get()
        try:
            logger.debug(
                f"DeepSeek API 请求:\n"
                f"  model: {model}\n"
                f"  temperature: {temperature if temperature is not None else config.deepseek_temperature}\n"
                f"  max_tokens: {max_tokens if max_tokens is not None else config.deepseek_max_tokens}\n"
                f"  frequency_penalty: {kwargs.get('frequency_penalty', config.deepseek_frequency_penalty)}\n"
                f"  system_instruction: {system_instruction}\n"
                f"  prompt: {prompt}\n"
                f"  json_mode: {response_schema is not None or response_format is not None}"
            )

            session = await self._get_session()

            # 构建消息
            messages = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            messages.append({"role": "user", "content": prompt})

            # 构建请求数据
            request_data = {
                "model": model,
                "messages": messages,
                "temperature": temperature
                if temperature is not None
                else config.deepseek_temperature,
                "max_tokens": max_tokens
                if max_tokens is not None
                else config.deepseek_max_tokens,
                "frequency_penalty": kwargs.get(
                    "frequency_penalty", config.deepseek_frequency_penalty
                ),
                "stream": False,
            }

            # 处理 JSON 模式
            if response_schema is not None or response_format is not None:
                # DeepSeek 只支持简单 JSON 模式
                request_data["response_format"] = response_format or {
                    "type": "json_object"
                }

            # 发送 API 请求
            async with session.post(
                config.deepseek_api_base, json=request_data
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"DeepSeek API 请求失败: {response.status} - {error_text}"
                    )
                    raise Exception(f"DeepSeek API 请求失败: {response.status}")  # noqa: TRY301,TRY002,TRY003

                response_data = await response.json()

                # 解析响应
                if "choices" in response_data and len(response_data["choices"]) > 0:
                    content = response_data["choices"][0]["message"]["content"]
                    logger.debug(f"DeepSeek API 响应: {content}")
                    return content.strip()
                logger.error(f"DeepSeek API 响应格式异常: {response_data}")
                raise Exception("DeepSeek API 响应格式异常")  # noqa: TRY301,TRY002,TRY003

        except TimeoutError:
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

    async def generate_text_with_contents(
        self,
        contents: list[dict],
        model: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        # 结构化输出参数
        response_schema: StructuredOutputSchema | None = None,
        response_json_schema: object | None = None,  # noqa: ARG002 - DeepSeek 不使用
        response_format: dict | None = None,
        **kwargs,  # noqa: ANN003
    ) -> str:
        """使用 contents 列表生成文本（多轮对话）。

        Args:
            contents: contents 列表，每个元素为 {"role": "user"/"model", "parts": [{"text": "..."}]}
            model: 模型名称
            system_instruction: 系统指令
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_schema: Pydantic 模型或 JSON Schema（触发 JSON 模式）
            response_json_schema: JSON Schema 字典（DeepSeek 不使用）
            response_format: Response format dict
            **kwargs: 其他参数（如 frequency_penalty）

        Returns:
            生成的文本（使用 JSON 模式时为 JSON 字符串）
        """
        from typing import Any

        config = config_manager.get()
        try:
            session = await self._get_session()

            # 构建 messages 数组
            messages: list[dict[str, Any]] = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})

            # 将 contents 列表转换为 DeepSeek 格式
            # contents 格式: {"role": "user"/"model", "parts": [{"text": "..."}]}
            # DeepSeek 格式: {"role": "user"/"assistant", "content": "..."}
            for content in contents:
                role = content["role"]
                # 健壮性检查：确保 parts 和 text 存在
                if not content.get("parts") or len(content["parts"]) == 0:
                    logger.warning(f"contents 中存在空的 parts: {content}")
                    continue
                text = content["parts"][0].get("text", "")
                # 转换 role: "model" -> "assistant"
                deepseek_role = "assistant" if role == "model" else role
                messages.append({"role": deepseek_role, "content": text})

            logger.debug(
                f"DeepSeek API 请求 (多轮):\n"
                f"  model: {model}\n"
                f"  temperature: {temperature if temperature is not None else config.deepseek_temperature}\n"
                f"  max_tokens: {max_tokens if max_tokens is not None else config.deepseek_max_tokens}\n"
                f"  frequency_penalty: {kwargs.get('frequency_penalty', config.deepseek_frequency_penalty)}\n"
                f"  system_instruction: {system_instruction}\n"
                f"  messages: {len(messages)} turns\n"
                f"  json_mode: {response_schema is not None or response_format is not None}"
            )

            # 构建请求数据
            request_data = {
                "model": model,
                "messages": messages,
                "temperature": temperature
                if temperature is not None
                else config.deepseek_temperature,
                "max_tokens": max_tokens
                if max_tokens is not None
                else config.deepseek_max_tokens,
                "frequency_penalty": kwargs.get(
                    "frequency_penalty", config.deepseek_frequency_penalty
                ),
                "stream": False,
            }

            # 处理 JSON 模式
            if response_schema is not None or response_format is not None:
                # DeepSeek 只支持简单 JSON 模式
                request_data["response_format"] = response_format or {
                    "type": "json_object"
                }

            # 发送 API 请求
            async with session.post(
                config.deepseek_api_base, json=request_data
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"DeepSeek API 请求失败: {response.status} - {error_text}"
                    )
                    raise Exception(f"DeepSeek API 请求失败: {response.status}")  # noqa: TRY301,TRY002,TRY003

                response_data = await response.json()

                # 解析响应
                if "choices" in response_data and len(response_data["choices"]) > 0:
                    content = response_data["choices"][0]["message"]["content"]
                    logger.debug(f"DeepSeek API 响应: {content}")
                    return content.strip()
                logger.error(f"DeepSeek API 响应格式异常: {response_data}")
                raise Exception("DeepSeek API 响应格式异常")  # noqa: TRY301,TRY002,TRY003

        except TimeoutError:
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
        config = config_manager.get()
        try:
            session = await self._get_session()

            request_data = {
                "model": config.deepseek_model,
                "messages": [{"role": "user", "content": "你好"}],
                "temperature": 0.1,
                "max_tokens": 10,
            }

            async with session.post(
                config.deepseek_api_base, json=request_data
            ) as response:
                return response.status == 200

        except Exception as e:
            logger.error(f"DeepSeek API 连接测试失败: {e}")
            return False

    async def close(self) -> None:
        """关闭客户端。"""
        if self.session and not self.session.closed:
            await self.session.close()
