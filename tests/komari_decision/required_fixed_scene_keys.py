"""test-owned required-fixed scene keys oracle（独立字面量真源）。

TSK-183 起，komari_decision 测试目录中的行为测试与 AST 契约测试统一引用
本模块，禁止从生产私有 `_fixed_scene_rules` 导入 required-fixed 常量。
本 oracle 是独立维护的字面量，用来对生产定义保持辨识力。
"""

from __future__ import annotations

REQUIRED_FIXED_SCENE_KEYS: tuple[str, ...] = (
    "NOISE",
    "MEANINGFUL",
    "CALL_DIRECT",
    "CALL_MENTION",
)
