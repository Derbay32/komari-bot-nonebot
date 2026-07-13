"""LLM Provider 插件 - 提供统一的 LLM 调用接口（OpenAI 兼容格式）。"""

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Literal

from nonebot import logger
from nonebot.plugin import PluginMetadata, require

from .api import register_llm_provider_api
from .base_client import LLMCompletionResultSchema, UnifiedUsageSchema
from .config import Config
from .config_schema import DynamicConfigSchema
from .llm_logger import log_llm_call
from .openai_compatible_api import OpenAICompatibleClient
from .reply_log_reader import ReplyLogReader

__plugin_meta__ = PluginMetadata(
    name="llm_provider",
    description="通用 LLM API 提供者（OpenAI 兼容格式），集成 Komari Knowledge 知识库",
    usage="""
    llm_provider = require("llm_provider")

    # 基础用法
    response = await llm_provider.generate_text(
        prompt="你好",
        model="deepseek-chat",
    )

    # 多轮对话（OpenAI messages 格式）
    response = await llm_provider.generate_text_with_messages(
        messages=[
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ],
        model="deepseek-chat",
    )

    # 结构化输出请直接在 prompt 中明确要求 JSON 字段
    response = await llm_provider.generate_text(
        prompt="请返回 JSON，对象字段为 name 和 age",
        model="deepseek-chat",
    )
    """,
    config=Config,
)

__all__ = [
    "generate_completion",
    "generate_messages_completion",
    "generate_text",
    "generate_text_with_messages",
    "get_reply_log_reader",
    "register_llm_provider_api",
]

# 依赖插件
config_manager_plugin = require("config_manager")
knowledge_plugin = require("komari_knowledge")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "llm_provider", DynamicConfigSchema
)
_reply_log_reader = ReplyLogReader()
_RATE_LIMIT_WINDOW_SECONDS = 60.0


class _AsyncSlidingWindowRateLimiter:
    """基于单进程滑动窗口的异步 RPM 限流器。"""

    def __init__(self, limit_getter: Callable[[], int]) -> None:
        self._limit_getter = limit_getter
        self._condition = asyncio.Condition()
        self._timestamps: deque[float] = deque()

    def _prune(self, now: float) -> None:
        """移除当前窗口之外的请求时间戳。"""
        while self._timestamps and now - self._timestamps[0] >= _RATE_LIMIT_WINDOW_SECONDS:
            self._timestamps.popleft()

    async def wait(self) -> None:
        """等待直到当前滑动窗口存在可用请求额度。"""
        async with self._condition:
            while True:
                now = time.monotonic()
                self._prune(now)
                limit = max(1, int(self._limit_getter()))
                if len(self._timestamps) < limit:
                    self._timestamps.append(now)
                    self._condition.notify_all()
                    return

                delay = _RATE_LIMIT_WINDOW_SECONDS - (now - self._timestamps[0])
                try:
                    await asyncio.wait_for(
                        self._condition.wait(),
                        timeout=max(delay, 0.01),
                    )
                except TimeoutError:
                    continue


def _resolve_rate_limit_bucket(request_phase: str) -> Literal["summary", "chat"]:
    """根据 request_phase 选择互相独立的 RPM 限流桶。"""
    phase = request_phase.strip().lower()
    summary_prefixes = (
        "summary_",
        "profile_agent",
        "forgetting_",
        "interaction_event_summary",
        "chat_memory_summary",
        "group_history_summary",
    )
    chat_prefixes = (
        "normal_reply_",
        "vision_tool_",
        "vision_search_tool_",
        "search_tool_",
        "profile_tool_",
        "tool_",
        "query_rewrite",
        "memory_reply",
        "chat_reply",
    )

    if phase.startswith(summary_prefixes):
        return "summary"
    if phase.startswith(chat_prefixes):
        return "chat"

    logger.warning("[LLM Provider] 未识别 request_phase，按 chat RPM 限流: {}", phase or "-")
    return "chat"


def _get_summary_task_rpm_limit() -> int:
    """读取总结桶 RPM 配置，兼容测试替身与旧运行时缓存。"""
    return int(getattr(config_manager.get(), "summary_task_rpm_limit", 20))


def _get_chat_rpm_limit() -> int:
    """读取聊天桶 RPM 配置，兼容测试替身与旧运行时缓存。"""
    return int(getattr(config_manager.get(), "chat_rpm_limit", 60))


