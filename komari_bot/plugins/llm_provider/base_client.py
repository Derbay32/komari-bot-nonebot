"""LLM 客户端抽象基类。"""

from abc import ABC, abstractmethod
from typing import Any


class BaseLLMClient(ABC):
    """LLM 客户端抽象基类，定义统一接口。"""

    @abstractmethod
    async def generate_text(
        self,
        prompt: str,
        model: str,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs,  # noqa: ANN003
    ) -> str:
        """生成文本。

        Args:
            prompt: 用户提示词
            model: 模型名称
            system_instruction: 系统指令
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: 为兼容旧调用保留；当前不会下发到模型，请通过 prompt 指定输出格式
            **kwargs: 其他 provider 特定参数

        Returns:
            生成的文本
        """

    @abstractmethod
    async def generate_text_with_messages(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        **kwargs,  # noqa: ANN003
    ) -> str:
        """使用 OpenAI 格式 messages 生成文本（支持多模态）。

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

    @abstractmethod
    async def test_connection(self) -> bool:
        """测试 API 连接。

        Returns:
            连接是否成功
        """

    @abstractmethod
    async def close(self) -> None:
        """关闭客户端并清理资源。"""
