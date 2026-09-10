# ruff: noqa: RUF001
"""TSK-278 RED baseline: QQ roulette command parser seam.

The single red root for this file is the missing ``parse_command`` symbol in
``komari_bot.plugins.komari_roulette.qq.parser``.  All cases below describe
the observable parsing contract recorded in ``TSK-278-contract.md`` section 3.
"""

from __future__ import annotations

import pytest

from komari_bot.plugins.komari_roulette import CanonicalCommand
from komari_bot.plugins.komari_roulette.qq.parser import parse_command

SYNTAX = "syntax_failure"


def _syntax(text: str) -> CanonicalCommand:
    command = parse_command(text)
    assert command is not None, f"expected a roulette command for {text!r}"
    assert command.intent == SYNTAX, f"expected syntax_failure, got {command}"
    return command


def _code(command: CanonicalCommand) -> str:
    """Narrow the optional syntax_code after asserting the failure intent."""

    assert command.syntax_code is not None, command
    return command.syntax_code


def _ok(text: str) -> CanonicalCommand:
    command = parse_command(text)
    assert command is not None, f"expected a roulette command for {text!r}"
    assert command.intent != SYNTAX, f"expected a real command, got {command}"
    return command


# ---------------------------------------------------------------------------
# Entry recognition
# ---------------------------------------------------------------------------


def test_parse_returns_none_for_non_roulette_text() -> None:
    for text in ("", "你好", "开枪", "/轮盘子", "轮盘 开枪", "help", " /轮盘x "):
        assert parse_command(text) is None, text


def test_parse_accepts_leading_and_trailing_whitespace() -> None:
    command = _ok("  /轮盘  开枪  ")
    assert command.intent == "shoot"


def test_parse_collapses_internal_whitespace() -> None:
    command = _ok("/轮盘  道具   使用   a")
    assert command.intent == "use_item"
    assert command.item == "magnifier"


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("/轮盘 开局", "create"),
        ("/轮盘 加入", "join"),
        ("/轮盘 退出", "leave"),
        ("/轮盘 取消", "cancel"),
        ("/轮盘 开始", "start"),
        ("/轮盘 开枪", "shoot"),
        ("/轮盘 弃权", "forfeit"),
        ("/轮盘 结束", "end_turn"),
        ("/轮盘 装填", "reload"),
        ("/轮盘 道具", "open_item_panel"),
        ("/轮盘 排行榜", "leaderboard"),
    ],
)
def test_parse_bare_subcommands(text: str, intent: str) -> None:
    assert _ok(text).intent == intent


def test_parse_leaderboard_accepts_no_arguments() -> None:
    command = _syntax("/轮盘 排行榜 x")
    assert _code(command) == "invalid_args:排行榜"


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("/轮盘 帮助", None),
        ("/轮盘 排行", None),
        ("/轮盘 查看", None),
        ("/轮盘 开始吧", None),
    ],
)
def test_parse_unknown_subcommand_is_unknown_command(
    text: str,
    intent: None,
) -> None:
    del intent
    command = _syntax(text)
    assert command.syntax_code == "unknown_command"


def test_parse_help_was_removed() -> None:
    # TSK-266 10.1: /轮盘 帮助 is deleted and must resolve as unknown.
    command = _syntax("/轮盘 帮助")
    assert command.syntax_code == "unknown_command"


def test_parse_bare_prefix_is_invalid_args() -> None:
    command = _syntax("/轮盘")
    assert command.syntax_code == "invalid_args"


