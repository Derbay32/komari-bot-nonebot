"""多模态视觉读图服务。"""

from __future__ import annotations

import asyncio

from nonebot import logger
from nonebot.plugin import require
from openai import AsyncOpenAI

from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema

config_manager_plugin = require("config_manager")
llm_provider_config_manager = config_manager_plugin.get_config_manager(
    "llm_provider",
    DynamicConfigSchema,
)

_IMAGE_READ_PROMPT = (
    "请详细描述这张图片的内容，重点说明画面主体、文字、人物动作、表情、场景、"
    "可能的梗图含义，以及用户可能想表达的意思。请使用简体中文，避免编造看不到的细节。"
)


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
) -> str:
    """调用视觉模型读取单张图片。"""
    config = llm_provider_config_manager.get()
    if not config.deepseek_api_token:
        return "[图片读取失败: 未配置 deepseek_api_token]"

    client = AsyncOpenAI(
        api_key=config.deepseek_api_token,
        base_url=str(config.deepseek_api_base),
        timeout=float(config.deepseek_timeout_seconds),
    )
    try:
        logger.info(
            "[VisionService] 开始读取图片: index={} model={} base64_chars={}",
            image_index,
            vision_model,
            len(image_data_uri),
        )
        response = await client.chat.completions.create(
            model=vision_model,
            temperature=temperature,
            max_tokens=max_tokens,
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
        )
        content = response.choices[0].message.content or ""
        description = content.strip() or "[图片读取失败: 视觉模型返回空内容]"
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
        return f"[图片读取失败: {_format_error(error)}]"
    else:
        return description
    finally:
        await client.close()


async def read_images(
    base64_images: list[str],
    vision_model: str,
    temperature: float = 0.3,
    max_tokens: int = 1024,
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
            )
            for index, image_data_uri in enumerate(base64_images)
        )
    )
