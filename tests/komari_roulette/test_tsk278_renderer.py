# ruff: noqa: RUF001
"""TSK-278 RED baseline: full-Markdown reply renderer seam.

The red root for this file is the missing ``render_reply`` symbol in
``komari_bot.plugins.komari_roulette.qq.renderer``.  Assertions follow
``TSK-278-contract.md`` section 4 and the TSK-266 12.2 / 11.1-11.5 confirmed
layouts, plus the leaderboard spec (10.2) and the real-mention acceptance.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from komari_bot.plugins.komari_roulette.qq.renderer import render_reply

from komari_bot.plugins.komari_roulette import (
    ReplyProjection,
    ReplyProjectionContext,
)

from .tsk278_support import (
    assert_body_has_markdown_structure,
    assert_no_member_openid,
    assert_no_mention_tag,
    assert_no_player_numbers,
    assert_single_mention_tag,
    context,
    game_view,
    mention_tag_position,
    player,
)


def _roster() -> tuple[Any, ...]:
    return (
        player(2, name="小红", inventory=(("beer", 2), ("lock", 1)), pending_lock=True),
        player(1, name="小明", inventory=(("magnifier", 1),)),
        player(3, name="小白", alive=False),
    )


def _follow_up_context() -> ReplyProjectionContext:
    return context(
        result_code="shot",
        lifecycle="active",
        phase="follow_up",
        details={"consumed_kind": "blank", "rewards": ["beer"]},
        players=_roster(),
        current_player=player(1, name="小明"),
        mention_target=player(1, name="小明"),
        mention_reason="current",
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )


# ---------------------------------------------------------------------------
# 继续行动：完整 Markdown 结构
# ---------------------------------------------------------------------------


def test_follow_up_is_complete_markdown() -> None:
    rendered = render_reply(_follow_up_context())
    assert isinstance(rendered, ReplyProjection)
    body = rendered.body

    assert_body_has_markdown_structure(body, dividers=1, blockquote=True, bold=True, roster=True)
    lines = body.splitlines()

    # 块引用结果句：冻结名可见、句子完整。
    assert lines[0].startswith("> ")
    assert "小明" in lines[0]
    assert lines[0].endswith("。")

    # 当前玩家行。
    assert "**当前：小明**" in body

    # 弹仓公共字段。
    assert "弹仓：**4/6**｜实弹 **1**｜空弹 **3**｜中弹概率 **25%**" in body

    # 玩家阵容行：当前加粗、状态与道具数、待锁标记。
    assert "- 小红｜存活｜道具 2｜待锁" in body
    assert "- **小明**｜当前｜道具 1" in body
    assert "- 小白｜出局｜道具 0" in body

    # 普通正文不带玩家编号与内部身份（平台提及 tag 除外）。
    assert_no_player_numbers(body, 1, 2, 3)
    assert_no_member_openid(body, "member-1", "member-2", "member-3")


def test_follow_up_mentions_current_player_once_in_body() -> None:
    rendered = render_reply(_follow_up_context())
    metadata = dict(rendered.metadata)
    assert metadata.get("mention_member_openid") == "member-1"
    assert metadata.get("mention_display_name") == "小明"
    assert len([k for k in metadata if k.startswith("mention_")]) == 2

    # 真实发送载荷以 markdown 正文中的原生提及为准：恰好一个 tag，紧跟
    # ``**当前：小明**``（轮转/奖励位置），不是全文末尾。
    body = rendered.body
    assert_single_mention_tag(body, "member-1")
    tag_at = mention_tag_position(body, "member-1")
    current_line_at = body.index("**当前：小明**")
    assert tag_at > current_line_at
    between = body[current_line_at + len("**当前：小明**") : tag_at]
    assert between == " ", body
    # 分割线定稿：弹仓行之后一条 ``***``，名单在末尾。
    assert body.index("***") > body.index("弹仓：")
    assert body.index("- 小红｜存活｜道具 2｜待锁") > body.index("***")


def test_follow_up_keyboard_spec_is_frozen_json() -> None:
    rendered = render_reply(_follow_up_context())
    spec = rendered.metadata.get("keyboard")
    assert isinstance(spec, str)
    parsed = json.loads(spec)
    assert "rows" in parsed
    assert isinstance(parsed["rows"], list)
    assert parsed["rows"], "follow_up must offer buttons"


# ---------------------------------------------------------------------------
# 奖励选择：两条分割线 + 提及位置
# ---------------------------------------------------------------------------


def test_reward_choice_has_two_dividers() -> None:
    base = context(
        result_code="item_choice_pending",
        lifecycle="active",
        phase="item_choice",
        details={
            "pending_item": "magnifier",
            "pending_item_count": 1,
            "inventory_full": True,
        },
        players=(
            player(2, name="小红", inventory=(("beer", 2), ("lock", 1)), pending_lock=True),
            player(1, name="小明", inventory=(("magnifier", 1), ("beer", 2), ("lock", 1))),
            player(3, name="小白", alive=False),
        ),
        current_player=player(1, name="小明"),
        reward_player=player(1, name="小明"),
        mention_target=player(1, name="小明"),
        mention_reason="reward",
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
            pending_reward_count=1,
        ),
    )
    rendered = render_reply(base)
    body = rendered.body

    assert_body_has_markdown_structure(body, dividers=2, blockquote=True, bold=True, roster=True)
    assert "当前新道具：**放大镜**" in body
    assert "已有道具：放大镜 ×1、啤酒 ×2、锁 ×1" in body
    assert "后续待处理奖励：**1 件**" in body
    assert "**当前：小明**" in body
    assert "弹仓：**4/6**｜实弹 **1**｜空弹 **3**｜中弹概率 **25%**" in body
    assert "- 小红｜存活｜道具 2｜待锁" in body
    assert_no_player_numbers(body, 1, 2, 3)
    assert_no_member_openid(body, "member-1", "member-2", "member-3")

    # 奖励选择：提及在 ``**当前：小明**`` 之后。
    assert_single_mention_tag(body, "member-1")
    tag_at = mention_tag_position(body, "member-1")
    current_line_at = body.index("**当前：小明**")
    assert tag_at > current_line_at
    assert body[current_line_at + len("**当前：小明**") : tag_at] == " "


# ---------------------------------------------------------------------------
# 成功上锁：@锁目标（括号内位置）
# ---------------------------------------------------------------------------


def test_lock_success_mentions_lock_target() -> None:
    base = context(
        result_code="item_used",
        lifecycle="active",
        phase="follow_up",
        details={"item": "lock", "target_player_seq": 2},
        players=(
            player(2, name="小红", inventory=(("magnifier", 1),)),
            player(1, name="小明", inventory=(("beer", 2), ("lock", 1)), pending_lock=True),
            player(3, name="小白", alive=False),
        ),
        current_player=player(2, name="小红"),
        lock_target_player=player(1, name="小明"),
        mention_target=player(1, name="小明"),
        mention_reason="lock_target",
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rendered = render_reply(base)
    body = rendered.body

    assert_body_has_markdown_structure(body, dividers=1, blockquote=True, bold=True, roster=True)
    assert "小红对小明（" in body and "）使用了锁。" in body
    assert "**当前：小红**" in body
    assert "- **小红**｜当前｜道具 1" in body
    assert "- 小明｜存活｜道具 2｜待锁" in body

    # 锁目标提及：位于 ``小明（`` 之后、``）`` 之前。
    assert_single_mention_tag(body, "member-1")
    tag_at = mention_tag_position(body, "member-1")
    open_paren_at = body.index("小红对小明（") + len("小红对小明（")
    close_paren_at = body.index("）使用了锁。")
    assert open_paren_at <= tag_at < close_paren_at

    metadata = dict(rendered.metadata)
    assert metadata.get("mention_member_openid") == "member-1"
    assert metadata.get("mention_display_name") == "小明"
    assert_no_player_numbers(body, 1, 2, 3)
    assert_no_member_openid(body, "member-1", "member-2", "member-3")


# ---------------------------------------------------------------------------
# 原地继续（原地不@）：actor 就是当前玩家 → 无提及
# ---------------------------------------------------------------------------


def test_continue_in_place_does_not_mention() -> None:
    base = context(
        result_code="shot",
        lifecycle="active",
        phase="follow_up",
        details={"consumed_kind": "blank"},
        players=_roster(),
        current_player=player(1, name="小明"),
        mention_target=None,
        mention_reason=None,
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rendered = render_reply(base)
    body = rendered.body
    assert_no_mention_tag(body)
    metadata = dict(rendered.metadata)
    assert "mention_member_openid" not in metadata
    assert "mention_display_name" not in metadata
    assert "**当前：小明**" in body


# ---------------------------------------------------------------------------
# 终局：普通单段 + 胜者提及
# ---------------------------------------------------------------------------


def test_final_is_single_plain_paragraph() -> None:
    base = context(
        result_code="shot",
        lifecycle="completed",
        phase=None,
        winner=player(1, name="小明"),
        winner_group_wins=1,
        mention_target=player(1, name="小明"),
        mention_reason="winner",
        players=(player(1, name="小明"), player(3, name="小白", alive=False)),
        view=game_view(remaining_total=0, remaining_live=0, remaining_blank=0, hit_percent=None),
    )
    rendered = render_reply(base)
    body = rendered.body

    assert_body_has_markdown_structure(
        body, dividers=0, blockquote=False, bold=False, roster=False
    )
    assert "\n\n" not in body.strip("\n"), "final must be a single paragraph"
    assert_no_player_numbers(body, 1, 3)
    assert_no_member_openid(body, "member-1", "member-2", "member-3")

    # 终局 = ``{冻结名} {tag} 获胜，累计胜场 {n}。``（TSK-266 真实提及验收定稿）。
    assert_single_mention_tag(body, "member-1")
    assert body == '小明 <qqbot-at-user id="member-1" /> 获胜，累计胜场 1。'


def test_final_mentions_winner_once_with_metadata_pair() -> None:
    base = context(
        result_code="shot",
        lifecycle="completed",
        phase=None,
        winner=player(1, name="小明"),
        winner_group_wins=6,
        mention_target=player(1, name="小明"),
        mention_reason="winner",
    )
    rendered = render_reply(base)
    body = rendered.body
    assert_single_mention_tag(body, "member-1")
    metadata = dict(rendered.metadata)
    assert metadata.get("mention_member_openid") == "member-1"
    assert metadata.get("mention_display_name") == "小明"
    assert len([k for k in metadata if k.startswith("mention_")]) == 2
    # 终局无按钮。
    assert json.loads(metadata["keyboard"])["rows"] == []


# ---------------------------------------------------------------------------
# 排行榜（TSK-266 10.2）
# ---------------------------------------------------------------------------


def _leaderboard_context(entries: tuple[str, ...]) -> ReplyProjectionContext:
    return context(
        result_code="leaderboard",
        lifecycle="active",
        phase=None,
        details={"leaderboard": entries},
        players=(),
        current_player=None,
        mention_target=None,
        mention_reason=None,
        view=None,
    )


def test_leaderboard_no_winners_copy() -> None:
    rendered = render_reply(_leaderboard_context(()))
    body = rendered.body
    assert body == "本群还没有俄罗斯轮盘胜者。"
    assert_no_mention_tag(body)
    assert json.loads(rendered.metadata["keyboard"])["rows"] == []


def test_leaderboard_shows_top_10_and_total_all_winners() -> None:
    entries = tuple(f"玩家{i}:{i}" for i in range(12, 0, -1))
    rendered = render_reply(_leaderboard_context(entries))
    body = rendered.body

    assert body.startswith("**本群俄罗斯轮盘排行榜｜前 10 名**")
    # 排行榜行首是名次，不是玩家编号；每行 ``名次. 冻结名|N 胜``。
    for rank in range(1, 11):
        assert f"{rank}. 玩家{13 - rank}｜{13 - rank} 胜" in body, body
    assert "11. 玩家2｜2 胜" not in body  # 只显示前 10
    assert "12. 玩家1｜1 胜" not in body
    # 总数统计所有胜者（含未进前 10 者）。
    assert "共有 12 名玩家取得过胜利。" in body
    assert_no_mention_tag(body)
    assert_no_member_openid(body, "member-1", "member-2", "member-3")


def test_leaderboard_rank_is_not_player_seq() -> None:
    entries = ("小红:8", "小明:5", "小白:5")
    rendered = render_reply(_leaderboard_context(entries))
    body = rendered.body
    assert "1. 小红｜8 胜" in body
    assert "2. 小明｜5 胜" in body
    assert "3. 小白｜5 胜" in body
    assert "共有 3 名玩家取得过胜利。" in body
    assert_no_mention_tag(body)
    assert_no_member_openid(body, "member-1", "member-2", "member-3")


def test_leaderboard_uses_frozen_latest_win_name() -> None:
    # 最近一次获胜对局保存的冻结显示名（改名不刷新）直接来自
    # storage.list_leaderboard 的 display_name，渲染层不得自行替换/拼接。
    entries = ("小鞠旧名", "另一人:3")
    rendered = render_reply(_leaderboard_context(entries))
    body = rendered.body
    assert "1. 小鞠旧名｜1 胜" in body
    assert "小鞠旧名" in body


def test_leaderboard_timeout_result_suppresses_leaderboard() -> None:
    # TSK-266 10.2：排行榜查询推进查询者自己到期 → 返回超时结果而非排行榜。
    # 服务层（真实 PG 测试）保证该场景 result_code=turn_expired；渲染层此时
    # 必须输出固定超时文案，不得渲染排行榜。
    base = context(
        result_code="turn_expired",
        lifecycle="active",
        phase="follow_up",
        details={"eliminated_reason": "timeout"},
        players=_roster(),
        current_player=player(2, name="小红"),
        mention_target=None,
        mention_reason=None,
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rendered = render_reply(base)
    body = rendered.body
    assert body == "你的行动时间已经结束，本次命令未执行。"
    assert "排行榜" not in body
    assert_no_mention_tag(body)


# ---------------------------------------------------------------------------
# 道具面板：锁目标编号区（唯一允许编号的专用区域）
# ---------------------------------------------------------------------------


def test_item_panel_lists_lock_target_numbers() -> None:
    base = context(
        result_code="panel_opened",
        lifecycle="active",
        phase="follow_up",
        details={"inventory": (("magnifier", 1), ("beer", 1), ("burst", 1), ("lock", 1))},
        players=(
            player(2, name="小红", inventory=(("lock", 1),)),
            player(1, name="小明", inventory=(("magnifier", 1), ("beer", 1), ("burst", 1), ("lock", 1))),
            player(5, name="小白", inventory=(("beer", 1),)),
        ),
        current_player=player(1, name="小明"),
        view=game_view(
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
            pending_lock_seqs=(2, 5),
            pending_lock_players=(
                player(2, name="小红", inventory=(("lock", 1),)),
                player(5, name="小白", inventory=(("beer", 1),)),
            ),
        ),
    )
    rendered = render_reply(base)
    body = rendered.body

    # 标题不带编号。
    assert "**小明的道具**" in body
    assert "- A｜放大镜 ×1" in body
    assert "- B｜啤酒 ×1" in body
    assert "- C｜连发器 ×1" in body
    assert "- D｜锁 ×1" in body

    # 可上锁区域：稳定编号 + 冻结显示名，保留缺口。
    assert "**可上锁的玩家**" in body
    assert "- 2｜小红" in body
    assert "- 5｜小白" in body
    assert "使用锁时，在“使用”后补上 D 和目标编号。" in body

    # 编号只出现在目标区，不出现在普通正文。
    ordinary = body.split("**可上锁的玩家**")[0]
    assert_no_player_numbers(ordinary, 2, 5)
    assert_no_member_openid(body, "member-1", "member-2", "member-5")


def test_item_panel_hides_lock_area_without_lock() -> None:
    base = context(
        result_code="panel_opened",
        lifecycle="active",
        phase="follow_up",
        details={"inventory": (("magnifier", 1),)},
        players=(player(1, name="小明", inventory=(("magnifier", 1),)),),
        current_player=player(1, name="小明"),
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rendered = render_reply(base)
    body = rendered.body
    assert "可上锁的玩家" not in body
    assert "2｜" not in body


# ---------------------------------------------------------------------------
# 等候阶段：人数/上限、玩家名单、可转让给区域
# ---------------------------------------------------------------------------


def test_waiting_shows_transfer_area_with_numbers() -> None:
    base = context(
        result_code="joined",
        lifecycle="waiting",
        phase=None,
        details={"joined_member_openid": "member-3", "capacity": 6},
        players=(
            player(1, name="小明"),
            player(2, name="小红"),
            player(5, name="小白"),
        ),
        current_player=None,
        mention_target=None,
        view=game_view(
            host_seq=1,
            remaining_total=4,
            remaining_live=1,
            remaining_blank=3,
            hit_percent=25.0,
        ),
    )
    rendered = render_reply(base)
    body = rendered.body

    # 人数/上限与在席名单（无编号）：编号只允许出现在可转让区。
    assert "3/6" in body or "3 人" in body
    assert "局主" in body
    ordinary = body.split("**可转让给**")[0]
    assert_no_player_numbers(ordinary, 1, 2, 5)

    # 可转让给区域：局主之外的稳定编号。
    assert "**可转让给**" in body
    assert "- 2｜小红" in body
    assert "- 5｜小白" in body
    assert "仅局主可转让，使用 /轮盘 转让 玩家编号。" in body
    assert_no_member_openid(body, "member-1", "member-2", "member-5")


# ---------------------------------------------------------------------------
# 名单转义：名字不得注入 Markdown / XML / 伪造第二个提及
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "visible", "forbidden"),
    [
        ("[恶意](http://evil.example)", "恶意", "[恶意](http://evil.example)"),
        ("小明**加粗**", "小明", "小明**加粗**"),
        ("[小红](https://x)", "小红", "[小红](https://x)"),
        ("小明 `code`", "小明", "小明 `code`"),
    ],
)
def test_render_escapes_unsafe_markdown_names(
    name: str,
    visible: str,
    forbidden: str,
) -> None:
    base = context(
        result_code="shot",
        lifecycle="active",
        phase="follow_up",
        details={},
        players=(player(1, name=name),),
        current_player=player(1, name=name),
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rendered = render_reply(base)
    body = rendered.body
    assert visible in body  # 名字内容可见
    assert forbidden not in body, f"unsafe markdown leaked through: {forbidden!r}"


def test_render_escapes_xml_injection_in_frozen_name() -> None:
    name = '<qqbot-at-user id="member-9" /><b>粗</b>&amp;'
    base = context(
        result_code="shot",
        lifecycle="completed",
        phase=None,
        winner=player(1, name=name, member_openid="member-1"),
        winner_group_wins=1,
        mention_target=player(1, name=name, member_openid="member-1"),
        mention_reason="winner",
    )
    rendered = render_reply(base)
    body = rendered.body
    # 冻结名中的 XML 必须转义：不伪造第二个提及、不出现原始 <b> 结构。
    assert_single_mention_tag(body, "member-1")
    assert '<qqbot-at-user id="member-9"' not in body
    assert "<b>" not in body
    # 名字内容仍可见（&amp; 已实体化，但字符不会消失）。
    assert "粗" in body
    assert_no_member_openid(body, "member-1")


# ---------------------------------------------------------------------------
# 固定错误：无状态泄漏、无 mention、无按钮
# ---------------------------------------------------------------------------

FIXED_ERROR_CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("unknown_command", "无法识别这条轮盘命令。发送 .docs 轮盘 查看使用说明。", {}),
    (
        "invalid_args:道具 使用 A｜B｜C｜D<玩家编号>",
        "命令参数不正确。正确用法：@Bot /轮盘 道具 使用 A｜B｜C｜D<玩家编号>",
        {},
    ),
    ("invalid_player_seq", "玩家编号格式不正确。请填写不带前导零的正整数。", {}),
    ("player_seq_not_found", "当前游戏中不存在这个玩家编号，请检查目标编号后重新操作。", {}),
    ("invalid_item_letter", "道具字母只能是：A＝放大镜、B＝啤酒、C＝连发器、D＝锁。", {}),
    ("invalid_reward_name", "替换的道具名称只能是：放大镜、啤酒、连发器或锁。", {}),
    ("game_already_exists", "本群已有一局俄罗斯轮盘，暂时不能创建新局。", {}),
    ("no_waiting_game", "本群没有等待开始的俄罗斯轮盘。", {}),
    ("already_joined", "你已经加入当前游戏。", {}),
    ("game_full", "当前游戏已满员（6/6）。", {}),
    ("game_already_started", "游戏已经开始，无法再改变等候阵容。", {}),
    ("not_joined", "你尚未加入当前等候局。", {}),
    ("not_host", "只有当前局主可以执行这个操作。", {}),
    ("not_enough_players", "至少需要 2 名玩家才能开始游戏。", {}),
    ("waiting_game_expired", "这局游戏等待太久仍未开始，现已自动结束。", {}),
    ("no_active_game", "本群没有进行中的俄罗斯轮盘。", {}),
    ("game_completed", "这局俄罗斯轮盘已经结束。", {}),
    ("not_participant", "你不是当前游戏的参与者。", {}),
    ("player_eliminated", "你已经出局，不能再操作这局游戏。", {}),
    ("not_current_player", "现在不是你的回合。", {}),
    ("turn_expired", "你的行动时间已经结束，本次命令未执行。", {}),
    ("state_conflict", "局面刚刚发生变化，本次操作未执行。请根据机器人最新回复重新操作。", {}),
    ("action_not_allowed_in_phase", "当前阶段不能执行这个操作。", {}),
    ("locked_turn_restriction", "你本回合受到锁限制，只能执行一次开枪命令或弃权。", {}),
    ("invalid_game_state", "游戏状态异常，本次操作未执行。请联系管理员。", {}),
    ("chamber_not_ready", "弹仓尚未就绪，本次操作未执行。", {}),
    ("chamber_full", "弹仓已满，不能装填。", {}),
    ("invalid_chamber_state", "弹仓状态异常，本次操作未执行。", {}),
    ("random_source_failed", "随机结果生成失败，本次操作未执行。请重新发送命令。", {}),
    ("item_not_owned", "你没有这件道具。", {}),
    ("no_pending_item_choice", "当前没有需要处理的道具奖励。", {}),
    ("invalid_transfer_target", "不能把局主转让给自己。", {"reason": "self"}),
    (
        "action_not_allowed_in_phase",
        "请先处理当前新道具；现在只能丢弃奖励、替换道具或弃权。",
        {"reason": "item_choice_pending"},
    ),
    ("invalid_item_target", "锁不能对自己使用。", {"reason": "self"}),
    (
        "invalid_item_target",
        "目标玩家已经出局。",
        {"reason": "eliminated"},
    ),
    (
        "item_effect_conflict",
        "手枪已经带有连发效果，不能重复使用连发器。",
        {"reason": "burst_already_pending"},
    ),
    (
        "item_effect_conflict",
        "目标已经有一把待生效的锁。",
        {"reason": "target_already_locked"},
    ),
    (
        "item_precondition_failed",
        "弹仓至少需要剩余 2 发才能使用连发器。",
        {"reason": "insufficient_chamber_for_burst"},
    ),
    (
        "item_precondition_failed",
        "手枪处于待连发状态，不能使用啤酒。",
        {"reason": "beer_blocked_by_burst"},
    ),
]


@pytest.mark.parametrize(
    ("result_code", "expected", "details"),
    FIXED_ERROR_CASES,
)
def test_error_reply_is_fixed_text_without_state(
    result_code: str,
    expected: str,
    details: dict[str, Any],
) -> None:
    base = context(
        result_code=result_code,
        game_id="game-1",
        lifecycle="active",
        phase="follow_up",
        state_revision=7,
        turn_seq=3,
        actor_member_openid="member-9",
        target_mention_count=1,
        details=details,
        players=_roster(),
        current_player=player(1, name="小明"),
        winner=player(1, name="小明"),
        mention_target=player(1, name="小明"),
        mention_reason="current",
        view=game_view(remaining_total=4, remaining_live=1, remaining_blank=3, hit_percent=25.0),
    )
    rendered = render_reply(base)
    body = rendered.body
    assert body == expected, body
    # 错误回复不附加局面、名单、编号、mention 或内部身份。
    assert "***" not in body
    assert "**" not in body
    assert "- " not in body
    assert not any(line.startswith("> ") for line in body.splitlines())
    assert_no_member_openid(body, "member-1", "member-2", "member-3", "member-9")
    metadata = dict(rendered.metadata)
    assert not [k for k in metadata if k.startswith("mention_")], metadata
    assert json.loads(metadata["keyboard"])["rows"] == []


def test_error_never_echoes_raw_input() -> None:
    base = context(
        result_code="invalid_args:道具 使用 A｜B｜C｜D<玩家编号>",
        game_id=None,
        lifecycle=None,
        phase=None,
        state_revision=None,
        turn_seq=None,
        actor_member_openid="member-1",
        target_mention_count=0,
        details={},
    )
    rendered = render_reply(base)
    assert "D2 3" not in rendered.body
    assert "D2" not in rendered.body


def test_render_reply_is_pure_over_frozen_context() -> None:
    """同一冻结投影两次渲染结果逐字节一致（无随机、无状态读取）。"""
    base = _follow_up_context()
    first = render_reply(base)
    second = render_reply(base)
    assert first.body == second.body
    assert dict(first.metadata) == dict(second.metadata)
