"""LLM 客户端抽象基类。"""

from abc import ABC, abstractmethod
from typing import Any

from .types import StructuredOutputSchema


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
        # 结构化输出参数（可选）
        response_schema: StructuredOutputSchema | None = None,
        response_json_schema: dict[str, Any] | None = None,
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
            response_schema: Pydantic 模型或 JSON Schema (Gemini/DeepSeek)
            response_json_schema: JSON Schema 字典 (Gemini only)
            response_format: Response format dict (DeepSeek only)
            **kwargs: 其他 provider 特定参数

        Returns:
            生成的文本（使用结构化输出时为 JSON 字符串）
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
