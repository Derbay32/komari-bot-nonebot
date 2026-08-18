"""TSK-191 三个 Prompt 资源字段契约 test oracle。

单一事实来源（对齐 ``tests/config/chat_prompt_field_contract.py`` 的 oracle
惯例）：

- 三个 Prompt 资源（chat / memory summary / group history summary）的正文
  字段集合全部直接派生自强类型 Schema，不复制生产默认正文；TSK-191 起
  三者都不再定义 Python 长文本 ``DEFAULTS``；
- seed 资产中三资源块的定位约定：按各自唯一判别键（discriminator）在文档
  中查找映射，不锁定 YAML 布局：

  - ``komari_chat``：包含 ``system_prompt`` 且不含 ``planning_system_prompt``；
  - ``group_history_summary``：包含 ``planning_system_prompt``；
  - ``komari_memory_summary``：包含 ``memory_summary_common_system``。

- 测试输入值一律使用 marker（``marker-<字段名>``），禁止复制 seed 或生产
  Prompt 正文；管理 API 测试通过 ``make_managed_prompt_resource`` 构造资源
  对象，兼容 `ManagedPromptResource` 移除 ``defaults`` 字段后的构造方式。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from komari_bot.plugins.group_history_summary.prompt_schema import (
    DISPLAY_NAME as GROUP_HISTORY_DISPLAY_NAME,
)
from komari_bot.plugins.group_history_summary.prompt_schema import (
    GroupHistorySummaryPromptSchema,
)
from komari_bot.plugins.komari_chat.prompt_schema import (
    DISPLAY_NAME as KOMARI_CHAT_DISPLAY_NAME,
)
from komari_bot.plugins.komari_chat.prompt_schema import (
    KomariChatPromptSchema,
)
from komari_bot.plugins.komari_memory.prompt_schema import (
    DISPLAY_NAME as MEMORY_SUMMARY_DISPLAY_NAME,
)
from komari_bot.plugins.komari_memory.prompt_schema import (
    KomariMemorySummaryPromptSchema,
)

if TYPE_CHECKING:
    from komari_bot.plugins.komari_management.managed_resources import (
        ManagedPromptResource,
    )

#: 三个 Prompt 资源的稳定顺序（与生产管理资源注册顺序一致）。
PROMPT_RESOURCE_IDS: tuple[str, str, str] = (
    "komari_chat",
    "komari_memory_summary",
    "group_history_summary",
)

INTERNAL_STORAGE_FIELDS: frozenset[str] = frozenset(
    {"id", "revision", "updated_at"}
)

_PROMPT_SCHEMAS: dict[str, type[Any]] = {
    "komari_chat": KomariChatPromptSchema,
    "komari_memory_summary": KomariMemorySummaryPromptSchema,
    "group_history_summary": GroupHistorySummaryPromptSchema,
}

_PROMPT_DISPLAY_NAMES: dict[str, str] = {
    "komari_chat": KOMARI_CHAT_DISPLAY_NAME,
    "komari_memory_summary": MEMORY_SUMMARY_DISPLAY_NAME,
    "group_history_summary": GROUP_HISTORY_DISPLAY_NAME,
}


def prompt_resource_field_names(resource_id: str) -> set[str]:
    """返回 Prompt 资源强类型 Schema 的正文字段名集合（不含存储专用字段）。"""
    if resource_id not in _PROMPT_SCHEMAS:
        raise KeyError(f"未知 Prompt 资源: {resource_id}")  # noqa: TRY003
    return set(_PROMPT_SCHEMAS[resource_id].model_fields) - INTERNAL_STORAGE_FIELDS


def prompt_table_name(resource_id: str) -> str:
    """返回 Prompt 资源对应的强类型表名。"""
    if resource_id not in _PROMPT_SCHEMAS:
        raise KeyError(f"未知 Prompt 资源: {resource_id}")  # noqa: TRY003
    return str(_PROMPT_SCHEMAS[resource_id].__tablename__)


def prompt_display_name(resource_id: str) -> str:
    """返回 Prompt 资源的展示名。"""
    if resource_id not in _PROMPT_DISPLAY_NAMES:
        raise KeyError(f"未知 Prompt 资源: {resource_id}")  # noqa: TRY003
    return _PROMPT_DISPLAY_NAMES[resource_id]


def prompt_marker_values(resource_id: str) -> dict[str, str]:
    """生成测试自有的完整 Prompt 字段 marker 值（不复制 seed/生产正文）。"""
    if resource_id not in _PROMPT_SCHEMAS:
        raise KeyError(f"未知 Prompt 资源: {resource_id}")  # noqa: TRY003
    return {
        field: f"marker-{field}"
        for field in sorted(prompt_resource_field_names(resource_id))
    }


def find_prompt_mapping(raw: object, resource_id: str) -> dict[str, Any] | None:
    """在 seed 文档中定位指定 Prompt 资源的初始数据块（见模块 docstring）。

    按唯一判别键查找映射；遍历不修改原文档，找不到返回 ``None``。
    """
    predicates: dict[str, Any] = {
        "komari_chat": (
            lambda node: "system_prompt" in node
            and "planning_system_prompt" not in node
        ),
        "group_history_summary": (
            lambda node: "planning_system_prompt" in node
        ),
        "komari_memory_summary": (
            lambda node: "memory_summary_common_system" in node
        ),
    }
    if resource_id not in predicates:
        raise KeyError(f"未知 Prompt 资源: {resource_id}")  # noqa: TRY003
    predicate = predicates[resource_id]

    def walk(node: object) -> dict[str, Any] | None:
        if isinstance(node, dict):
            if predicate(node):
                return node
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = walk(value)
                if found is not None:
                    return found
        return None

    return walk(raw)


def make_managed_prompt_resource(
    resource_id: str,
    display_name: str,
    defaults: dict[str, str] | None = None,
) -> ManagedPromptResource:
    """构造管理 Prompt 资源，兼容 defaults 字段存在与否两种实现形态。

    TSK-191 后管理资源不再携带 Python 默认正文；若实现保留 ``defaults``
    字段（如 Schema 字段占位符），测试照常透传，否则省略该参数。
    """
    from komari_bot.plugins.komari_management.managed_resources import (
        ManagedPromptResource,
    )

    kwargs: dict[str, Any] = {
        "resource_id": resource_id,
        "display_name": display_name,
    }
    if "defaults" in ManagedPromptResource.__dataclass_fields__:
        kwargs["defaults"] = {} if defaults is None else defaults
    return ManagedPromptResource(**kwargs)