_summary_task_rate_limiter = _AsyncSlidingWindowRateLimiter(
    _get_summary_task_rpm_limit
)
_chat_rate_limiter = _AsyncSlidingWindowRateLimiter(_get_chat_rpm_limit)


async def _wait_for_llm_rate_limit(request_phase: str) -> None:
    """按请求阶段等待对应的 LLM RPM 额度。"""
    bucket = _resolve_rate_limit_bucket(request_phase)
    if bucket == "summary":
        await _summary_task_rate_limiter.wait()
    else:
        await _chat_rate_limiter.wait()


def get_reply_log_reader() -> ReplyLogReader:
    return _reply_log_reader


def _summarize_messages_payload(messages: list[dict[str, Any]]) -> dict[str, int]:
    """统计 messages 请求中的文本与图片体量。"""
    text_parts = 0
    text_chars = 0
    image_parts = 0
    image_url_chars = 0

    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            text_parts += 1
            text_chars += len(content)
            continue

        if not isinstance(content, list):
            continue

        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type == "text":
                text_parts += 1
                text_chars += len(str(part.get("text", "")))
            elif part_type == "image_url":
                image_parts += 1
                image_data = part.get("image_url")
                if isinstance(image_data, dict):
                    image_url_chars += len(str(image_data.get("url", "")))

    return {
        "turns": len(messages),
        "text_parts": text_parts,
        "text_chars": text_chars,
        "image_parts": image_parts,
        "image_url_chars": image_url_chars,
    }


def _build_log_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """构造写入 JSONL 的剩余调用参数，排除追踪专用字段。"""
    return {
        key: value
        for key, value in kwargs.items()
        if key not in {"request_trace_id", "request_phase"}
    }


def _build_prompt_log_input(
    *,
    trace_id: str,
    phase: str,
    prompt: str,
    system_instruction: str,
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict | None,
    enable_knowledge: bool,
    knowledge_query: str | None,
    knowledge_limit: int,
    kwargs: dict[str, Any],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    parallel_tool_calls: bool | None = None,
) -> dict[str, Any]:
    """构造 prompt 路径完整 JSONL 请求体。"""
    input_data: dict[str, Any] = {
        "trace_id": trace_id,
        "phase": phase,
        "prompt": prompt,
        "system_instruction": system_instruction,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "enable_knowledge": enable_knowledge,
        "knowledge_query": knowledge_query,
        "knowledge_limit": knowledge_limit,
        "kwargs": _build_log_kwargs(kwargs),
    }
    if tools is not None:
        input_data["tools"] = tools
    if tool_choice is not None:
        input_data["tool_choice"] = tool_choice
    if parallel_tool_calls is not None:
        input_data["parallel_tool_calls"] = parallel_tool_calls
    return input_data


def _build_messages_log_input(
    *,
    trace_id: str,
    payload_summary: dict[str, int],
    messages: list[dict],
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict | None,
    kwargs: dict[str, Any],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    parallel_tool_calls: bool | None = None,
) -> dict[str, Any]:
    """构造 messages 路径完整 JSONL 请求体。"""
    input_data: dict[str, Any] = {
        "trace_id": trace_id,
        "payload_summary": payload_summary,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": response_format,
        "kwargs": _build_log_kwargs(kwargs),
    }
    if tools is not None:
        input_data["tools"] = tools
    if tool_choice is not None:
        input_data["tool_choice"] = tool_choice
    if parallel_tool_calls is not None:
        input_data["parallel_tool_calls"] = parallel_tool_calls
    return input_data


def _get_client() -> OpenAICompatibleClient:
    """获取 LLM 客户端实例。"""
    config = config_manager.get()
    token = config.api_token
    if not token:
        raise ValueError("API Token 未配置，请在配置中设置 api_token")  # noqa: TRY003
    return OpenAICompatibleClient(
        token,
        base_url=str(config.api_base),
        timeout_seconds=float(config.timeout_seconds),
    )


def _get_completion_content(result: LLMCompletionResultSchema | str) -> str:
    """兼容测试替身与真实客户端的完成文本。"""
    if isinstance(result, str):
        return result
    return result.content


def _get_completion_reasoning_content(
    result: LLMCompletionResultSchema | str,
) -> str | None:
    """兼容测试替身与真实客户端的推理内容。"""
    if isinstance(result, str):
        return None
    return result.reasoning_content


