"""多模态视觉读图服务。

TSK-190：视觉描述 Prompt 经 chat Prompt 公开 loader
（``services/prompt_template.get_template``）从 PostgreSQL 快照读取，
不再保留任何 Python 长文本常量；loader 冷启动失败或快照视觉描述字段
缺失/空白时异常向上传播，绝不降级为空文本继续调用 LLM。
"""

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

from .prompt_template import get_template

llm_provider_config_manager = config_manager_plugin.get_config_manager(
    "llm_provider",
    DynamicConfigSchema,
)

_VISION_READ_CONCURRENCY_LIMIT = 2
_VISION_READ_SEMAPHORE = asyncio.Semaphore(_VISION_READ_CONCURRENCY_LIMIT)


async def _load_vision_description_prompt() -> str:
    """经 chat Prompt 公开 loader 读取视觉描述 Prompt（TSK-190 AC5）。

    loader 在 PostgreSQL 无完整初始值且无缓存时明确抛错，此处不吞异常：
    非空图片输入时冷启动失败必须向上传播，调用方不会继续视觉 LLM 调用。
    快照成功返回但视觉描述字段缺失/空白同样明确失败，绝不降级为空文本。
    """
    template = await get_template()
    prompt = template.get("vision_description_prompt")
    if prompt is None or not str(prompt).strip():
        msg = (
            "Prompt 的视觉描述字段（vision_description_prompt）缺失或空白，"
            "无法读取图片: komari_chat"
        )
        raise RuntimeError(msg)
    return str(prompt)


def _format_error(error: Exception) -> str:
    """归一化读图失败信息（TSK-195 第二轮安全验收反馈）。

    只返回异常类型名作为稳定错误码；该值会回流主模型工具结果与诊断
    收集器，绝不携带 provider 原始异常消息、URL 或 data URI。
    """
    return type(error).__name__


async def _read_single_image(
    *,
    image_data_uri: str,
    image_index: int,
    vision_model: str,
    vision_description_prompt: str,
    temperature: float,
    max_tokens: int,
    request_api: str = "chat_completions",
    stream_enabled: bool = False,
    thinking_mode: bool = False,
    reasoning_effort: str = "",
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
                {"type": "text", "text": vision_description_prompt},
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
        # TSK-194 / ADR-0010：视觉槽位推理参数在任务起点随 llm_provider
        # 配置快照冻结，由 read_image 子调用携带（主循环恒用 chat 槽位）。
        "thinking_mode": thinking_mode,
        "reasoning_effort": reasoning_effort,
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
        error_type = _format_error(error)
        failure_text = f"[图片读取失败: {error_type}]"
        # 普通日志只记录 index/model 与归一化异常类型：不捕获 traceback
        # （栈帧局部变量 image_data_uri/request_data 含 base64），不记录
        # str(error)（provider 异常正文可能内嵌 URL/data URI，TSK-195 第
        # 二轮安全验收反馈）。
        logger.warning(
            "[VisionService] 图片读取失败: index={} model={} error_type={}",
            image_index,
            vision_model,
            error_type,
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
                message=failure_text,
            )
            collector.add_error(
                phase="vision_read_image",
                error_type=error_type,
                message=failure_text,
            )
        return failure_text
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
    thinking_mode: bool = False,
    reasoning_effort: str = "",
    request_trace_id: str | None = None,
    parent_call_id: str | None = None,
    collector: "LLMDiagnosticCollector | None" = None,
) -> list[str]:
    """调用多模态 AI 读取图片，返回图片描述列表。"""
    if not base64_images:
        return []

    vision_description_prompt = await _load_vision_description_prompt()

    return await asyncio.gather(
        *(
            _read_single_image(
                image_data_uri=image_data_uri,
                image_index=index,
                vision_model=vision_model,
                vision_description_prompt=vision_description_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                request_api=request_api,
                stream_enabled=stream_enabled,
                thinking_mode=thinking_mode,
                reasoning_effort=reasoning_effort,
                request_trace_id=request_trace_id,
                parent_call_id=parent_call_id,
                collector=collector,
            )
            for index, image_data_uri in enumerate(base64_images)
        )
    )
