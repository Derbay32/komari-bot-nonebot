"""TSK-190 历史迁移的测试侧字段契约（固定于 0032）。

字段来自历史表结构，不依赖已退役的运行时 Schema 或提示词正文。
"""

from __future__ import annotations

from typing import Any

HISTORICAL_CHAT_FIELDS = frozenset({
    "system_prompt", "memory_ack", "memory_ack_role", "cot_prefix",
    "cot_prefix_role", "tool_call_instruction", "image_read_instruction",
    "search_web_instruction", "fetch_page_instruction",
    "delegated_vision_instruction", "vision_description_prompt",
})

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
    return set(HISTORICAL_CHAT_FIELDS)


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