def _get_completion_usage(
    result: LLMCompletionResultSchema | str,
) -> UnifiedUsageSchema | None:
    """兼容测试替身与真实客户端的用量信息。"""
    if isinstance(result, LLMCompletionResultSchema):
        return result.usage
    return None


async def generate_text(
    prompt: str,
    model: str,
    system_instruction: str | None = None,
    temperature: int | None = None,
    max_tokens: int | None = None,
    knowledge_query: str | None = None,
    knowledge_limit: int = 3,
    *,
    enable_knowledge: bool = False,
    response_format: dict | None = None,
    record_chat_log: bool = False,
    **kwargs,  # noqa: ANN003
) -> str:
    """生成文本（简单 prompt 模式）。

    Args:
        prompt: 用户提示词
        model: 模型名称
        system_instruction: 系统指令
        temperature: 温度参数
        max_tokens: 最大 token 数
        enable_knowledge: 是否启用知识库检索
        knowledge_query: 知识库查询文本
        knowledge_limit: 检索返回的知识数量上限
        response_format: OpenAI 兼容结构化输出参数；非空时下发到底层 LLM API
        record_chat_log: 是否记录聊天回复日志
        **kwargs: 其他参数

    Returns:
        生成的文本
    """
    client: OpenAICompatibleClient | None = None
    start_time = time.monotonic()
    request_trace_id = str(kwargs.get("request_trace_id", "")).strip()
    request_phase = str(kwargs.get("request_phase", "")).strip()
    final_system_instruction = system_instruction or ""

    try:
        # 知识库检索
        knowledge_context = ""
        if enable_knowledge:
            try:
                query = knowledge_query or prompt
                results = await knowledge_plugin.search_knowledge(
                    query, limit=knowledge_limit
                )
                if results:
                    knowledge_context = "\n".join(result.content for result in results)
                    logger.info(f"[LLM Provider] 已检索到 {len(results)} 条相关知识")
            except Exception as e:
                logger.warning(f"[LLM Provider] 知识库检索失败: {e}")

        # 构建系统指令：处理占位符
        placeholder = "{{DYNAMIC_KNOWLEDGE_BASE}}"
        final_system_instruction = (system_instruction or "").replace(
            placeholder, knowledge_context
        )
        if request_trace_id:
            logger.info(
                "[LLM Provider] 文本请求追踪: trace_id={} phase={} model={} prompt_chars={} system_chars={}",
                request_trace_id,
                request_phase or "-",
                model,
                len(prompt),
                len(final_system_instruction),
            )

        await _wait_for_llm_rate_limit(request_phase)
        client = _get_client()
        result = await client.generate_text(
            prompt=prompt,
            model=model,
            system_instruction=final_system_instruction,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            **kwargs,
        )
    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        if record_chat_log:
            await log_llm_call(
                method="generate_text",
                model=model,
                input_data=_build_prompt_log_input(
                    trace_id=request_trace_id,
                    phase=request_phase,
                    prompt=prompt,
                    system_instruction=final_system_instruction,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    enable_knowledge=enable_knowledge,
                    knowledge_query=knowledge_query,
                    knowledge_limit=knowledge_limit,
                    kwargs=kwargs,
                ),
                error=str(e),
                duration_ms=duration_ms,
            )
        logger.error(
            "[LLM Provider] 文本请求失败: trace_id={} phase={} error={}",
            request_trace_id or "-",
            request_phase or "-",
            e,
        )
        raise
    else:
        duration_ms = (time.monotonic() - start_time) * 1000
        content = _get_completion_content(result)
        reasoning_content = _get_completion_reasoning_content(result)
        usage = _get_completion_usage(result)
        if record_chat_log:
            await log_llm_call(
                method="generate_text",
                model=model,
                input_data=_build_prompt_log_input(
                    trace_id=request_trace_id,
                    phase=request_phase,
                    prompt=prompt,
                    system_instruction=final_system_instruction,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    enable_knowledge=enable_knowledge,
                    knowledge_query=knowledge_query,
                    knowledge_limit=knowledge_limit,
                    kwargs=kwargs,
                ),
                output=content,
                reasoning_content=reasoning_content,
                duration_ms=duration_ms,
                usage=usage,
            )
        return content
    finally:
        if client is not None:
            await client.close()


