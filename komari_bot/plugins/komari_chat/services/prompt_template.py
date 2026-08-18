"""Komari Chat PostgreSQL 提示词模板加载器。

TSK-190：聊天 Prompt 无 Python 长文本默认值（见 prompt_schema），
加载器以空 defaults 构造；冷启动门槛是“完整 Prompt”——PostgreSQL 无
完整初始值、stored row 缺 Schema 正文字段或字段仅空白且无有效缓存时
明确失败（点名 Prompt 资源与缺失/空字段），绝不静默返回空模板；成功
加载后数据库暂时故障仍继续使用最后有效缓存。
"""

from __future__ import annotations

from komari_bot.config.prompt_storage import PromptTemplateLoader
from komari_bot.plugins.komari_chat.prompt_schema import (
    DISPLAY_NAME,
    RESOURCE_ID,
    KomariChatPromptSchema,
)

#: Prompt 强类型表继承自 TypedConfigModel 的存储专用字段，不属于正文。
_PROMPT_STORAGE_FIELDS = frozenset({"id", "revision", "updated_at"})

#: 聊天 Prompt 正文字段集，真源 = 强类型 Schema（TSK-190，不复制列表）。
_PROMPT_FIELDS = tuple(
    sorted(set(KomariChatPromptSchema.model_fields) - _PROMPT_STORAGE_FIELDS)
)


def _raise_if_incomplete(template: dict[str, str]) -> None:
    """冷启动完整性 gate：缺 Schema 字段或字段空白时点名资源与字段并抛错。

    完整快照正常返回；不完整快照（含无缓存冷启动、stored row 缺字段或
    仅空白）一律视为不可冷启动，绝不回退为空 Prompt 继续运行。
    """
    missing = [field for field in _PROMPT_FIELDS if field not in template]
    blank = [
        field
        for field in _PROMPT_FIELDS
        if field in template and not str(template[field] or "").strip()
    ]
    if not missing and not blank:
        return
    problems: list[str] = []
    if missing:
        problems.append(f"缺失字段: {', '.join(missing)}")
    if blank:
        problems.append(f"空白字段: {', '.join(blank)}")
    msg = (
        f"[PromptTemplate] Prompt 的 PostgreSQL 快照不是完整 Prompt，"
        f"无法冷启动: {DISPLAY_NAME}（{'；'.join(problems)}）"
    )
    raise RuntimeError(msg)


_loader = PromptTemplateLoader(
    resource_id=RESOURCE_ID,
    display_name=DISPLAY_NAME,
    defaults={},
    log_prefix="[PromptTemplate]",
)


async def get_template() -> dict[str, str]:
    """异步获取最新提示词模板；不完整快照/无缓存冷启动失败向上传播。

    PostgreSQL 暂时故障（读取异常）时仍沿用最后有效缓存语义；但 stored
    row 缺 Schema 字段或字段空白属于不完整快照，必须抛错而非继续使用。
    """
    template = await _loader.get_template_async()
    try:
        _raise_if_incomplete(template)
    except RuntimeError:
        # 不完整快照不得留在新鲜度缓存内：下次调用必须重新读取并再次
        # 通过完整性 gate，避免 1 秒内静默放行空 Prompt。
        _loader._invalidate()
        raise
    return template
