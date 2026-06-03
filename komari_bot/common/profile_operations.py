"""用户画像操作的纯函数模型与合并逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

ProfileOperationOp = Literal["add", "set", "delete"]
ProfileTraitCategory = Literal["preference", "fact", "relation", "general"]

_ALLOWED_CATEGORIES: set[str] = {"preference", "fact", "relation", "general"}


@dataclass(frozen=True)
class ProfileOperation:
    """LLM 暂存工具接收的单条画像操作。"""

    op: ProfileOperationOp
    user_id: str
    key: str
    value: str | None = None
    category: ProfileTraitCategory | None = None
    importance: int | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> ProfileOperation | None:
        """从工具参数解析画像操作，非法数据返回 None。"""
        op = str(raw.get("op", "")).strip().lower()
        if op == "replace":
            op = "set"
        if op not in {"add", "set", "delete"}:
            return None

        user_id = str(raw.get("user_id", "")).strip()
        key = str(raw.get("key", "")).strip()
        if not user_id or not key:
            return None

        if op == "delete":
            return cls(op="delete", user_id=user_id, key=key)

        value = str(raw.get("value", "")).strip()
        if not value:
            return None
        category = str(raw.get("category", "general")).strip() or "general"
        if category not in _ALLOWED_CATEGORIES:
            category = "general"
        try:
            importance = int(raw.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3

        return cls(
            op=op,  # type: ignore[arg-type]
            user_id=user_id,
            key=key,
            value=value,
            category=category,  # type: ignore[arg-type]
            importance=max(1, min(5, importance)),
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为普通 dict。"""
        data: dict[str, Any] = {
            "op": self.op,
            "user_id": self.user_id,
            "key": self.key,
        }
        if self.value is not None:
            data["value"] = self.value
        if self.category is not None:
            data["category"] = self.category
        if self.importance is not None:
            data["importance"] = self.importance
        return data


@dataclass(frozen=True)
class ProfileDiffItem:
    """画像暂存区中的最终意图。"""

    op: ProfileOperationOp
    user_id: str
    key: str
    value: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    category: ProfileTraitCategory | None = None
    importance: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "op": self.op,
            "user_id": self.user_id,
            "key": self.key,
        }
        for field_name in ("value", "old_value", "new_value", "category", "importance"):
            value = getattr(self, field_name)
            if value is not None:
                data[field_name] = value
        return data

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> ProfileDiffItem | None:
        op = str(raw.get("op", "")).strip().lower()
        if op not in {"add", "set", "delete"}:
            return None
        user_id = str(raw.get("user_id", "")).strip()
        key = str(raw.get("key", "")).strip()
        if not user_id or not key:
            return None
        importance_raw = raw.get("importance")
        importance = None
        if importance_raw is not None:
            try:
                importance = max(1, min(5, int(importance_raw)))
            except (TypeError, ValueError):
                importance = 3
        category = str(raw.get("category", "")).strip() or None
        if category is not None and category not in _ALLOWED_CATEGORIES:
            category = "general"
        return cls(
            op=op,  # type: ignore[arg-type]
            user_id=user_id,
            key=key,
            value=str(raw["value"]).strip() if raw.get("value") is not None else None,
            old_value=(
                str(raw["old_value"]).strip()
                if raw.get("old_value") is not None
                else None
            ),
            new_value=(
                str(raw["new_value"]).strip()
                if raw.get("new_value") is not None
                else None
            ),
            category=category,  # type: ignore[arg-type]
            importance=importance,
        )


@dataclass(frozen=True)
class ProfileConflict:
    """画像暂存冲突。"""

    op: str
    user_id: str
    key: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "op": self.op,
            "user_id": self.user_id,
            "key": self.key,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class StageResult:
    status: str
    staged_count: int
    diff: list[ProfileDiffItem] = field(default_factory=list)
    conflicts: list[ProfileConflict] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "staged_count": self.staged_count,
            "diff": [item.to_dict() for item in self.diff],
            "summary": self.summary,
        }
        if self.conflicts:
            data["conflicts"] = [conflict.to_dict() for conflict in self.conflicts]
        return data


@dataclass(frozen=True)
class PreviewResult:
    staged_count: int
    diff: list[ProfileDiffItem]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "staged_count": self.staged_count,
            "diff": [item.to_dict() for item in self.diff],
            "summary": self.summary,
        }


@dataclass(frozen=True)
class CommitResult:
    status: str
    committed_count: int
    changed_user_ids: set[str] = field(default_factory=set)
    summary: str = ""
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "status": self.status,
            "committed_count": self.committed_count,
            "changed_user_ids": sorted(self.changed_user_ids),
            "summary": self.summary,
        }
        if self.conflicts:
            data["conflicts"] = self.conflicts
        return data


