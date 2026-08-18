"""Komari Chat PostgreSQL 提示词模板加载器。

TSK-190/191：聊天 Prompt 无 Python 长文本默认值（见 prompt_schema），
模板值完全来自 PostgreSQL 快照。冷启动门槛是“完整 Prompt”：无行、stored
row 缺 Schema 正文字段、字段仅空白或混入异资源字段且无有效缓存时明确失败
（点名 Prompt 资源与缺失/空/未知字段），绝不静默返回空模板；完整性校验
统一由共享 loader 在缓存前执行（TSK-191 深模块），本模块不再重复 gate。
成功加载后数据库暂短故障仍使用最后有效缓存，恢复后按 revision 刷新。
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
    log_prefix="[PromptTemplate]",
)


async def get_template() -> dict[str, str]:
    """异步获取最新提示词模板；不完整快照/无缓存冷启动失败向上传播。"""
    return await _loader.get_template_async()
