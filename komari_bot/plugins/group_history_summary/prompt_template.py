"""群聊历史总结 PostgreSQL 提示词模板加载器。

TSK-191：无 Python 默认正文，模板值完全来自 PostgreSQL 快照；冷启动缺
行/不完整由共享 loader 的完整性门明确失败，读取异常靠最后有效缓存容灾。
"""

from __future__ import annotations

from komari_bot.config.prompt_storage import PromptTemplateLoader
from komari_bot.plugins.group_history_summary.prompt_schema import (
    DISPLAY_NAME,
    RESOURCE_ID,
)

_loader = PromptTemplateLoader(
    resource_id=RESOURCE_ID,
    display_name=DISPLAY_NAME,
    log_prefix="[GroupHistorySummary]",
)


async def get_template() -> dict[str, str]:
    """异步获取最新提示词模板。"""
    return await _loader.get_template_async()
