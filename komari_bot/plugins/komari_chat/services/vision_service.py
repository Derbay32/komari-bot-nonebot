"""多模态视觉读图服务。"""

from __future__ import annotations

import asyncio
from typing import cast

from nonebot import logger
from nonebot.plugin import require

from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema

if __import__("typing", fromlist=["TYPE_CHECKING"]).TYPE_CHECKING:
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

require("config_manager")
require("llm_provider")

from komari_bot.plugins import config_manager as config_manager_plugin
from komari_bot.plugins import llm_provider

llm_provider_config_manager = config_manager_plugin.get_config_manager(
    "llm_provider",
    DynamicConfigSchema,
)

_IMAGE_READ_PROMPT = (
    "请详细描述这张图片的内容，重点说明画面主体、文字、人物动作、表情、场景、"
    "可能的梗图含义，以及用户可能想表达的意思。请使用简体中文，避免编造看不到的细节。"
)
_VISION_READ_CONCURRENCY_LIMIT = 2
_VISION_READ_SEMAPHORE = asyncio.Semaphore(_VISION_READ_CONCURRENCY_LIMIT)


def _format_error(error: Exception) -> str:
    """格式化读图失败信息，避免把过长异常塞回主模型。"""
    message = str(error).strip() or error.__class__.__name__
    if len(message) > 200:
        message = f"{message[:200]}..."
    return message


async def _read_single_image(
    *,
    image_data_uri: str,
    image_index: int,
    vision_model: str,
    temperature: float,
    max_tokens: int,
    request_api: str = "chat_completions",
    stream_enabled: bool = False,
    request_trace_id: str | None = None,
    parent_call_id: str | None = None,
    collector: "LLMDiagnosticCollector | None" = None,
) -> str:
    """调用视觉模型读取单张图片。"""
    config = cast("DynamicConfigSchema", llm_provider_config_manager.get())
    if not config.api_token:
        return "[图片读取失败: 未配置 api_token]"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _IMAGE_READ_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_uri},
                },
            ],
        }
    ]
    request_data = {
        "messages": messages,
        "model": vision_model,
        "temperature": temperature,
        "max_tokens": int(max_tokens),
        "request_api": request_api,
        "stream_enabled": stream_enabled,
    }
    try:
        logger.info(
            "[VisionService] 开始读取图片: index={} model={} base64_chars={}",
            image_index,
            vision_model,
            len(image_data_uri),
        )
        async with _VISION_READ_SEMAPHORE:
            completion = await llm_provider.generate_messages_completion(
                **request_data,
                request_trace_id=request_trace_id or "",
                request_phase="vision_read_image",
            )
        content = completion.content or ""
        description = content.strip() or "[图片读取失败: 视觉模型返回空内容]"

        if collector is not None:
            from komari_bot.plugins.agent_run_logger.diagnostic import (
                record_completion_call,
            )

            record_completion_call(
                collector,
                parent_call_id=parent_call_id,
                phase="vision_read_image",
                round_index=image_index,
                method="generate_messages_completion",
                model=vision_model,
                request=request_data,
                completion=completion,
            )

        logger.info(
            "[VisionService] 图片读取完成: index={} model={} description_chars={}",
            image_index,
            vision_model,
            len(description),
        )
    except Exception as error:
        logger.warning(
            "[VisionService] 图片读取失败: index={} model={} error={}",
            image_index,
            vision_model,
            error,
            exc_info=True,
        )
        if collector is not None:
            from komari_bot.plugins.agent_run_logger.diagnostic import (
                record_failed_call,
            )

            record_failed_call(
                collector,
                phase="vision_read_image",
                round_index=image_index,
                method="generate_messages_completion",
                model=vision_model,
                request=request_data,
                error=error,
                parent_call_id=parent_call_id,
            )
            collector.add_error(
                phase="vision_read_image",
                error_type=type(error).__name__,
                message=_format_error(error),
            )
        return f"[图片读取失败: {_format_error(error)}]"
    else:
        return description


async def read_images(
    base64_images: list[str],
    vision_model: str,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    *,
    request_api: str = "chat_completions",
    stream_enabled: bool = False,
    request_trace_id: str | None = None,
    parent_call_id: str | None = None,
    collector: "LLMDiagnosticCollector | None" = None,
) -> list[str]:
    """调用多模态 AI 读取图片，返回图片描述列表。"""
    if not base64_images:
        return []

    return await asyncio.gather(
        *(
            _read_single_image(
                image_data_uri=image_data_uri,
                image_index=index,
                vision_model=vision_model,
                temperature=temperature,
                max_tokens=max_tokens,
                request_api=request_api,
                stream_enabled=stream_enabled,
                request_trace_id=request_trace_id,
                parent_call_id=parent_call_id,
                collector=collector,
            )
            for index, image_data_uri in enumerate(base64_images)
        )
    )
