"""DeepSeek API 客户端。"""

from typing import Never

from nonebot import logger
from nonebot.plugin import require
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI, OpenAIError

from .base_client import BaseLLMClient
from .config_schema import DynamicConfigSchema

# 依赖 config_manager 插件
config_manager_plugin = require("config_manager")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "llm_provider", DynamicConfigSchema
)


class DeepSeekClient(BaseLLMClient):
    """DeepSeek API 客户端。"""

    _INVALID_RESPONSE_MESSAGE = "DeepSeek API 响应格式异常"

    def __init__(
        self,
        api_token: str,
        base_url: str,
        timeout_seconds: float = 300.0,
    ) -> None:
        """初始化客户端。

        Args:
            api_token: DeepSeek API Token
            base_url: OpenAI 兼容 API Base URL
            timeout_seconds: 请求总超时时间（秒）
        """
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds
        self.base_url = base_url
        self.client = AsyncOpenAI(
            api_key=api_token,
            base_url=base_url,
            timeout=timeout_seconds,
        )

    @staticmethod
    def _resolve_reasoning_effort(
        config: DynamicConfigSchema, **kwargs: object
    ) -> str | None:
        """解析 OpenAI 兼容的 reasoning_effort 请求参数。"""
        raw_value = kwargs.get("reasoning_effort", config.deepseek_reasoning_effort)
        if raw_value is None:
            return None
        value = str(raw_value).strip()
        return value or None

    @classmethod
    def _raise_invalid_response(cls) -> "Never":
        """抛出响应格式异常。"""
        raise RuntimeError(cls._INVALID_RESPONSE_MESSAGE)

    async def generate_text(
        self,
        prompt: str,
        model: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
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
            response_format: 为兼容旧调用保留；当前不会下发到模型，请通过 prompt 指定输出格式
            **kwargs: 其他参数（如 frequency_penalty）

        Returns:
            生成的文本
        """
        config = config_manager.get()
        try:
            reasoning_effort = self._resolve_reasoning_effort(config, **kwargs)
            logger.debug(
                f"DeepSeek API 请求:\n"
                f"  model: {model}\n"
                f"  temperature: {temperature if temperature is not None else config.deepseek_temperature}\n"
                f"  max_tokens: {max_tokens if max_tokens is not None else config.deepseek_max_tokens}\n"
                f"  reasoning_effort: {reasoning_effort}\n"
                f"  frequency_penalty: {kwargs.get('frequency_penalty', config.deepseek_frequency_penalty)}\n"
                f"  system_instruction: {system_instruction}\n"
                f"  prompt: {prompt}"
            )
            del response_format

            messages = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            messages.append({"role": "user", "content": prompt})

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
            }

            if reasoning_effort is not None:
                request_data["reasoning_effort"] = reasoning_effort

            response = await self.client.chat.completions.create(**request_data)

            if response.choices:
                content = response.choices[0].message.content
                if content is not None:
                    logger.debug(f"DeepSeek API 响应: {content}")
                    return content.strip()
            logger.error(f"DeepSeek API 响应格式异常: {response}")
            self._raise_invalid_response()

        except APITimeoutError:
            logger.error("DeepSeek API 请求超时")
            raise
        except APIConnectionError as e:
            logger.error(f"DeepSeek API 网络错误: {e}")
            raise
        except OpenAIError as e:
            logger.error(f"DeepSeek API 调用失败: {e}")
            raise
        except Exception as e:
            logger.error(f"DeepSeek API 未知错误: {e}")
            raise

    async def generate_text_with_messages(
        self,
        messages: list[dict],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        **kwargs,  # noqa: ANN003
    ) -> str:
        """使用 OpenAI 格式 messages 直接生成文本（支持多模态）。

        Args:
            messages: 消息列表 [{role, content}]，content 可以是字符串或数组（OpenAI Vision 格式）
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: 为兼容旧调用保留；当前不会下发到模型，请通过 prompt 指定输出格式
            **kwargs: 其他参数

        Returns:
            生成的文本
        """
        config = config_manager.get()
        try:
            reasoning_effort = self._resolve_reasoning_effort(config, **kwargs)
            del response_format

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
            }

            if reasoning_effort is not None:
                request_data["reasoning_effort"] = reasoning_effort

            logger.debug(
                f"DeepSeek API 请求 (messages):\n"
                f"  model: {model}\n"
                f"  messages: {len(messages)} turns\n"
                f"  temperature: {request_data['temperature']}\n"
                f"  max_tokens: {request_data['max_tokens']}\n"
                f"  reasoning_effort: {reasoning_effort}"
            )

            response = await self.client.chat.completions.create(**request_data)

            if response.choices:
                content = response.choices[0].message.content
                if content is not None:
                    logger.debug(f"DeepSeek API 响应: {content[:200]}...")
                    return content.strip()
            logger.error(f"DeepSeek API 响应格式异常: {response}")
            self._raise_invalid_response()

        except APITimeoutError:
            logger.error("DeepSeek API 请求超时")
            raise
        except APIConnectionError as e:
            logger.error(f"DeepSeek API 网络错误: {e}")
            raise
        except OpenAIError as e:
            logger.error(f"DeepSeek API 调用失败: {e}")
            raise

    async def test_connection(self) -> bool:
        """测试 API 连接。

        Returns:
            连接是否成功
        """
        config = config_manager.get()
        try:
            await self.client.chat.completions.create(
                model=config.deepseek_model,
                messages=[{"role": "user", "content": "你好"}],
                temperature=0.1,
                max_tokens=10,
            )
        except Exception as e:
            logger.error(f"DeepSeek API 连接测试失败: {e}")
            return False
        else:
            return True

    async def close(self) -> None:
        """关闭客户端。"""
        await self.client.close()
