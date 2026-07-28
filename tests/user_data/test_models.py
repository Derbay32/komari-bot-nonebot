"""user_data 当前好感度模型测试。"""

from komari_bot.plugins.user_data.models import (
    FavorabilityAdjustmentResult,
    FavorabilitySetResult,
    UserFavorability,
    get_favorability_stage,
)


def test_get_favorability_stage_boundaries() -> None:
    cases = [
        (0, 1, "疏离戒备"),
        (99, 1, "疏离戒备"),
        (100, 2, "普通熟人"),
        (199, 2, "普通熟人"),
        (200, 3, "亲近朋友"),
        (299, 3, "亲近朋友"),
        (300, 4, "高度信任"),
        (400, 4, "高度信任"),
    ]

    for score, expected_index, expected_name in cases:
        stage = get_favorability_stage(score)
        assert stage.index == expected_index
        assert stage.name == expected_name


def test_get_favorability_stage_clamps_out_of_range_values() -> None:
    assert get_favorability_stage(-1).index == 1
    assert get_favorability_stage(401).index == 4


def test_favorability_set_result_from_values() -> None:
    result = FavorabilitySetResult.from_values(
        user_id="42",
        before=0,
        after=200,
        updated_at="2026-07-11T23:00:00+08:00",
    )
    assert result.user_id == "42"
    assert result.before == 0
    assert result.after == 200
    assert result.stage_index == 3
    assert result.stage_name == "亲近朋友"
    assert result.updated_at == "2026-07-11T23:00:00+08:00"


def test_favorability_set_result_boundary_stages() -> None:
    cases = [
        (0, 1, "疏离戒备"),
        (99, 1, "疏离戒备"),
        (100, 2, "普通熟人"),
        (199, 2, "普通熟人"),
        (200, 3, "亲近朋友"),
        (299, 3, "亲近朋友"),
        (300, 4, "高度信任"),
        (400, 4, "高度信任"),
    ]
    for after, expected_index, expected_name in cases:
        result = FavorabilitySetResult.from_values(
            user_id="u1",
            before=50,
            after=after,
            updated_at="2026-07-11T23:00:00+08:00",
        )
        assert result.stage_index == expected_index
        assert result.stage_name == expected_name


def test_favorability_set_result_independent_from_adjust_result() -> None:
    """验证 FavorabilitySetResult 字段语义与 FavorabilityAdjustmentResult 不同（不含 delta）。"""
    set_result = FavorabilitySetResult.from_values(
        user_id="u1",
        before=10,
        after=200,
        updated_at="2026-07-11T23:00:00+08:00",
    )
    adjust_result = FavorabilityAdjustmentResult.from_values(
        user_id="u1",
        before=10,
        delta=190,
        after=200,
        updated_at="2026-07-11T23:00:00+08:00",
    )

    assert hasattr(adjust_result, "delta")
    assert not hasattr(set_result, "delta")
    assert set_result.before == adjust_result.before == 10
    assert set_result.after == adjust_result.after == 200


def test_user_favorability_from_score() -> None:
    uf = UserFavorability.from_score(
        user_id="u1",
        favorability=250,
        updated_at="2026-07-11T23:00:00+08:00",
    )
    assert uf.user_id == "u1"
    assert uf.favorability == 250
    assert uf.stage_index == 3
    assert uf.stage_name == "亲近朋友"
