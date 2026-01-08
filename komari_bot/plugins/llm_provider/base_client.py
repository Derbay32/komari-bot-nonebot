"""LLM 客户端抽象基类。"""

from abc import ABC, abstractmethod


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
        **kwargs,  # noqa: ANN003
    ) -> str:
        """生成文本。

        Args:
            prompt: 用户提示词
            system_instruction: 系统指令
            temperature: 温度参数
            max_tokens: 最大 token 数
            **kwargs: 其他 provider 特定参数

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
