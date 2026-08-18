"""TSK-190 聊天 Prompt 字段契约 test oracle。

本文件是聊天 Prompt 新字段集的测试侧单一事实来源（对齐
``tests/komari_decision/required_fixed_scene_keys.py`` 的 oracle 惯例）：

- 字段名集合直接派生自强类型 Schema（``KomariChatPromptSchema``），
  不复制生产默认正文；
- ``output_instruction`` 是被删除字段；
- ``tool_call_instruction`` / ``image_read_instruction`` 是 TSK-188
  实现决策 26 明确点名的字段名，按精确名称断言；
- 其余职责（画像读取、联网搜索、网页抓取、委托图片理解、视觉描述）
  只按字段名关键词断言“存在独立行为字段”，不锁定完整命名；
- seed 资产的聊天 Prompt 块定位约定：``_find_chat_prompt_mapping``
  查找包含 ``system_prompt`` 且不含 ``planning_system_prompt`` 的映射
  （后者是 group_history_summary 的判别键；komari_memory_summary 无
  ``system_prompt``），除此之外不锁定 YAML 布局。
"""

from __future__ import annotations

from typing import Any

from komari_bot.plugins.komari_chat.prompt_schema import KomariChatPromptSchema

INTERNAL_STORAGE_FIELDS: frozenset[str] = frozenset({"id", "revision", "updated_at"})

#: 必须从聊天 Prompt 字段集中消失的旧字段（TSK-188 决策 26 / 36）。
REMOVED_FIELD = "output_instruction"

#: TSK-188 决策 26 精确点名的两个新行为字段。
REQUIRED_EXACT_FIELDS: tuple[str, ...] = (
    "tool_call_instruction",
    "image_read_instruction",
)

#: 其余职责 → 字段名必须包含的关键词之一（同一职责可命中多个备选）。
#: 每个职责必须命中至少一个字段，且各职责命中的字段互不相同
#: （"独立行为字段"，TSK-190 验收标准 2）。
BEHAVIOR_RESPONSIBILITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("画像读取", ("profile",)),
    ("联网搜索", ("search",)),
    ("网页抓取", ("fetch",)),
    ("委托图片理解/图片理解引导", ("delegat",)),
    ("视觉描述", ("vision", "description")),
)

#: 0003 旧 Prompt 表保留列（TSK-190 迁移所删除字段之外的既有正文列）。
LEGACY_KEPT_CHAT_COLUMNS: frozenset[str] = frozenset(
    {
        "system_prompt",
        "memory_ack",
        "memory_ack_role",
        "cot_prefix",
        "cot_prefix_role",
    }
)

#: 0003 旧 Prompt 表全部正文列（含 output_instruction，供旧库夹具使用）。
LEGACY_CHAT_COLUMNS: frozenset[str] = LEGACY_KEPT_CHAT_COLUMNS | {
    REMOVED_FIELD,
}

type Mapping = dict[str, Any]


def chat_prompt_field_names() -> set[str]:
    """返回聊天 Prompt Schema 的正文字段名集合（不含存储专用字段）。"""
    return set(KomariChatPromptSchema.model_fields) - INTERNAL_STORAGE_FIELDS


def new_chat_prompt_column_names() -> set[str]:
    """TSK-190 迁移应新增的列 = Schema 字段 - 0003 保留列。"""
    return chat_prompt_field_names() - LEGACY_KEPT_CHAT_COLUMNS


def find_chat_prompt_mapping(raw: object) -> Mapping | None:
    """在 seed 文档中定位聊天 Prompt 块（见模块 docstring 约定）。

    返回包含 ``system_prompt`` 且不含 ``planning_system_prompt`` 的映射；
    找不到返回 ``None``。遍历不修改原文档。
    """

    def walk(node: object) -> Mapping | None:
        if isinstance(node, dict):
            if "system_prompt" in node and "planning_system_prompt" not in node:
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


def resolve_behavior_field_names(field_names: set[str]) -> dict[str, str]:
    """为每个职责挑选一个代表字段；独立字段不足时抛出断言错误。"""
    resolved: dict[str, str] = {}
    taken: set[str] = set()
    for label, keywords in BEHAVIOR_RESPONSIBILITIES:
        candidates = sorted(
            name
            for name in field_names
            if any(keyword in name for keyword in keywords) and name not in taken
        )
        assert candidates, (
            f"缺少 {label} 的独立行为字段（期望字段名包含关键词之一: "
            f"{', '.join(keywords)}）"
        )
        resolved[label] = candidates[0]
        taken.add(candidates[0])
    assert len(resolved) == len(BEHAVIOR_RESPONSIBILITIES), (
        "各职责必须由不同字段承担（独立行为字段）"
    )
    return resolved
