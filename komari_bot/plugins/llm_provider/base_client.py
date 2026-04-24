"""LLM 客户端抽象基类。"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class LLMToolCallFunctionSchema(BaseModel):
    """LLM 工具调用中的函数信息。"""

    name: str = Field(description="工具函数名")
    arguments: str = Field(default="", description="原始 JSON 参数字符串")


class LLMToolCallSchema(BaseModel):
    """LLM 工具调用结构。"""

    id: str | None = Field(default=None, description="工具调用 ID")
    type: str = Field(default="function", description="工具调用类型")
    function: LLMToolCallFunctionSchema = Field(description="函数调用信息")
    raw_arguments: str = Field(default="", description="模型返回的原始参数字符串")
    parsed_arguments: dict[str, Any] | None = Field(
        default=None, description="安全解析后的参数"
    )


class LLMCompletionResultSchema(BaseModel):
    """统一的 LLM 完成结果。"""

    content: str = Field(default="", description="文本内容")
    reasoning_content: str | None = Field(
        default=None, description="思考模式返回的推理内容"
    )
    tool_calls: list[LLMToolCallSchema] = Field(
        default_factory=list, description="工具调用列表"
    )
    finish_reason: str | None = Field(default=None, description="结束原因")


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
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs,  # noqa: ANN003
    ) -> LLMCompletionResultSchema:
        """生成文本。

        Args:
            prompt: 用户提示词
            model: 模型名称
            system_instruction: 系统指令
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: 为兼容旧调用保留；当前不会下发到模型，请通过 prompt 指定输出格式
            tools: 可用工具定义
            tool_choice: 工具选择策略
            parallel_tool_calls: 是否允许并行工具调用
            **kwargs: 其他 provider 特定参数

        Returns:
            统一完成结果
        """

    @abstractmethod
    async def generate_text_with_messages(
        self,
        messages: list[dict[str, Any]],
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        **kwargs,  # noqa: ANN003
    ) -> LLMCompletionResultSchema:
        """使用 OpenAI 格式 messages 生成文本（支持多模态）。

        Args:
            messages: 消息列表 [{role, content}]，content 可以是字符串或数组（OpenAI Vision 格式）
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: 为兼容旧调用保留；当前不会下发到模型，请通过 prompt 指定输出格式
            tools: 可用工具定义
            tool_choice: 工具选择策略
            parallel_tool_calls: 是否允许并行工具调用
            **kwargs: 其他参数

        Returns:
            统一完成结果
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
