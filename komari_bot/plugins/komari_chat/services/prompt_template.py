"""Komari Chat PostgreSQL 提示词模板加载器。

TSK-190：聊天 Prompt 无 Python 长文本默认值（见 prompt_schema），
加载器以空 defaults 构造；冷启动时若 PostgreSQL 无完整初始值且无
缓存则明确失败，成功加载后数据库暂时故障仍继续使用最后有效缓存。
"""

from __future__ import annotations

from komari_bot.config.prompt_storage import PromptTemplateLoader
from komari_bot.plugins.komari_chat.prompt_schema import (
    DISPLAY_NAME,
    RESOURCE_ID,
)

_loader = PromptTemplateLoader(
    resource_id=RESOURCE_ID,
    display_name=DISPLAY_NAME,
    defaults={},
    log_prefix="[PromptTemplate]",
)


async def get_template() -> dict[str, str]:
    """异步获取最新提示词模板。"""
    return await _loader.get_template_async()