def test_parse_bare_prefix_with_space_is_invalid_args() -> None:
    command = _syntax("/轮盘 ")
    assert command.syntax_code == "invalid_args"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("/轮盘 开局 额外", "invalid_args:开局"),
        ("/轮盘 加入 额外", "invalid_args:加入"),
        ("/轮盘 退出 额外", "invalid_args:退出"),
        ("/轮盘 取消 额外", "invalid_args:取消"),
        ("/轮盘 开始 额外", "invalid_args:开始"),
        ("/轮盘 开枪 1", "invalid_args:开枪"),
        ("/轮盘 弃权 额外", "invalid_args:弃权"),
        ("/轮盘 结束 额外", "invalid_args:结束"),
        ("/轮盘 装填 额外", "invalid_args:装填"),
    ],
)
def test_parse_bare_subcommand_with_extra_args_carries_usage(
    text: str, code: str
) -> None:
    """已识别裸子命令 + 多余参数必须携带该子命令的用法后缀。

    TSK-266 11.1：参数多余与参数缺失/格式错误同属“用法文案”；裸 `invalid_args`
    会让渲染层落入通用系统错误文案（见 renderer 的 `test_bare_invalid_args_*`）。
    用法模板永不含原始输入，这里只断言后缀稳定且不回显额外参数。
    """
    command = _syntax(text)
    assert _code(command) == code
    assert "额外" not in _code(command)


def test_parse_bare_subcommand_usage_suffix_is_static() -> None:
    """同一子命令的不同多余参数必须产生相同用法后缀（不回显输入）。"""
    first = _code(_syntax("/轮盘 开局 额外"))
    second = _code(_syntax("/轮盘 开局 别的"))
    assert first == second == "invalid_args:开局"


# ---------------------------------------------------------------------------
# Item letters (case-insensitive) and lock target numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("letter", "item"),
    [
        ("A", "magnifier"),
        ("a", "magnifier"),
        ("B", "beer"),
        ("b", "beer"),
        ("C", "burst"),
        ("c", "burst"),
    ],
)
def test_parse_use_item_letter_case_insensitive(letter: str, item: str) -> None:
    command = _ok(f"/轮盘 道具 使用 {letter}")
    assert command.intent == "use_item"
    assert command.item == item
    assert command.target_player_seq is None


@pytest.mark.parametrize("token", ["D", "d"])
def test_parse_use_lock_with_target(token: str) -> None:
    command = _ok(f"/轮盘 道具 使用 {token}2")
    assert command.intent == "use_item"
    assert command.item == "lock"
    assert command.target_player_seq == 2


@pytest.mark.parametrize(
    "text",
    [
        "/轮盘 道具 使用 D",
        "/轮盘 道具 使用 d",
        "/轮盘 道具 使用",
    ],
)
def test_parse_use_lock_missing_target_is_invalid_args(text: str) -> None:
    command = _syntax(text)
    assert _code(command).startswith("invalid_args")


def test_parse_use_lock_missing_target_carries_static_usage() -> None:
    command = _syntax("/轮盘 道具 使用 D")
    assert command.syntax_code == "invalid_args:道具 使用 A｜B｜C｜D<玩家编号>"


@pytest.mark.parametrize(
    "text",
    [
        "/轮盘 道具 使用 X",
        "/轮盘 道具 使用 E",
        "/轮盘 道具 使用 A2",
        "/轮盘 道具 使用 锁",
        "/轮盘 道具 使用 1",
    ],
)
def test_parse_use_item_bad_letter_is_invalid_item_letter(text: str) -> None:
    command = _syntax(text)
    assert _code(command) == "invalid_item_letter"


@pytest.mark.parametrize(
    "text",
    [
        "/轮盘 道具 使用 D0",
        "/轮盘 道具 使用 D01",
        "/轮盘 道具 使用 D1x",
        "/轮盘 道具 使用 D-2",
    ],
)
def test_parse_use_lock_bad_number_is_invalid_player_seq(text: str) -> None:
    command = _syntax(text)
    assert command.syntax_code == "invalid_player_seq"


@pytest.mark.parametrize(
    ("letter", "item"),
    [("A", "magnifier"), ("b", "beer"), ("C", "burst"), ("d", "lock")],
)
def test_parse_discard_item_case_insensitive(letter: str, item: str) -> None:
    command = _ok(f"/轮盘 道具 丢弃 {letter}")
    assert command.intent == "discard_item"
    assert command.item == item


