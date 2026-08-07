"""用户画像操作 patch 转换测试。"""

from __future__ import annotations

from komari_bot.memory.profile_operations import (
    ProfileDiffItem,
    ProfileOperation,
    build_profile_traits_patch,
)


def test_build_profile_traits_patch_handles_add_set_and_delete() -> None:
    set_traits, delete_keys = build_profile_traits_patch(
        [
            ProfileDiffItem(
                op="add",
                user_id="u1",
                key="喜欢的游戏",
                value="塞尔达传说",
                category="preference",
                importance=5,
            ),
            ProfileDiffItem(
                op="set",
                user_id="u1",
                key="喜欢的食物",
                new_value="布丁",
                category="preference",
                importance=4,
            ),
            ProfileDiffItem(op="delete", user_id="u1", key="旧爱好"),
        ]
    )

    assert set_traits["喜欢的游戏"]["value"] == "塞尔达传说"
    assert set_traits["喜欢的游戏"]["importance"] == 5
    assert set_traits["喜欢的食物"]["value"] == "布丁"
    assert delete_keys == ["旧爱好"]


def test_build_profile_traits_patch_filters_empty_values() -> None:
    set_traits, delete_keys = build_profile_traits_patch(
        [
            ProfileOperation(op="add", user_id="u1", key="空值", value=""),
            ProfileOperation(op="set", user_id="u1", key="空白", value="   "),
            ProfileOperation(op="delete", user_id="u1", key=""),
        ]
    )

    assert set_traits == {}
    assert delete_keys == []


def test_build_profile_traits_patch_delete_wins_for_same_key() -> None:
    set_traits, delete_keys = build_profile_traits_patch(
        [
            ProfileOperation(op="add", user_id="u1", key="喜欢的游戏", value="塞尔达传说"),
            ProfileOperation(op="delete", user_id="u1", key="喜欢的游戏"),
            ProfileOperation(op="set", user_id="u1", key="喜欢的游戏", value="星之卡比"),
        ]
    )

    assert set_traits == {}
    assert delete_keys == ["喜欢的游戏"]
