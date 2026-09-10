# ruff: noqa: RUF001  # 命令用法模板使用的全角分隔符
"""QQ roulette command parser seam (TSK-278).

``parse_command`` is a pure function mapping a message text to a
``CanonicalCommand`` (or ``None`` for non-roulette input).  Parsing never
raises, never logs and never touches services.  Syntax failures are typed as
``CanonicalCommand.syntax_failure(code=...)`` so the service can project a
fixed reply without ever echoing the raw input back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..command_service import CanonicalCommand

if TYPE_CHECKING:
    from collections.abc import Callable

PREFIX = "/轮盘"

ITEM_BY_LETTER: dict[str, str] = {
    "A": "magnifier",
    "B": "beer",
    "C": "burst",
    "D": "lock",
}
REWARD_NAME_TO_ITEM: dict[str, str] = {
    "放大镜": "magnifier",
    "啤酒": "beer",
    "连发器": "burst",
    "锁": "lock",
}

ITEM_USE_USAGE = "道具 使用 A｜B｜C｜D<玩家编号>"
ITEM_DISCARD_USAGE = "道具 丢弃 <道具字母>"
REWARD_USAGE = "奖励 丢弃｜替换 <道具名>"
TRANSFER_USAGE = "转让 <玩家编号>"
LEADERBOARD_USAGE = "排行榜"

_BARE_COMMANDS: dict[str, Callable[[], CanonicalCommand]] = {
    "开局": CanonicalCommand.create,
    "加入": CanonicalCommand.join,
    "退出": CanonicalCommand.leave,
    "取消": CanonicalCommand.cancel,
    "开始": CanonicalCommand.start,
    "开枪": CanonicalCommand.shoot,
    "弃权": CanonicalCommand.forfeit,
    "结束": CanonicalCommand.end_turn,
    "装填": CanonicalCommand.reload,
    "道具": CanonicalCommand.open_item_panel,
}


def _syntax(code: str) -> CanonicalCommand:
    return CanonicalCommand.syntax_failure(code=code)


def _invalid_args(usage: str) -> CanonicalCommand:
    return _syntax(f"invalid_args:{usage}")


def _player_seq(token: str) -> int | None:
    """Strict ``[1-9][0-9]*`` player sequence: ASCII digits, no leading zero."""
    if not token.isascii() or not token.isdigit():
        return None
    if token[0] == "0":
        return None
    return int(token)


def _item_token(token: str) -> tuple[str, str]:
    """Split ``<letter><suffix>`` into (letter, suffix); empty when not a letter."""
    if not token:
        return ("", "")
    letter = token[0].upper()
    if letter not in ITEM_BY_LETTER:
        return ("", "")
    return (letter, token[1:])


def _parse_item_use(token: str) -> CanonicalCommand:
    letter, suffix = _item_token(token)
    if not letter:
        return _syntax("invalid_item_letter")
    if letter == "D":
        if not suffix:
            return _invalid_args(ITEM_USE_USAGE)
        seq = _player_seq(suffix)
        if seq is None:
            return _syntax("invalid_player_seq")
        return CanonicalCommand.use_item("lock", seq)
    if suffix:
        return _syntax("invalid_item_letter")
    return CanonicalCommand.use_item(ITEM_BY_LETTER[letter])


def _parse_item_discard(token: str) -> CanonicalCommand:
    letter, suffix = _item_token(token)
    if not letter or suffix:
        return _syntax("invalid_item_letter")
    return CanonicalCommand.discard_item(ITEM_BY_LETTER[letter])


def _parse_item(args: list[str]) -> CanonicalCommand:
    if not args:
        return CanonicalCommand.open_item_panel()
    verb, rest = args[0], args[1:]
    if verb == "使用":
        if len(rest) != 1:
            return _invalid_args(ITEM_USE_USAGE)
        return _parse_item_use(rest[0])
    if verb == "丢弃":
        if len(rest) != 1:
            return _invalid_args(ITEM_DISCARD_USAGE)
        return _parse_item_discard(rest[0])
    return _syntax("unknown_command")


def _parse_reward(args: list[str]) -> CanonicalCommand:  # noqa: PLR0911
    if not args:
        return _invalid_args(REWARD_USAGE)
    verb, rest = args[0], args[1:]
    if verb == "丢弃":
        if rest:
            return _invalid_args(REWARD_USAGE)
        return CanonicalCommand.choose_item(decision="discard")
    if verb == "替换":
        if len(rest) != 1:
            return _invalid_args(REWARD_USAGE)
        item = REWARD_NAME_TO_ITEM.get(rest[0])
        if item is None:
            return _syntax("invalid_reward_name")
        return CanonicalCommand.choose_item(decision="replace", replace_item=item)
    return _syntax("unknown_command")


def _parse_transfer(args: list[str]) -> CanonicalCommand:
    if len(args) != 1:
        return _invalid_args(TRANSFER_USAGE)
    seq = _player_seq(args[0])
    if seq is None:
        return _syntax("invalid_player_seq")
    return CanonicalCommand.transfer(target_player_seq=seq)


def parse_command(text: str) -> CanonicalCommand | None:  # noqa: PLR0911
    """Parse one message text into a canonical command.

    Returns ``None`` for anything that is not a ``/轮盘`` command so the
    handler can stay silent; syntax problems become typed ``syntax_failure``
    commands instead of exceptions.
    """
    normalized = " ".join(text.split())
    if not normalized.startswith(PREFIX):
        return None
    tail = normalized[len(PREFIX) :]
    if tail and not tail[0].isspace():
        return None
    rest = tail.strip()
    if not rest:
        return _syntax("invalid_args")
    parts = rest.split()
    sub, args = parts[0], parts[1:]
    if sub == "排行榜":
        if args:
            return _invalid_args(LEADERBOARD_USAGE)
        return CanonicalCommand.leaderboard()
    if sub == "道具":
        if not args:
            return CanonicalCommand.open_item_panel()
        return _parse_item(args)
    if sub == "奖励":
        return _parse_reward(args)
    if sub == "转让":
        return _parse_transfer(args)
    builder = _BARE_COMMANDS.get(sub)
    if builder is None:
        return _syntax("unknown_command")
    if args:
        # Recognized bare subcommands with extra arguments carry their own
        # static usage suffix; a bare ``invalid_args`` would fall through to
        # the generic system-error copy (TSK-266 11.1).
        return _invalid_args(sub)
    return builder()
