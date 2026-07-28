"""LLM Provider 插件 - 提供统一的 LLM 调用接口（OpenAI 兼容格式）。"""

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Literal

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.untrusted_context import UntrustedContext

from .base_client import LLMCompletionResultSchema
from .client_pool import ClientSettings, LLMClientPool
from .config import Config
from .config_schema import DynamicConfigSchema
from .openai_compatible_api import OpenAICompatibleClient

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
    "UntrustedContext",
    "generate_completion",
    "generate_messages_completion",
    "generate_text",
    "generate_text_with_messages",
]

# 依赖插件
config_manager_plugin = require("config_manager")
knowledge_plugin = require("komari_knowledge")

# 获取配置管理器
config_manager = config_manager_plugin.get_config_manager(
    "llm_provider", DynamicConfigSchema
)
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_KNOWLEDGE_PLACEHOLDER = "{{DYNAMIC_KNOWLEDGE_BASE}}"
_KNOWLEDGE_CONTEXT_NOTICE = "相关知识已作为独立的不可信数据块提供。"
_MAX_KNOWLEDGE_CONTEXTS = 10
_MAX_KNOWLEDGE_CONTEXT_CHARS = 4_000


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


def _estimate_base64_data_url_bytes(image_url: str) -> int:
    """不解码图片正文，按 base64 长度估算原始字节数。"""
    header, separator, payload = image_url.partition(",")
    if not separator or ";base64" not in header.lower():
        return 0

    compact_payload = "".join(payload.split())
    padding = len(compact_payload) - len(compact_payload.rstrip("="))
    return max(len(compact_payload) * 3 // 4 - padding, 0)


def _summarize_messages_payload(
    messages: list[dict[str, Any]],
) -> dict[str, int | str]:
    """统计 messages 请求中的文本与图片体量。"""
    text_parts = 0
    text_chars = 0
    image_parts = 0
    image_data_url_parts = 0
    image_remote_url_parts = 0
    image_url_chars = 0
    image_bytes = 0

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
                    image_url = str(image_data.get("url", ""))
                    image_url_chars += len(image_url)
                    if image_url.startswith("data:"):
                        image_data_url_parts += 1
                        image_bytes += _estimate_base64_data_url_bytes(image_url)
                    elif image_url:
                        image_remote_url_parts += 1

    return {
        "turns": len(messages),
        "text_parts": text_parts,
        "text_chars": text_chars,
        "image_parts": image_parts,
        "image_data_url_parts": image_data_url_parts,
        "image_remote_url_parts": image_remote_url_parts,
        "image_url_chars": image_url_chars,
        "image_bytes": image_bytes,
    }


def _get_client_settings() -> ClientSettings:
    """读取影响 HTTP 连接的动态配置快照。"""
    return ClientSettings.from_config(config_manager.get())


def _create_client(settings: ClientSettings) -> OpenAICompatibleClient:
    """按配置快照创建候选客户端。"""
    return OpenAICompatibleClient(
        settings.api_token,
        base_url=settings.base_url,
        timeout_seconds=settings.timeout_seconds,
    )


_client_pool = LLMClientPool(
    settings_getter=_get_client_settings,
    client_factory=_create_client,
)


def _get_completion_content(result: LLMCompletionResultSchema | str) -> str:
    """兼容测试替身与真实客户端的完成文本。"""
    if isinstance(result, str):
        return result
    return result.content


async def _load_knowledge_contexts(
    *,
    enabled: bool,
    query: str,
    limit: int,
) -> list[UntrustedContext]:
    """检索知识并保留来源信息，不拼入 system prompt。"""
    if not enabled:
        return []

    try:
        results = await knowledge_plugin.search_knowledge(query, limit=limit)
    except Exception as exc:
        logger.warning(
            "[LLM Provider] 知识库检索失败: error_type={}",
            type(exc).__name__,
        )
        return []

    contexts: list[UntrustedContext] = []
    for index, result in enumerate(results[:_MAX_KNOWLEDGE_CONTEXTS], start=1):
        content = getattr(result, "content", None)
        if not isinstance(content, str) or not content.strip():
            continue
        contexts.append(
            UntrustedContext(
                source_type="knowledge",
                source_id=(
                    f"{getattr(result, 'source', 'unknown')}:"
                    f"{getattr(result, 'id', index)}"
                ),
                content=content,
                trust_level="low",
                max_chars=_MAX_KNOWLEDGE_CONTEXT_CHARS,
            )
        )
    if contexts:
        logger.info("[LLM Provider] 已检索到 {} 条相关知识", len(contexts))
    return contexts


def _prepare_untrusted_request_contexts(
    *,
    system_instruction: str | None,
    provided_contexts: list[UntrustedContext] | None,
    knowledge_contexts: list[UntrustedContext],
) -> tuple[str, list[UntrustedContext]]:
    """移除旧知识占位符，并合并调用方与检索上下文。"""
    contexts = [*(provided_contexts or []), *knowledge_contexts]
    replacement = _KNOWLEDGE_CONTEXT_NOTICE if knowledge_contexts else ""
    final_system_instruction = (system_instruction or "").replace(
        _KNOWLEDGE_PLACEHOLDER,
        replacement,
    )
    return final_system_instruction, contexts


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
    untrusted_contexts: list[UntrustedContext] | None = None,
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
        **kwargs: 其他参数

    Returns:
        生成的文本
    """
    request_trace_id = str(kwargs.get("request_trace_id", "")).strip()
    request_phase = str(kwargs.get("request_phase", "")).strip()
    final_system_instruction = system_instruction or ""

    try:
        knowledge_contexts = await _load_knowledge_contexts(
            enabled=enable_knowledge,
            query=knowledge_query or prompt,
            limit=knowledge_limit,
        )
        final_system_instruction, request_contexts = (
            _prepare_untrusted_request_contexts(
                system_instruction=system_instruction,
                provided_contexts=untrusted_contexts,
                knowledge_contexts=knowledge_contexts,
            )
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
        async with _client_pool.acquire() as client:
            result = await client.generate_text(
                prompt=prompt,
                model=model,
                system_instruction=final_system_instruction,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                untrusted_contexts=request_contexts,
                **kwargs,
            )
    except Exception as e:
        logger.error(
            "[LLM Provider] 文本请求失败: trace_id={} phase={} error_type={}",
            request_trace_id or "-",
            request_phase or "-",
            type(e).__name__,
        )
        raise
    else:
        return _get_completion_content(result)


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
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    parallel_tool_calls: bool | None = None,
    untrusted_contexts: list[UntrustedContext] | None = None,
    **kwargs,  # noqa: ANN003
) -> LLMCompletionResultSchema:
    """生成统一完成结果。"""
    start_time = time.monotonic()
    request_trace_id = str(kwargs.get("request_trace_id", "")).strip()
    request_phase = str(kwargs.get("request_phase", "")).strip()
    final_system_instruction = system_instruction or ""

    try:
        knowledge_contexts = await _load_knowledge_contexts(
            enabled=enable_knowledge,
            query=knowledge_query or prompt,
            limit=knowledge_limit,
        )
        final_system_instruction, request_contexts = (
            _prepare_untrusted_request_contexts(
                system_instruction=system_instruction,
                provided_contexts=untrusted_contexts,
                knowledge_contexts=knowledge_contexts,
            )
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
        async with _client_pool.acquire() as client:
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
                untrusted_contexts=request_contexts,
                **kwargs,
            )
    except Exception as e:
        logger.error(
            "[LLM Provider] Completion 请求失败: trace_id={} phase={} error_type={}",
            request_trace_id or "-",
            request_phase or "-",
            type(e).__name__,
        )
        raise
    else:
        duration_ms = (time.monotonic() - start_time) * 1000
        result.duration_ms = duration_ms
        return result


async def generate_text_with_messages(
    messages: list[dict],
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    *,
    untrusted_contexts: list[UntrustedContext] | None = None,
    **kwargs,  # noqa: ANN003
) -> str:
    """使用 OpenAI 格式 messages 生成文本（支持多模态）。

    Args:
        messages: 消息列表 [{role, content}]，content 可以是字符串或数组（OpenAI Vision 格式）
        model: 模型名称
        temperature: 温度参数
        max_tokens: 最大 token 数
        response_format: OpenAI 兼容结构化输出参数；非空时下发到底层 LLM API
        **kwargs: 其他参数

    Returns:
        生成的文本
    """
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
        async with _client_pool.acquire() as client:
            result = await client.generate_text_with_messages(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                untrusted_contexts=untrusted_contexts,
                **kwargs,
            )
    except Exception as e:
        logger.error(
            "[LLM Provider] Messages 请求失败: trace_id={} model={} error_type={} payload={}",
            request_trace_id or "-",
            model,
            type(e).__name__,
            payload_summary,
        )
        raise
    else:
        return _get_completion_content(result)


async def generate_messages_completion(
    messages: list[dict],
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    parallel_tool_calls: bool | None = None,
    untrusted_contexts: list[UntrustedContext] | None = None,
    **kwargs,  # noqa: ANN003
) -> LLMCompletionResultSchema:
    """使用 messages 生成统一完成结果。"""
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
        async with _client_pool.acquire() as client:
            result = await client.generate_text_with_messages(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                untrusted_contexts=untrusted_contexts,
                **kwargs,
            )
    except Exception as e:
        logger.error(
            "[LLM Provider] Completion(messages) 请求失败: trace_id={} phase={} error_type={}",
            request_trace_id or "-",
            request_phase or "-",
            type(e).__name__,
        )
        raise
    else:
        duration_ms = (time.monotonic() - start_time) * 1000
        result.duration_ms = duration_ms
        return result


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

    async with _client_pool.acquire() as client:
        return await client.test_connection(config.model)


try:
    driver = get_driver()
except ValueError:
    driver = None

if driver is not None:

    @driver.on_startup
    async def _register_sensitive_urls() -> None:
        """将 API Base URL 登记为 Sentry 敏感值。

        base URL 未标记 secret（管理 API 需明文展示），需显式登记，
        由精确值替换层在事件脱敏时字面量替换。
        """
        from komari_bot.common.sentry_support import register_sensitive_value

        config = await config_manager.get_async()
        register_sensitive_value(config.api_base)

    @driver.on_shutdown
    async def _shutdown_provider_resources() -> None:
        """在进程退出时关闭复用的 HTTP 连接。"""
        await _client_pool.close()
