"""Komari Memory 总结 PostgreSQL 提示词模板加载器。"""

from komari_bot.common.prompt_storage import PromptTemplateLoader
from komari_bot.plugins.komari_memory.prompt_schema import (
    DEFAULTS,
    DISPLAY_NAME,
    RESOURCE_ID,
)


def render_template(template: str, **variables: object) -> str:
    """替换模板中的 {{变量}} 占位符。"""
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


_loader = PromptTemplateLoader(
    resource_id=RESOURCE_ID,
    display_name=DISPLAY_NAME,
    defaults=DEFAULTS,
    log_prefix="[KomariMemory]",
)


async def get_template() -> dict[str, str]:
    """异步获取最新提示词模板。"""
    return await _loader.get_template_async()