async def generate_completion(
    prompt: str,
    model: str,
    system_instruction: str | None = None,
    temperature: int | None = None,
    max_tokens: int | None = None,
    knowledge_query: str | None = None,
    knowledge_limit: int = 3,
    *,
    enable_knowledge: bool = False,
    response_format: dict | None = None,
    record_chat_log: bool = False,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    parallel_tool_calls: bool | None = None,
    **kwargs,  # noqa: ANN003
) -> LLMCompletionResultSchema:
    """生成统一完成结果。"""
    client: OpenAICompatibleClient | None = None
    start_time = time.monotonic()
    request_trace_id = str(kwargs.get("request_trace_id", "")).strip()
    request_phase = str(kwargs.get("request_phase", "")).strip()
    final_system_instruction = system_instruction or ""

    try:
        knowledge_context = ""
        if enable_knowledge:
            try:
                query = knowledge_query or prompt
                results = await knowledge_plugin.search_knowledge(
                    query, limit=knowledge_limit
                )
                if results:
                    knowledge_context = "\n".join(result.content for result in results)
                    logger.info(f"[LLM Provider] 已检索到 {len(results)} 条相关知识")
            except Exception as e:
                logger.warning(f"[LLM Provider] 知识库检索失败: {e}")

        placeholder = "{{DYNAMIC_KNOWLEDGE_BASE}}"
        final_system_instruction = (system_instruction or "").replace(
            placeholder, knowledge_context
        )
        if request_trace_id:
            logger.info(
                "[LLM Provider] Completion 请求追踪: trace_id={} phase={} model={} prompt_chars={} system_chars={} tools={}",
                request_trace_id,
                request_phase or "-",
                model,
                len(prompt),
                len(final_system_instruction),
                len(tools or []),
            )

        await _wait_for_llm_rate_limit(request_phase)
        client = _get_client()
        result = await client.generate_text(
            prompt=prompt,
            model=model,
            system_instruction=final_system_instruction,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            **kwargs,
        )
    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        if record_chat_log:
            await log_llm_call(
                method="generate_completion",
                model=model,
                input_data=_build_prompt_log_input(
                    trace_id=request_trace_id,
                    phase=request_phase,
                    prompt=prompt,
                    system_instruction=final_system_instruction,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    enable_knowledge=enable_knowledge,
                    knowledge_query=knowledge_query,
                    knowledge_limit=knowledge_limit,
                    kwargs=kwargs,
                    tools=tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                ),
                error=str(e),
                duration_ms=duration_ms,
            )
        raise
    else:
        duration_ms = (time.monotonic() - start_time) * 1000
        result.duration_ms = duration_ms
        if record_chat_log:
            await log_llm_call(
                method="generate_completion",
                model=model,
                input_data=_build_prompt_log_input(
                    trace_id=request_trace_id,
                    phase=request_phase,
                    prompt=prompt,
                    system_instruction=final_system_instruction,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    enable_knowledge=enable_knowledge,
                    knowledge_query=knowledge_query,
                    knowledge_limit=knowledge_limit,
                    kwargs=kwargs,
                    tools=tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                ),
                output=json.dumps(result.model_dump(), ensure_ascii=False),
                reasoning_content=result.reasoning_content,
                duration_ms=duration_ms,
                usage=result.usage,
            )
        return result
    finally:
        if client is not None:
            await client.close()


