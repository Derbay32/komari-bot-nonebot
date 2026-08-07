"""LLM 客户端抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from komari_bot.llm.llm_protocol import RequestApi
    from komari_bot.llm.untrusted_context import UntrustedContext


class UnifiedUsageSchema(BaseModel):
    """统一的 LLM 用量模型。

    所有字段为 int | None，以 None 表示后端未报告该字段，
    避免用 0 冒充缺失数据。
    """

    input_tokens: int | None = Field(default=None, description="输入 token 数")
    cached_input_tokens: int | None = Field(
        default=None, description="缓存命中的输入 token 数"
    )
    cache_miss_input_tokens: int | None = Field(
        default=None, description="缓存未命中的输入 token 数"
    )
    output_tokens: int | None = Field(default=None, description="输出 token 数")
    reasoning_output_tokens: int | None = Field(
        default=None, description="推理输出 token 数"
    )
    total_tokens: int | None = Field(default=None, description="总 token 数")


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


class LLMProviderContinuationSchema(BaseModel):
    """Responses 协议的不透明续接项。

    只在当前业务任务的多轮工具循环内存活：不写入业务数据库、
    不跨 QQ 消息复用、不属于持久协议。Chat 路径不产生此对象。
    """

    api: Literal["responses"] = Field(description="协议鉴别（Chat 无此对象）")
    output_items: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "序列化的 Responses output items（message / function_call / "
            "reasoning，含加密推理内容，原样回传）"
        ),
    )


CONTINUATION_METADATA_KEY = "_llm_continuation"
"""assistant 消息上附着 continuation 的内部元数据键。

Chat 请求构建器会剥离全部 `_` 前缀内部键（永不发往 Chat 端点）；
Responses 请求构建器校验协议后原样展开 output items。
"""


def build_assistant_message(
    completion: LLMCompletionResultSchema,
) -> dict[str, Any]:
    """统一助手消息构造函数。

    把统一完成结果转为工具循环内部 messages 条目：包含 assistant 文本与
    tool_calls（Chat 格式）；completion 的 continuation（如有）附着为内部
    元数据，Chat 构建器剥离、Responses 构建器原样展开。
    无工具但需重试的 assistant 输出同样经此构造函数附着 continuation。
    """
    message: dict[str, Any] = {
        "role": "assistant",
        "content": completion.content,
    }
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": tool_call.id or tool_call.function.name,
                "type": tool_call.type,
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.raw_arguments
                    or tool_call.function.arguments,
                },
            }
            for tool_call in completion.tool_calls
        ]
    continuation = getattr(completion, "continuation", None)
    if continuation is not None:
        message[CONTINUATION_METADATA_KEY] = continuation.model_dump()
    return message


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
    usage: UnifiedUsageSchema | None = Field(default=None, description="用量信息")
    duration_ms: float | None = Field(default=None, description="调用耗时（毫秒）")
    continuation: LLMProviderContinuationSchema | None = Field(
        default=None,
        description="Responses 协议续接项；Chat Completions 路径恒为 None",
    )


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
        untrusted_contexts: list[UntrustedContext] | None = None,
        request_api: RequestApi | None = None,
        stream_enabled: bool | None = None,
        **kwargs,  # noqa: ANN003
    ) -> LLMCompletionResultSchema:
        """生成文本。

        Args:
            prompt: 用户提示词
            model: 模型名称
            system_instruction: 系统指令
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: OpenAI 兼容结构化输出参数；非空时下发到底层 LLM API
            tools: 可用工具定义
            tool_choice: 工具选择策略
            parallel_tool_calls: 是否允许并行工具调用
            untrusted_contexts: 由 provider 作为独立数据块注入的不可信上下文
            request_api: 请求 API（None 时解析默认槽位配置）
            stream_enabled: 是否启用流式传输（None 时解析默认槽位配置）
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
        untrusted_contexts: list[UntrustedContext] | None = None,
        request_api: RequestApi | None = None,
        stream_enabled: bool | None = None,
        **kwargs,  # noqa: ANN003
    ) -> LLMCompletionResultSchema:
        """使用 OpenAI 格式 messages 生成文本（支持多模态）。

        Args:
            messages: 消息列表 [{role, content}]，content 可以是字符串或数组（OpenAI Vision 格式）
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_format: OpenAI 兼容结构化输出参数；非空时下发到底层 LLM API
            tools: 可用工具定义
            tool_choice: 工具选择策略
            parallel_tool_calls: 是否允许并行工具调用
            untrusted_contexts: 由 provider 作为独立数据块注入的不可信上下文
            request_api: 请求 API（None 时解析默认槽位配置）
            stream_enabled: 是否启用流式传输（None 时解析默认槽位配置）
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
