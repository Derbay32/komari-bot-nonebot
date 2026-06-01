"""画像 Agent 暴露给 LLM 的工具定义。"""

from __future__ import annotations

from typing import Any

_CATEGORY_ENUM = ["preference", "fact", "relation", "general"]

READ_PROFILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_profile",
        "description": "读取指定用户在当前群的已有画像，可选择叠加当前暂存区。",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选，只读取指定画像 key",
                },
                "include_staged": {
                    "type": "boolean",
                    "description": "是否返回叠加暂存区后的有效画像",
                },
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
    },
}

WRITE_PROFILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "write_profile",
        "description": (
            "批量暂存用户画像操作，不会直接写入数据库。"
            "画像仅记录用户自身的长期特征，严禁写入 bot（小鞠知花）与用户之间的互动关系（如「经常找小鞠帮忙」）。"
            "类别说明：preference=长期偏好/兴趣，fact=固定事实/身份，"
            "relation=仅限用户之间关系（如朋友/同学），general=其他稳定特征（如语气/习惯）。"
            "relation 类别严禁涉及与 bot 的任何关系。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["add", "set", "delete"]},
                            "user_id": {"type": "string"},
                            "key": {"type": "string"},
                            "value": {"type": "string"},
                            "category": {
                                "type": "string",
                                "enum": _CATEGORY_ENUM,
                                "description": (
                                    "preference=长期偏好/兴趣，fact=固定事实/身份，"
                                    "relation=仅限用户之间关系，general=其他稳定特征。"
                                    "严禁在 relation 中写入任何与 bot 的关系。"
                                ),
                            },
                            "importance": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=可有可无, 3=一般, 5=核心特征"},
                        },
                        "required": ["op", "user_id", "key"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
    },
}

PREVIEW_PROFILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "preview_profile",
        "description": "查看当前会话暂存区中所有待提交画像 diff。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

COUNT_PROFILE_TRAITS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "count_profile_traits",
        "description": "统计指定用户在当前群的画像 trait 数量，可选择纳入暂存区未提交的修改。用于判断是否需要压缩画像。",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "用户 ID"},
                "include_staged": {
                    "type": "boolean",
                    "description": "是否纳入暂存区未提交的操作来计算有效 trait 数。设为 true 可以判断提交后是否会超出上限。",
                },
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
    },
}

COMMIT_PROFILE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "commit_profile",
        "description": "提交当前暂存区画像修改。提交前会校验所有受影响用户的 trait 数量，超限时返回错误并保留暂存区，供继续压缩后重试。",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

PROFILE_AGENT_TOOLS = [
    READ_PROFILE_TOOL,
    WRITE_PROFILE_TOOL,
    PREVIEW_PROFILE_TOOL,
    COUNT_PROFILE_TRAITS_TOOL,
    COMMIT_PROFILE_TOOL,
]
