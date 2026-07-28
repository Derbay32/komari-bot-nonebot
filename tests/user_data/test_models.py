"""user_data 当前好感度模型测试。"""

from komari_bot.plugins.user_data.models import get_favorability_stage


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
