"""LLM 客户端抽象基类。"""
from abc import ABC, abstractmethod
from typing import Optional


class BaseLLMClient(ABC):
    """LLM 客户端抽象基类，定义统一接口。"""

    @abstractmethod
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
            temperature: 温度参数
            max_tokens: 最大 token 数
            **kwargs: 其他 provider 特定参数

        Returns:
            生成的文本
        """
        pass

    @abstractmethod
    async def test_connection(self) -> bool:
        """测试 API 连接。

        Returns:
            连接是否成功
        """
        pass

    @abstractmethod
    async def close(self):
        """关闭客户端并清理资源。"""
        pass