def test_parse_discard_item_missing_arg_is_invalid_args() -> None:
    command = _syntax("/轮盘 道具 丢弃")
    assert _code(command).startswith("invalid_args")


def test_parse_discard_item_bad_letter_is_invalid_item_letter() -> None:
    command = _syntax("/轮盘 道具 丢弃 X")
    assert _code(command) == "invalid_item_letter"


def test_parse_item_extra_args_is_invalid_args() -> None:
    for text in ("/轮盘 道具 使用 A 2", "/轮盘 道具 使用 D 2"):
        command = _syntax(text)
        assert _code(command).startswith("invalid_args")


# ---------------------------------------------------------------------------
# Reward choices (names are case-sensitive Chinese)
# ---------------------------------------------------------------------------


def test_parse_reward_discard() -> None:
    command = _ok("/轮盘 奖励 丢弃")
    assert command.intent == "choose_item"
    assert command.decision == "discard"
    assert command.replace_item is None


@pytest.mark.parametrize(
    ("name", "item"),
    [
        ("放大镜", "magnifier"),
        ("啤酒", "beer"),
        ("连发器", "burst"),
        ("锁", "lock"),
    ],
)
def test_parse_reward_replace(name: str, item: str) -> None:
    command = _ok(f"/轮盘 奖励 替换 {name}")
    assert command.intent == "choose_item"
    assert command.decision == "replace"
    assert command.replace_item == item


def test_parse_reward_replace_bad_name_is_invalid_reward_name() -> None:
    command = _syntax("/轮盘 奖励 替换 别的")
    assert command.syntax_code == "invalid_reward_name"


@pytest.mark.parametrize(
    "text",
    [
        "/轮盘 奖励",
        "/轮盘 奖励 替换",
        "/轮盘 奖励 丢弃 额外",
        "/轮盘 奖励 替换 放大镜 额外",
    ],
)
def test_parse_reward_wrong_arity_is_invalid_args(text: str) -> None:
    command = _syntax(text)
    assert _code(command).startswith("invalid_args")


# ---------------------------------------------------------------------------
# Transfer player numbers
# ---------------------------------------------------------------------------


def test_parse_transfer() -> None:
    command = _ok("/轮盘 转让 2")
    assert command.intent == "transfer"
    assert command.target_player_seq == 2


@pytest.mark.parametrize(
    "text",
    [
        "/轮盘 转让",
        "/轮盘 转让 2 3",
    ],
)
def test_parse_transfer_wrong_arity_is_invalid_args(text: str) -> None:
    command = _syntax(text)
    assert _code(command).startswith("invalid_args")


@pytest.mark.parametrize(
    "text",
    [
        "/轮盘 转让 0",
        "/轮盘 转让 01",
        "/轮盘 转让 007",
        "/轮盘 转让 x",
        "/轮盘 转让 -1",
        "/轮盘 转让 2.5",
        "/轮盘 转让 ２",
    ],
)
def test_parse_transfer_bad_number_is_invalid_player_seq(text: str) -> None:
    command = _syntax(text)
    assert command.syntax_code == "invalid_player_seq"


# ---------------------------------------------------------------------------
# Stability: parser must not throw and must not mutate shared state
# ---------------------------------------------------------------------------


def test_parse_never_raises_on_arbitrary_text() -> None:
    for text in ("/轮盘 " * 100, "/轮盘\n开枪", "/轮盘 道具 使用 D" * 5, "\x00"):
        try:
            parse_command(text)
        except Exception as error:  # pragma: no cover - assertion below
            pytest.fail(f"parse_command raised {type(error).__name__} for {text!r}")


def test_parse_result_is_frozen_dataclass_value() -> None:
    command = _ok("/轮盘 道具 使用 d2")
    assert isinstance(command, CanonicalCommand)
    assert command.target_player_seq == 2
    assert command.fingerprint_fields()["intent"] == "use_item"