def normalize_profile_operations(
    operations: list[ProfileOperation],
    *,
    existing_traits_by_user: dict[str, dict[str, Any]],
    staged_items: list[ProfileDiffItem] | None = None,
) -> list[ProfileDiffItem]:
    """把多轮画像操作合并为每个 user_id/key 的最终意图。"""
    merged: dict[tuple[str, str], ProfileDiffItem] = {}
    for item in staged_items or []:
        merged[(item.user_id, item.key)] = item

    for operation in operations:
        identity = (operation.user_id, operation.key)
        current = merged.get(identity)
        old_trait = existing_traits_by_user.get(operation.user_id, {}).get(operation.key)
        old_value = _trait_value(old_trait)

        if current is None:
            item = _operation_to_diff(operation, old_value=old_value)
            if item is not None:
                merged[identity] = item
            continue

        if current.op == "add":
            if operation.op == "set":
                if (item := _operation_to_diff(operation, old_value=None, force_op="add")) is not None:
                    merged[identity] = item
            elif operation.op == "delete":
                merged.pop(identity, None)
            elif (item := _operation_to_diff(operation, old_value=None, force_op="add")) is not None:
                merged[identity] = item
            continue

        if current.op == "set":
            if operation.op == "delete":
                merged[identity] = ProfileDiffItem(
                    op="delete",
                    user_id=operation.user_id,
                    key=operation.key,
                    old_value=old_value,
                )
            elif (item := _operation_to_diff(operation, old_value=old_value, force_op="set")) is not None:
                merged[identity] = item
            continue

        if current.op == "delete":
            if operation.op == "delete":
                continue
            force_op: ProfileOperationOp = "set" if old_value is not None else "add"
            if (item := _operation_to_diff(operation, old_value=old_value, force_op=force_op)) is not None:
                merged[identity] = item

    return sorted(merged.values(), key=lambda item: (item.user_id, item.key))


def apply_profile_operations(
    base_profile: dict[str, Any] | None,
    operations: Sequence[ProfileDiffItem | ProfileOperation],
    *,
    user_id: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    """把最终画像 diff 应用到画像 JSON。"""
    profile = dict(base_profile or {})
    profile["version"] = 1
    profile["user_id"] = user_id
    resolved_display_name = (
        str(display_name or "").strip()
        or str(profile.get("display_name", "")).strip()
        or user_id
    )
    profile["display_name"] = resolved_display_name

    traits_raw = profile.get("traits")
    traits = dict(traits_raw) if isinstance(traits_raw, dict) else {}
    for operation in operations:
        if operation.user_id != user_id:
            continue
        if operation.op == "delete":
            traits.pop(operation.key, None)
            continue

        value = operation.value if isinstance(operation, ProfileOperation) else operation.value or operation.new_value
        if not value:
            continue
        category = operation.category or "general"
        importance = operation.importance or 3
        traits[operation.key] = {
            "value": value,
            "category": category,
            "importance": max(1, min(5, int(importance))),
            "updated_at": datetime.now(UTC).isoformat(),
        }

    profile["traits"] = traits
    profile["updated_at"] = datetime.now(UTC).isoformat()
    return profile


def profile_traits_to_list(traits: object) -> list[dict[str, Any]]:
    """将 PG 画像 traits 精简为可展示条目列表。

    只保留回复上下文真正需要的 key/value/category，过滤内部管理字段，
    避免 chat prompt 或工具结果暴露 importance、updated_at 等系统参数。
    """
    if not isinstance(traits, dict):
        return []

    items: list[dict[str, Any]] = []
    for raw_key, raw_payload in traits.items():
        key = str(raw_key).strip()
        if not key or not isinstance(raw_payload, dict):
            continue
        value = str(raw_payload.get("value", "")).strip()
        if not value:
            continue
        category = str(raw_payload.get("category", "general")).strip() or "general"
        items.append({"key": key, "value": value, "category": category})
    return items


def _operation_to_diff(
    operation: ProfileOperation,
    *,
    old_value: str | None,
    force_op: ProfileOperationOp | None = None,
) -> ProfileDiffItem | None:
    op = force_op or ("set" if operation.op == "set" else operation.op)
    if op == "delete":
        return ProfileDiffItem(
            op="delete",
            user_id=operation.user_id,
            key=operation.key,
            old_value=old_value,
        )
    if operation.value is None:
        return None
    if op == "add":
        return ProfileDiffItem(
            op="add",
            user_id=operation.user_id,
            key=operation.key,
            value=operation.value,
            category=operation.category or "general",
            importance=operation.importance or 3,
        )
    return ProfileDiffItem(
        op="set",
        user_id=operation.user_id,
        key=operation.key,
        old_value=old_value,
        new_value=operation.value,
        category=operation.category or "general",
        importance=operation.importance or 3,
    )


def _trait_value(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("value", "")).strip()
    return value or None
