"""用户画像规范化工具测试。"""

from __future__ import annotations

from typing import Any

from komari_bot.memory import profile_compaction as profile_compaction_module


def _make_profile(trait_count: int, *, uniform: bool = False) -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": "10001",
        "display_name": "阿明",
        "traits": {
            f"特征{i:02d}": {
                "value": f"长期描述{i}",
                "category": "general",
                "importance": 3 if uniform else 5 - (i % 3),
                "updated_at": (
                    "2026-03-21T00:00:00+08:00"
                    if uniform
                    else f"2026-03-21T00:00:{i % 60:02d}+08:00"
                ),
            }
            for i in range(trait_count)
        },
    }


def test_count_profile_traits_counts_valid_traits() -> None:
    profile = _make_profile(3)
    profile["traits"]["空值"] = {"value": "", "category": "general"}
    profile["traits"]["坏值"] = "不是对象"

    assert profile_compaction_module.count_profile_traits(profile) == 3


def test_profile_traits_to_list_supports_dict_and_list_shapes() -> None:
    dict_profile = {
        "traits": {
            "偏好": {"value": "喜欢策略游戏", "category": "preference", "importance": 5},
            "未知分类": {"value": "长期稳定信息", "category": "bad", "importance": 9},
        }
    }
    list_profile = {
        "traits": [
            {"key": "职业", "value": "软件工程师", "category": "fact", "importance": 4},
            {"key": "", "value": "缺少 key"},
        ]
    }

    dict_traits = profile_compaction_module.profile_traits_to_list(dict_profile)
    list_traits = profile_compaction_module.profile_traits_to_list(list_profile)

    assert {trait["key"] for trait in dict_traits} == {"偏好", "未知分类"}
    assert next(trait for trait in dict_traits if trait["key"] == "未知分类")["category"] == "general"
    assert next(trait for trait in dict_traits if trait["key"] == "未知分类")["importance"] == 5
    assert [trait["key"] for trait in list_traits] == ["职业"]


def test_normalize_profile_for_storage_preserves_program_fields_and_caps_traits() -> None:
    result = profile_compaction_module.normalize_profile_for_storage(
        _make_profile(8, uniform=True),
        fallback_user_id="fallback-user",
        fallback_display_name="fallback-name",
        trait_limit=3,
    )

    assert result["user_id"] == "10001"
    assert result["display_name"] == "阿明"
    assert len(result["traits"]) == 3
    assert result["version"] == 1
    assert result["updated_at"]


def test_normalize_profile_for_storage_uses_fallback_fields() -> None:
    result = profile_compaction_module.normalize_profile_for_storage(
        {"traits": {"偏好": {"value": "喜欢文字游戏"}}},
        fallback_user_id="10086",
        fallback_display_name="小明",
    )

    assert result["user_id"] == "10086"
    assert result["display_name"] == "小明"
    assert result["traits"]["偏好"]["category"] == "general"
    assert result["traits"]["偏好"]["importance"] == 3


def test_profile_json_length_uses_chinese_without_ascii_escape() -> None:
    profile = {"traits": {"偏好": {"value": "喜欢中文"}}}

    assert profile_compaction_module.profile_json_length(profile) == len(
        '{"traits": {"偏好": {"value": "喜欢中文"}}}'
    )
