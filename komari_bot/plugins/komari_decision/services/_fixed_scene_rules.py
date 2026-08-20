"""必需 fixed scene 的私有纯领域规则。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

_REQUIRED_FIXED_SCENE_KEYS = (
    "NOISE",
    "MEANINGFUL",
    "CALL_DIRECT",
    "CALL_MENTION",
)


def find_missing_required_fixed_scene_keys(
    scene_keys: Collection[str],
) -> list[str]:
    """返回缺失的必需 fixed scene key，并保持稳定顺序。"""
    return [key for key in _REQUIRED_FIXED_SCENE_KEYS if key not in scene_keys]


def validate_required_fixed_scene_write(
    *,
    scene_key: str,
    scene_type: str,
    enabled: bool,
) -> None:
    """拒绝破坏必需 fixed scene 可用性的写入。"""
    if scene_key not in _REQUIRED_FIXED_SCENE_KEYS:
        return
    if scene_type != "fixed":
        msg = f"必需 fixed scene 不允许改为其他类型: {scene_key}"
        raise ValueError(msg)
    if not enabled:
        msg = f"必需 fixed scene 不允许禁用: {scene_key}"
        raise ValueError(msg)
