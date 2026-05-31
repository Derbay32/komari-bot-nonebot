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
        "description": "批量暂存用户画像操作，不会直接写入数据库。",
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
                            "category": {"type": "string", "enum": _CATEGORY_ENUM},
                            "importance": {"type": "integer", "minimum": 1, "maximum": 5},
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

PROFILE_AGENT_TOOLS = [READ_PROFILE_TOOL, WRITE_PROFILE_TOOL, PREVIEW_PROFILE_TOOL]
