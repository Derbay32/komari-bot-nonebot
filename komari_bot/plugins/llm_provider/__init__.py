"""LLM Provider 插件 - 提供统一的 LLM 调用接口（OpenAI 兼容格式）。"""

import time
from typing import Any

from nonebot import logger
from nonebot.plugin import PluginMetadata, require

from .api import register_llm_provider_api
from .config import Config
from .config_schema import DynamicConfigSchema
from .deepseek_client import DeepSeekClient
from .llm_logger import log_llm_call
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


def _get_client() -> DeepSeekClient:
    """获取 LLM 客户端实例。"""
    config = config_manager.get()
    token = config.deepseek_api_token
    if not token:
        raise ValueError("API Token 未配置，请在配置中设置 deepseek_api_token")  # noqa: TRY003
    return DeepSeekClient(
        token,
        timeout_seconds=float(config.deepseek_timeout_seconds),
    )


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
        response_format: 为兼容旧调用保留；当前不会下发到模型，请通过 prompt 指定输出格式
        record_chat_log: 是否记录聊天回复日志
        **kwargs: 其他参数

    Returns:
        生成的文本
    """
    client = _get_client()
    start_time = time.monotonic()
    request_trace_id = str(kwargs.get("request_trace_id", "")).strip()
    request_phase = str(kwargs.get("request_phase", "")).strip()

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
                input_data={
                    "trace_id": request_trace_id,
                    "phase": request_phase,
                    "prompt": prompt,
                    "system_instruction": system_instruction,
                },
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
        if record_chat_log:
            await log_llm_call(
                method="generate_text",
                model=model,
                input_data={
                    "trace_id": request_trace_id,
                    "phase": request_phase,
                    "prompt": prompt,
                    "system_instruction": final_system_instruction,
                },
                output=result,
                duration_ms=duration_ms,
            )
        return result
    finally:
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
        response_format: 为兼容旧调用保留；当前不会下发到模型，请通过 prompt 指定输出格式
        record_chat_log: 是否记录聊天回复日志
        **kwargs: 其他参数

    Returns:
        生成的文本
    """
    client = _get_client()
    start_time = time.monotonic()
    request_trace_id = str(kwargs.get("request_trace_id", "")).strip()
    payload_summary = _summarize_messages_payload(messages)

    try:
        logger.info(
            "[LLM Provider] Messages 请求追踪: trace_id={} model={} turns={} text_parts={} text_chars={} image_parts={} image_url_chars={}",
            request_trace_id or "-",
            model,
            payload_summary["turns"],
            payload_summary["text_parts"],
            payload_summary["text_chars"],
            payload_summary["image_parts"],
            payload_summary["image_url_chars"],
        )
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
                input_data={
                    "trace_id": request_trace_id,
                    "payload_summary": payload_summary,
                    "messages": messages,
                },
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
        if record_chat_log:
            await log_llm_call(
                method="generate_text_with_messages",
                model=model,
                input_data={
                    "trace_id": request_trace_id,
                    "payload_summary": payload_summary,
                    "messages": messages,
                },
                output=result,
                duration_ms=duration_ms,
            )
        return result
    finally:
        await client.close()


async def test_connection() -> bool:
    """测试 API 连接。

    Returns:
        连接是否成功
    """
    config = config_manager.get()
    token = config.deepseek_api_token
    if not token:
        logger.warning("API Token 未配置，跳过连接测试")
        return False

    client = DeepSeekClient(
        token,
        timeout_seconds=float(config.deepseek_timeout_seconds),
    )
    try:
        return await client.test_connection()
    finally:
        await client.close()