async def generate_text_with_messages(
    messages: list[dict],
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    *,
    record_chat_log: bool = False,
    **kwargs,  # noqa: ANN003
) -> str:
    """使用 OpenAI 格式 messages 生成文本（支持多模态）。

    Args:
        messages: 消息列表 [{role, content}]，content 可以是字符串或数组（OpenAI Vision 格式）
        model: 模型名称
        temperature: 温度参数
        max_tokens: 最大 token 数
        response_format: OpenAI 兼容结构化输出参数；非空时下发到底层 LLM API
        record_chat_log: 是否记录聊天回复日志
        **kwargs: 其他参数

    Returns:
        生成的文本
    """
    client: OpenAICompatibleClient | None = None
    start_time = time.monotonic()
    request_trace_id = str(kwargs.get("request_trace_id", "")).strip()
    request_phase = str(kwargs.get("request_phase", "")).strip()
    payload_summary = _summarize_messages_payload(messages)

    try:
        logger.info(
            "[LLM Provider] Messages 请求追踪: trace_id={} phase={} model={} turns={} text_parts={} text_chars={} image_parts={} image_url_chars={}",
            request_trace_id or "-",
            request_phase or "-",
            model,
            payload_summary["turns"],
            payload_summary["text_parts"],
            payload_summary["text_chars"],
            payload_summary["image_parts"],
            payload_summary["image_url_chars"],
        )
        await _wait_for_llm_rate_limit(request_phase)
        client = _get_client()
        result = await client.generate_text_with_messages(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            **kwargs,
        )
    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        if record_chat_log:
            await log_llm_call(
                method="generate_text_with_messages",
                model=model,
                input_data=_build_messages_log_input(
                    trace_id=request_trace_id,
                    payload_summary=payload_summary,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    kwargs=kwargs,
                ),
                error=str(e),
                duration_ms=duration_ms,
            )
        logger.error(
            "[LLM Provider] Messages 请求失败: trace_id={} model={} error={} payload={}",
            request_trace_id or "-",
            model,
            e,
            payload_summary,
        )
        raise
    else:
        duration_ms = (time.monotonic() - start_time) * 1000
        content = _get_completion_content(result)
        reasoning_content = _get_completion_reasoning_content(result)
        usage = _get_completion_usage(result)
        if record_chat_log:
            await log_llm_call(
                method="generate_text_with_messages",
                model=model,
                input_data=_build_messages_log_input(
                    trace_id=request_trace_id,
                    payload_summary=payload_summary,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    kwargs=kwargs,
                ),
                output=content,
                reasoning_content=reasoning_content,
                duration_ms=duration_ms,
                usage=usage,
            )
        return content
    finally:
        if client is not None:
            await client.close()


async def generate_messages_completion(
    messages: list[dict],
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    *,
    record_chat_log: bool = False,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    parallel_tool_calls: bool | None = None,
    **kwargs,  # noqa: ANN003
) -> LLMCompletionResultSchema:
    """使用 messages 生成统一完成结果。"""
    client: OpenAICompatibleClient | None = None
    start_time = time.monotonic()
    request_trace_id = str(kwargs.get("request_trace_id", "")).strip()
    request_phase = str(kwargs.get("request_phase", "")).strip()
    payload_summary = _summarize_messages_payload(messages)

    try:
        logger.info(
            "[LLM Provider] Completion(messages) 请求追踪: trace_id={} phase={} model={} turns={} text_parts={} text_chars={} image_parts={} image_url_chars={} tools={}",
            request_trace_id or "-",
            request_phase or "-",
            model,
            payload_summary["turns"],
            payload_summary["text_parts"],
            payload_summary["text_chars"],
            payload_summary["image_parts"],
            payload_summary["image_url_chars"],
            len(tools or []),
        )
        await _wait_for_llm_rate_limit(request_phase)
        client = _get_client()
        result = await client.generate_text_with_messages(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            **kwargs,
        )
    except Exception as e:
        duration_ms = (time.monotonic() - start_time) * 1000
        if record_chat_log:
            await log_llm_call(
                method="generate_messages_completion",
                model=model,
                input_data=_build_messages_log_input(
                    trace_id=request_trace_id,
                    payload_summary=payload_summary,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    kwargs=kwargs,
                    tools=tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                ),
                error=str(e),
                duration_ms=duration_ms,
            )
        raise
    else:
        duration_ms = (time.monotonic() - start_time) * 1000
        result.duration_ms = duration_ms
        if record_chat_log:
            await log_llm_call(
                method="generate_messages_completion",
                model=model,
                input_data=_build_messages_log_input(
                    trace_id=request_trace_id,
                    payload_summary=payload_summary,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    kwargs=kwargs,
                    tools=tools,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                ),
                output=json.dumps(result.model_dump(), ensure_ascii=False),
                reasoning_content=result.reasoning_content,
                duration_ms=duration_ms,
                usage=result.usage,
            )
        return result
    finally:
        if client is not None:
            await client.close()


async def test_connection() -> bool:
    """测试 API 连接。

    Returns:
        连接是否成功
    """
    config = config_manager.get()
    token = config.api_token
    if not token:
        logger.warning("API Token 未配置，跳过连接测试")
        return False

    client = OpenAICompatibleClient(
        token,
        base_url=str(config.api_base),
        timeout_seconds=float(config.timeout_seconds),
    )
    try:
        return await client.test_connection()
    finally:
        await client.close()
