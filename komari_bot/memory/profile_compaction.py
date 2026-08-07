"""用户画像规范化与统计的共享逻辑。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

_ALLOWED_CATEGORIES = {"preference", "fact", "relation", "general"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clamp_importance(value: Any, default: int = 3) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(5, parsed))


def _normalize_category(value: Any) -> str:
    category = str(value or "general").strip() or "general"
    if category not in _ALLOWED_CATEGORIES:
        return "general"
    return category


def _trait_sort_key(trait: dict[str, Any]) -> tuple[int, str, str]:
    return (
        _clamp_importance(trait.get("importance", 3)),
        str(trait.get("updated_at", "")),
        str(trait.get("key", "")),
    )


def _dedupe_traits(traits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for trait in sorted(traits, key=_trait_sort_key, reverse=True):
        key = str(trait.get("key", "")).strip()
        if not key or key in seen_keys:
            continue
        value = str(trait.get("value", "")).strip()
        if not value:
            continue
        seen_keys.add(key)
        deduped.append(
            {
                "key": key,
                "value": value,
                "category": _normalize_category(trait.get("category", "general")),
                "importance": _clamp_importance(trait.get("importance", 3)),
                "updated_at": str(trait.get("updated_at", "")).strip(),
            }
        )
    return deduped


def profile_traits_to_list(profile: dict[str, Any]) -> list[dict[str, Any]]:
    traits_raw = profile.get("traits")
    normalized: list[dict[str, Any]] = []

    if isinstance(traits_raw, dict):
        for key, payload in traits_raw.items():
            if not isinstance(payload, dict):
                continue
            value = str(payload.get("value", "")).strip()
            if not value:
                continue
            normalized.append(
                {
                    "key": str(key).strip(),
                    "value": value,
                    "category": _normalize_category(payload.get("category", "general")),
                    "importance": _clamp_importance(payload.get("importance", 3)),
                    "updated_at": str(payload.get("updated_at", "")).strip(),
                }
            )
    elif isinstance(traits_raw, list):
        for payload in traits_raw:
            if not isinstance(payload, dict):
                continue
            key = str(payload.get("key", "")).strip()
            value = str(payload.get("value", "")).strip()
            if not key or not value:
                continue
            normalized.append(
                {
                    "key": key,
                    "value": value,
                    "category": _normalize_category(payload.get("category", "general")),
                    "importance": _clamp_importance(payload.get("importance", 3)),
                    "updated_at": str(payload.get("updated_at", "")).strip(),
                }
            )

    return _dedupe_traits(normalized)


def count_profile_traits(profile: dict[str, Any]) -> int:
    return len(profile_traits_to_list(profile))


def profile_json_length(profile: dict[str, Any]) -> int:
    return len(json.dumps(profile, ensure_ascii=False))


def normalize_profile_for_storage(
    profile: dict[str, Any],
    *,
    fallback_user_id: str = "",
    fallback_display_name: str = "",
    trait_limit: int | None = None,
) -> dict[str, Any]:
    user_id = str(profile.get("user_id", "")).strip() or fallback_user_id
    display_name = str(profile.get("display_name", "")).strip() or fallback_display_name
    traits = profile_traits_to_list(profile)
    if trait_limit is not None:
        traits = traits[: max(0, trait_limit)]

    traits_payload: dict[str, dict[str, Any]] = {}
    for trait in traits:
        key = str(trait.get("key", "")).strip()
        value = str(trait.get("value", "")).strip()
        if not key or not value:
            continue
        traits_payload[key] = {
            "value": value,
            "category": _normalize_category(trait.get("category", "general")),
            "importance": _clamp_importance(trait.get("importance", 3)),
            "updated_at": str(trait.get("updated_at", "")).strip() or _now_iso(),
        }

    return {
        "version": 1,
        "user_id": user_id,
        "display_name": display_name,
        "traits": traits_payload,
        "updated_at": _now_iso(),
    }
