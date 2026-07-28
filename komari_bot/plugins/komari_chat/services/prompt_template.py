"""Komari Chat PostgreSQL 提示词模板加载器。"""

from __future__ import annotations

from komari_bot.common.prompt_storage import PromptTemplateLoader

# 默认模板值（PG 配置缺失或读取失败时使用）
_DEFAULTS: dict[str, str] = {
    "system_prompt": "你是一个友善的助手。",
    "memory_ack": "好的，我了解了。",
    "memory_ack_role": "assistant",
    "output_instruction": "请将最终回复放在 <content></content> 标签中。",
    "cot_prefix": "<think>\n开始思考。\n",
    "cot_prefix_role": "assistant",
}

_RESOURCE_ID = "komari_chat"

_loader = PromptTemplateLoader(
    resource_id=_RESOURCE_ID,
    display_name="Komari Chat Prompt",
    defaults=_DEFAULTS,
    log_prefix="[PromptTemplate]",
)


async def get_template() -> dict[str, str]:
    """异步获取最新提示词模板。"""
    return await _loader.get_template_async()
