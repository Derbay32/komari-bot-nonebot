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
  Prompt 正文；管理资源经 ``make_managed_prompt_resource(resource_id,
  display_name)`` 构造，这是 TSK-191 后的唯一形态：资源不再携带
  ``defaults``（AC8），测试绝不传/透传 Python 默认正文；
- ``CROSS_RESOURCE_FOREIGN_FIELDS`` 提供跨资源拒绝用例的注入字段：每个
  资源取一个属于其他资源、不属于本资源 Schema 的字段，用于证明白名单与
  完整性校验是 resource_id 对应 Schema，不是三资源字段的全局 union。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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

#: 跨资源拒绝用例的注入字段：key 是目标资源，value 是注入的异资源字段。
#: 每个 value 都属于其他某个资源的 Schema、但不属于目标资源 Schema
#: （chat←group 的 planning_system_prompt；memory←chat/group 的
#: system_prompt；group←chat 的 tool_call_instruction），覆盖三资源互证。
CROSS_RESOURCE_FOREIGN_FIELDS: dict[str, str] = {
    "komari_chat": "planning_system_prompt",
    "komari_memory_summary": "system_prompt",
    "group_history_summary": "tool_call_instruction",
}

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
) -> ManagedPromptResource:
    """构造无 defaults 的管理 Prompt 资源（TSK-191 契约，唯一形态）。

    绝不接受/透传 Python 默认正文：AC8 要求旧默认机制与兼容分支物理删除。
    旧实现仍把 ``defaults`` 当作必填字段，这里用 ``cast(Any, ...)`` 让
    测试只按新契约构造；实现移除字段后即为完全合法的直接构造。
    """
    from komari_bot.plugins.komari_management.managed_resources import (
        ManagedPromptResource,
    )

    constructor = cast("Any", ManagedPromptResource)
    return constructor(resource_id=resource_id, display_name=display_name)
