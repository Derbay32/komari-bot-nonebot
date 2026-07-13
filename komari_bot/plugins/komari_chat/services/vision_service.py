"""多模态视觉读图服务。"""

from __future__ import annotations

import asyncio

from nonebot import logger
from nonebot.plugin import require

from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema

if __import__("typing", fromlist=["TYPE_CHECKING"]).TYPE_CHECKING:
    from komari_bot.plugins.llm_provider.diagnostic import LLMDiagnosticCollector

config_manager_plugin = require("config_manager")
llm_provider = require("llm_provider")
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
    request_trace_id: str | None = None,
    parent_call_id: str | None = None,
    collector: "LLMDiagnosticCollector | None" = None,
) -> str:
    """调用视觉模型读取单张图片。"""
    config = llm_provider_config_manager.get()
    if not config.api_token:
        return "[图片读取失败: 未配置 api_token]"

    try:
        logger.info(
            "[VisionService] 开始读取图片: index={} model={} base64_chars={}",
            image_index,
            vision_model,
            len(image_data_uri),
        )
        async with _VISION_READ_SEMAPHORE:
            completion = await llm_provider.generate_messages_completion(
                messages=[
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
                ],
                model=vision_model,
                temperature=temperature,
                max_tokens=int(max_tokens),
                request_trace_id=request_trace_id or "",
                request_phase="vision_read_image",
            )
        content = completion.content or ""
        description = content.strip() or "[图片读取失败: 视觉模型返回空内容]"

        if collector is not None:
            from komari_bot.plugins.llm_provider.diagnostic import LLMCallTrace

            vision_call = LLMCallTrace(
                parent_call_id=parent_call_id,
                phase="vision_read_image",
                round_index=image_index,
                model=vision_model,
                finish_reason=completion.finish_reason,
                duration_ms=completion.duration_ms,
                usage=completion.usage,
            )
            collector.add_call(vision_call)

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
                request_trace_id=request_trace_id,
                parent_call_id=parent_call_id,
                collector=collector,
            )
            for index, image_data_uri in enumerate(base64_images)
        )
    )
