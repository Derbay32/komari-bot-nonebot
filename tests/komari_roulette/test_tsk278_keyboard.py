"""TSK-278 RED baseline: QQ button keyboard seam.

The red roots for this file are the missing ``build_keyboard`` and
``keyboard_from_spec`` symbols in ``komari_bot.plugins.komari_roulette.qq
.keyboard``.  Assertions follow ``TSK-278-contract.md`` section 5 and the
TSK-266 1H ``6a9bc5dd`` / 1D ``6a9ac966`` / ``6a9e6ce3`` confirmed layouts:
emoji labels, at most 3 buttons per row, at most 3 Chinese chars per label
(5 when a row has exactly 2 buttons), reward-replacement labels never 6
glyphs, action.type=2 / permission.type=2 / reply=false / enter=false,
fill-only commands.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from komari_bot.plugins.komari_roulette.qq.keyboard import (
    build_keyboard,
    keyboard_from_spec,
)

from .tsk278_support import (
    ButtonSpec,
    context,
    flatten_keyboard,
    game_view,
    player,
)

if TYPE_CHECKING:
    from typing import Any

    from komari_bot.plugins.komari_roulette import ReplyProjectionContext


def _labels(rows: list[list[ButtonSpec]]) -> list[list[str]]:
    return [[button.label for button in row] for row in rows]


def _data(rows: list[list[ButtonSpec]]) -> list[list[str]]:
    return [[button.data for button in row] for row in rows]


def _assert_fill_only(rows: list[list[ButtonSpec]]) -> None:
    for row in rows:
        assert len(row) <= 3, "at most 3 buttons per row"
        for button in row:
            assert button.action_type == 2, button
            assert button.permission_type == 2, button
            assert button.reply is False, button
            assert button.enter is False, button
            assert button.data, "button data must not be empty"


def _materialize(context_obj: ReplyProjectionContext) -> list[list[ButtonSpec]]:
    """build_keyboard → JSON spec → real QQ MessageKeyboard → flattened."""
    spec = build_keyboard(context_obj)
    assert isinstance(spec, str), "build_keyboard must return a JSON spec string"
    parsed = json.loads(spec)
    assert "rows" in parsed and isinstance(parsed["rows"], list)
    return flatten_keyboard(keyboard_from_spec(spec))


def _hanzi_count(label: str) -> int:
    """Count Chinese characters in a label (emoji not counted)."""
    return sum(1 for ch in label if "\u4e00" <= ch <= "\u9fff")


def _follow_up_context() -> ReplyProjectionContext:
    return context(
        result_code="shot",
        lifecycle="active",
        phase="follow_up",
        players=(
            player(2, name="小红", inventory=(("beer", 1), ("lock", 1))),
            player(1, name="小明", inventory=(("magnifier", 1),)),
            player(3, name="小白", alive=False),
        ),
        current_player=player(1, name="小明"),
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )


def test_follow_up_button_layout_1h() -> None:
    rows = _materialize(_follow_up_context())
    assert _labels(rows) == [
        ["🧰使用", "🗑️丢弃"],
        ["🔫开枪", "🔄装填"],
        ["⏹️结束", "🏳️弃权"],
    ]
    _assert_fill_only(rows)
    # 1H 定稿：使用/丢弃只填入前缀，由用户补完道具字母后手动发送，无尾随空格。
    assert _data(rows)[0] == ["/轮盘 道具 使用", "/轮盘 道具 丢弃"]
    assert _data(rows)[1] == ["/轮盘 开枪", "/轮盘 装填"]
    assert _data(rows)[2] == ["/轮盘 结束", "/轮盘 弃权"]


def test_follow_up_labels_within_size_rules() -> None:
    rows = _materialize(_follow_up_context())
    for row in rows:
        for button in row:
            assert _hanzi_count(button.label) <= 3, button.label
    # 2 按钮行可放宽至 5 汉字（本布局均满足更严的 3 字规则）。


def test_locked_turn_buttons() -> None:
    base = context(
        result_code="shot",
        lifecycle="active",
        phase="locked_turn",
        players=(player(1, name="小明"), player(2, name="小红")),
        current_player=player(1, name="小明"),
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
            active_lock_player=player(1, name="小明"),
        ),
    )
    rows = _materialize(base)
    assert _labels(rows) == [["🔫开枪", "🏳️弃权"]]
    _assert_fill_only(rows)
    assert _data(rows)[0] == ["/轮盘 开枪", "/轮盘 弃权"]


def test_locked_phase_alias_is_not_accepted() -> None:
    """`locked` 不是 TSK-276 投影拼写；不得再被当锁定回合处理。"""
    base = context(
        result_code="shot",
        lifecycle="active",
        phase="locked",
        players=(player(1, name="小明"), player(2, name="小红")),
        current_player=player(1, name="小明", inventory=(("magnifier", 1),)),
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
        ),
    )
    labels = [button.label for row in _materialize(base) for button in row]
    # 应该是普通 follow_up 布局（含使用/丢弃），而不是只剩开枪/弃权。
    assert "🧰使用" in labels
    assert "⏹️结束" in labels


# ---------------------------------------------------------------------------
# 1H 动态布局：只展示提交后权威局面允许的动作
# ---------------------------------------------------------------------------


def _follow_up_context_with(
    *,
    inventory: tuple[tuple[str, int], ...],
    others: tuple[Any, ...] = (),
    remaining_total: int = 4,
) -> ReplyProjectionContext:
    current = player(1, name="小明", inventory=inventory)
    roster = (current, *others)
    return context(
        result_code="shot",
        lifecycle="active",
        phase="follow_up",
        players=roster,
        current_player=current,
        view=game_view(
            remaining_total=remaining_total,
            remaining_live=1,
            remaining_blank=max(remaining_total - 1, 0),
            hit_percent=25.0,
        ),
    )


def test_follow_up_without_inventory_omits_use_and_discard() -> None:
    """当前玩家道具 0 → 不出现 🧰使用 / 🗑️丢弃（1H 动态布局）。"""
    base = _follow_up_context_with(
        inventory=(), others=(player(2, name="小红"),)
    )
    labels = [button.label for row in _materialize(base) for button in row]
    assert "🧰使用" not in labels
    assert "🗑️丢弃" not in labels
    assert "🔫开枪" in labels
    assert "⏹️结束" in labels
    assert "🏳️弃权" in labels


def test_follow_up_on_full_chamber_omits_reload() -> None:
    """弹仓 6/6 → 不出现 🔄装填。"""
    base = _follow_up_context_with(
        inventory=(("magnifier", 1),),
        others=(player(2, name="小红"),),
        remaining_total=6,
    )
    labels = [button.label for row in _materialize(base) for button in row]
    assert "🔄装填" not in labels
    assert "🔫开枪" in labels
    assert "🧰使用" in labels


def test_follow_up_lock_only_without_legal_target_omits_use() -> None:
    """只有锁且无合法目标 → 不出现 🧰使用（仍可丢弃）。"""
    base = _follow_up_context_with(
        inventory=(("lock", 1),),
        # 唯一在席对手已有待生效锁 → 不是合法目标。
        others=(player(2, name="小红", pending_lock=True),),
    )
    labels = [button.label for row in _materialize(base) for button in row]
    assert "🧰使用" not in labels
    assert "🗑️丢弃" in labels


def test_follow_up_lock_only_with_legal_target_keeps_use() -> None:
    """只有锁但存在合法目标 → 🧰使用 仍出现。"""
    base = _follow_up_context_with(
        inventory=(("lock", 1),),
        others=(player(2, name="小红"),),
    )
    labels = [button.label for row in _materialize(base) for button in row]
    assert "🧰使用" in labels


def test_follow_up_omits_item_buttons_when_all_counts_zero() -> None:
    """只有 0 数量条目等价于无道具：不出现使用/丢弃。"""
    base = _follow_up_context_with(
        inventory=(("magnifier", 0), ("lock", 0)),
        others=(player(2, name="小红"),),
    )
    labels = [button.label for row in _materialize(base) for button in row]
    assert "🧰使用" not in labels
    assert "🗑️丢弃" not in labels


def test_keyboard_from_spec_rejects_bare_list() -> None:
    with pytest.raises((TypeError, KeyError, ValueError)):
        keyboard_from_spec("[]")


def test_keyboard_from_spec_rejects_dict_rows() -> None:
    """规范行恒为按钮对象列表；`{"buttons": [...]}` 不再兼容。"""
    with pytest.raises((TypeError, KeyError, ValueError)):
        keyboard_from_spec('{"rows": [{"buttons": []}]}')


def test_item_choice_buttons_1d() -> None:
    base = context(
        result_code="item_choice_pending",
        lifecycle="active",
        phase="item_choice",
        details={"pending_item": "magnifier"},
        players=(player(1, name="小明", inventory=(("magnifier", 1), ("beer", 2), ("lock", 1))),),
        current_player=player(1, name="小明"),
        reward_player=player(1, name="小明"),
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
            pending_reward_count=1,
        ),
    )
    rows = _materialize(base)
    assert _labels(rows) == [
        ["🗑️丢弃新道具", "🏳️弃权"],
        ["🔄放大镜", "🔄啤酒", "🔄锁"],
    ]
    _assert_fill_only(rows)
    assert _data(rows)[0] == ["/轮盘 奖励 丢弃", "/轮盘 弃权"]
    assert _data(rows)[1] == [
        "/轮盘 奖励 替换 放大镜",
        "/轮盘 奖励 替换 啤酒",
        "/轮盘 奖励 替换 锁",
    ]


def test_item_choice_replacement_labels_never_six_glyphs() -> None:
    base = context(
        result_code="item_choice_pending",
        lifecycle="active",
        phase="item_choice",
        details={"pending_item": "burst"},
        players=(player(1, name="小明", inventory=(("magnifier", 1), ("beer", 1), ("burst", 1), ("lock", 1))),),
        current_player=player(1, name="小明"),
        reward_player=player(1, name="小明"),
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
            pending_reward_count=1,
        ),
    )
    rows = _materialize(base)
    labels = [button.label for row in rows for button in row]
    # 奖励替换按钮不得是 ``🔄替换放大镜`` 这类 6 字标签；emoji + 最多 3 汉字
    # （2 按钮行放宽至 5 汉字，本行首行 ``🗑️丢弃新道具`` 恰为 5 汉字）。
    assert "🔄替换放大镜" not in labels
    assert "🔄替换啤酒" not in labels
    assert "🔄替换连发器" not in labels
    assert "🔄替换锁" not in labels
    for row in rows:
        limit = 5 if len(row) == 2 else 3
        for button in row:
            assert _hanzi_count(button.label) <= limit, button.label
    # 每行不超过 3 个按钮；4 种持有类型须分两行。
    for row in rows:
        assert len(row) <= 3, row


def test_item_choice_held_types_are_fill_only_and_ordered() -> None:
    base = context(
        result_code="item_choice_pending",
        lifecycle="active",
        phase="item_choice",
        details={"pending_item": "beer"},
        players=(player(1, name="小明", inventory=(("lock", 1), ("beer", 1), ("magnifier", 1))),),
        current_player=player(1, name="小明"),
        reward_player=player(1, name="小明"),
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
            pending_reward_count=1,
        ),
    )
    rows = _materialize(base)
    # 持有类型按 A→B→C→D 顺序；重复类型不重复出按钮。
    replace_labels = [
        button.label for row in rows for button in row if button.label.startswith("🔄")
    ]
    assert replace_labels == ["🔄放大镜", "🔄啤酒", "🔄锁"]


def test_waiting_buttons_with_transfer() -> None:
    base = context(
        result_code="joined",
        lifecycle="waiting",
        phase=None,
        players=(player(1, name="小明"), player(2, name="小红")),
        current_player=None,
        view=game_view(host_seq=1, remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rows = _materialize(base)
    # 加入/开始 + 退出/取消 两排，加通用 🔄转让（末尾保留参数分隔空格）。
    labels = _labels(rows)
    assert labels == [
        ["加入", "开始"],
        ["退出", "取消"],
        ["🔄转让"],
    ]
    _assert_fill_only(rows)
    assert _data(rows)[2] == ["/轮盘 转让 "]


def test_waiting_solo_host_omits_transfer() -> None:
    base = context(
        result_code="created",
        lifecycle="waiting",
        phase=None,
        players=(player(1, name="小明"),),
        current_player=None,
        view=game_view(host_seq=1, remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rows = _materialize(base)
    labels = [b for row in _labels(rows) for b in row]
    assert "🔄转让" not in labels


def test_final_has_no_buttons() -> None:
    base = context(
        result_code="shot",
        lifecycle="completed",
        phase=None,
        winner=player(1, name="小明"),
        winner_group_wins=1,
        players=(player(1, name="小明"),),
        view=game_view(remaining_total=0, remaining_live=0, remaining_blank=0, hit_percent=None),
    )
    spec = build_keyboard(base)
    assert json.loads(spec)["rows"] == []


def test_leaderboard_has_no_buttons() -> None:
    base = context(
        result_code="leaderboard",
        lifecycle="active",
        phase=None,
        details={"leaderboard": ("小明:5",)},
        players=(),
        current_player=None,
        view=None,
    )
    spec = build_keyboard(base)
    assert json.loads(spec)["rows"] == []


def test_error_has_no_buttons() -> None:
    base = context(
        result_code="not_current_player",
        game_id=None,
        lifecycle=None,
        phase=None,
        state_revision=None,
        turn_seq=None,
        actor_member_openid="member-9",
        target_mention_count=0,
        details={},
    )
    spec = build_keyboard(base)
    assert json.loads(spec)["rows"] == []


def test_keyboard_spec_round_trips_through_real_adapter() -> None:
    base = _follow_up_context()
    spec = build_keyboard(base)
    keyboard = keyboard_from_spec(spec)
    rows = flatten_keyboard(keyboard)
    assert rows == flatten_keyboard(keyboard_from_spec(spec))


def test_keyboard_is_pure_over_frozen_context() -> None:
    base = _follow_up_context()
    first = _materialize(base)
    second = _materialize(base)
    assert first == second
