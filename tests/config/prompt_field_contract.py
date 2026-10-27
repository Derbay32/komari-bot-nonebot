"""Test-owned markers for the sole surviving PostgreSQL prompt resource (Jev)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from komari_bot.plugins.komari_decision.prompt_schema import (
    DISPLAY_NAME as DECISION_DISPLAY_NAME,
)
from komari_bot.plugins.komari_decision.prompt_schema import (
    KomariDecisionPromptSchema,
)

if TYPE_CHECKING:
    from komari_bot.plugins.komari_management.managed_resources import (
        ManagedPromptResource,
    )

PROMPT_RESOURCE_IDS: tuple[str, ...] = ("komari_decision",)
# A formerly valid chat field must not be accepted by the surviving resource.
CROSS_RESOURCE_FOREIGN_FIELDS = {"komari_decision": "system_prompt"}
INTERNAL_STORAGE_FIELDS = frozenset({"id", "revision", "updated_at"})
_PROMPT_SCHEMAS: dict[str, type[Any]] = {"komari_decision": KomariDecisionPromptSchema}
_PROMPT_DISPLAY_NAMES = {"komari_decision": DECISION_DISPLAY_NAME}


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
        "komari_decision": lambda node: "role_description" in node,
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
