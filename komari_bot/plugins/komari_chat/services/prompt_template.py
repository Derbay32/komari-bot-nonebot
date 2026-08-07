"""Komari Chat PostgreSQL 提示词模板加载器。"""

from __future__ import annotations

from komari_bot.config.prompt_storage import PromptTemplateLoader
from komari_bot.plugins.komari_chat.prompt_schema import (
    DEFAULTS,
    DISPLAY_NAME,
    RESOURCE_ID,
)

_loader = PromptTemplateLoader(
    resource_id=RESOURCE_ID,
    display_name=DISPLAY_NAME,
    defaults=DEFAULTS,
    log_prefix="[PromptTemplate]",
)


async def get_template() -> dict[str, str]:
    """异步获取最新提示词模板。"""
    return await _loader.get_template_async()
