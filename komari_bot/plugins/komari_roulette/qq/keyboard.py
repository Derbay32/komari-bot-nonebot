"""QQ button keyboard seam (TSK-278).

``build_keyboard`` projects a frozen ``ReplyProjectionContext`` into the
canonical JSON spec string stored under ``metadata["keyboard"]``;
``keyboard_from_spec`` materializes that spec into a real QQ
``MessageKeyboard``.  Both are pure: no services, no random, no global state.
The canonical spec is always the object form ``{"rows": [...]}`` — there is no
historical bare-list fallback.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nonebot.adapters.qq.models import (
    Action,
    Button,
    InlineKeyboard,
    InlineKeyboardRow,
    MessageKeyboard,
    Permission,
    RenderData,
)

if TYPE_CHECKING:
    from ..command_service import ReplyPlayer, ReplyProjectionContext

# Canonical item order A→B→C→D and their stable Chinese labels.
ITEM_ORDER: tuple[str, ...] = ("magnifier", "beer", "burst", "lock")
ITEM_CN: dict[str, str] = {
    "magnifier": "放大镜",
    "beer": "啤酒",
    "burst": "连发器",
    "lock": "锁",
}
ITEM_LETTER: dict[str, str] = {
    "magnifier": "A",
    "beer": "B",
    "burst": "C",
    "lock": "D",
}

#: Result codes that carry a real non-terminal reply (buttons are possible).
NON_ERROR_RESULT_CODES: frozenset[str] = frozenset(
    {
        "created",
        "joined",
        "left",
        "cancelled",
        "started",
        "shot",
        "forfeited",
        "reloaded",
        "turn_ended",
        "item_used",
        "item_discarded",
        "panel_opened",
        "item_choice_pending",
        "item_choice_updated",
        "leaderboard",
        # A waiting host left and the earliest joiner took over: still a
        # normal waiting-game reply with buttons, not a fixed error.
        "host_transferred",
    }
)

#: The only accepted locked-phase spelling is TSK-276's ``locked_turn`` projection.
#: There is no historical short ``locked`` alias.
LOCKED_TURN_PHASE = "locked_turn"

TERMINAL_LIFECYCLES: frozenset[str] = frozenset({"completed", "cancelled", "expired"})


def is_error_result_code(result_code: str) -> bool:
    """Fixed-error replies (including unknown codes) carry no buttons."""
    return result_code not in NON_ERROR_RESULT_CODES


def _button(label: str, data: str) -> dict[str, Any]:
    return {
        "label": label,
        "data": data,
        "action_type": 2,
        "permission_type": 2,
        "reply": False,
        "enter": False,
    }


def eligible_lock_targets(context: ReplyProjectionContext) -> tuple[ReplyPlayer, ...]:
    """Legal lock targets: alive, not the actor, and not already pending a lock.

    ``ReplyGameView.pending_lock_players`` is the *exclusion* set (players that
    already carry a pending lock), never the eligible list.  The eligible set
    is recomputed from the frozen roster so both the keyboard and the panel
    renderer agree.
    """

    current_seq = (
        context.current_player.join_seq if context.current_player is not None else None
    )
    return tuple(
        sorted(
            (
                player
                for player in context.players
                if player.alive
                and player.join_seq != current_seq
                and not player.pending_lock
            ),
            key=lambda player: player.join_seq,
        )
    )


def _current_inventory(context: ReplyProjectionContext) -> dict[str, int]:
    """Positive item counts for the current player.

    The frozen production context resolves ``current_player`` from the roster,
    so the two copies carry identical inventories; taking the per-item maximum
    reconciles lighter projections without double counting.
    """

    current = context.current_player
    if current is None:
        return {}
    sources = [current]
    sources.extend(
        player for player in context.players if player.join_seq == current.join_seq
    )
    counts: dict[str, int] = {}
    for source in sources:
        for item, count in source.inventory_counts:
            if count > 0:
                counts[item] = max(counts.get(item, 0), count)
    return counts


def _has_usable_item(context: ReplyProjectionContext) -> bool:
    """Whether the authoritative post-submit state allows any ``使用`` action."""

    counts = _current_inventory(context)
    if any(counts.get(item, 0) > 0 for item in ("magnifier", "beer", "burst")):
        return True
    # A held lock is only usable while a legal target exists.
    return counts.get("lock", 0) > 0 and bool(eligible_lock_targets(context))


def _follow_up_rows(context: ReplyProjectionContext) -> list[list[dict[str, Any]]]:
    """Emit only the actions the post-submit authoritative state allows (1H)."""

    rows: list[list[dict[str, Any]]] = []
    if sum(_current_inventory(context).values()) > 0:
        item_row: list[dict[str, Any]] = []
        if _has_usable_item(context):
            item_row.append(_button("🧰使用", "/轮盘 道具 使用"))
        item_row.append(_button("🗑️丢弃", "/轮盘 道具 丢弃"))
        rows.append(item_row)
    action_row = [_button("🔫开枪", "/轮盘 开枪")]
    if context.chamber_remaining_total != 6:
        action_row.append(_button("🔄装填", "/轮盘 装填"))
    rows.append(action_row)
    rows.append([_button("⏹️结束", "/轮盘 结束"), _button("🏳️弃权", "/轮盘 弃权")])
    return rows


def _locked_rows() -> list[list[dict[str, Any]]]:
    return [[_button("🔫开枪", "/轮盘 开枪"), _button("🏳️弃权", "/轮盘 弃权")]]


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[offset : offset + size] for offset in range(0, len(items), size)]


def _item_choice_rows(context: ReplyProjectionContext) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = [
        [_button("🗑️丢弃新道具", "/轮盘 奖励 丢弃"), _button("🏳️弃权", "/轮盘 弃权")],
    ]
    holder = context.reward_player or context.current_player
    inventory: tuple[tuple[str, int], ...] = ()
    if holder is not None:
        for roster in context.players:
            if roster.join_seq == holder.join_seq:
                inventory = roster.inventory_counts
                break
    held_types = {raw_item for raw_item, _count in inventory}
    held = [item for item in ITEM_ORDER if item in held_types]
    rows.extend(
        [
            [
                _button(f"🔄{ITEM_CN[item]}", f"/轮盘 奖励 替换 {ITEM_CN[item]}")
                for item in chunk
            ]
            for chunk in _chunk(held, 3)
        ]
    )
    return rows


def _waiting_rows(context: ReplyProjectionContext) -> list[list[dict[str, Any]]]:
    rows: list[list[dict[str, Any]]] = [
        [_button("加入", "/轮盘 加入"), _button("开始", "/轮盘 开始")],
        [_button("退出", "/轮盘 退出"), _button("取消", "/轮盘 取消")],
    ]
    if len(context.players) > 1:
        rows.append([_button("🔄转让", "/轮盘 转让 ")])
    return rows


def _layout_rows(context: ReplyProjectionContext) -> list[list[dict[str, Any]]]:  # noqa: PLR0911
    if context.lifecycle in TERMINAL_LIFECYCLES:
        return []
    if context.lifecycle == "waiting":
        return _waiting_rows(context)
    if context.result_code == "leaderboard":
        return []
    if is_error_result_code(context.result_code):
        return []
    if context.phase == "item_choice":
        return _item_choice_rows(context)
    if context.phase == LOCKED_TURN_PHASE:
        return _locked_rows()
    return _follow_up_rows(context)


def build_keyboard(context: ReplyProjectionContext) -> str:
    """Serialize the frozen button layout as the canonical JSON spec string."""
    return json.dumps({"rows": _layout_rows(context)}, ensure_ascii=False, separators=(",", ":"))


def keyboard_from_spec(spec: str) -> MessageKeyboard:
    """Materialize a canonical JSON spec into a real QQ ``MessageKeyboard``.

    Only the object form ``{"rows": [[button, ...], ...]}`` is accepted: a bare
    list and the historical dict-row form ``{"buttons": [...]}`` are rejected.
    """

    parsed = json.loads(spec)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
        raise TypeError("keyboard spec must be an object with a rows list")  # noqa: TRY003
    rows: list[InlineKeyboardRow] = []
    for row_spec in parsed["rows"]:
        if not isinstance(row_spec, list):
            raise TypeError("each keyboard row must be a button list")  # noqa: TRY003
        buttons = [
            Button(
                render_data=RenderData(label=str(button_spec["label"])),
                action=Action(
                    type=int(button_spec.get("action_type", 2)),
                    permission=Permission(type=int(button_spec.get("permission_type", 2))),
                    data=str(button_spec["data"]),
                    reply=bool(button_spec.get("reply", False)),
                    enter=bool(button_spec.get("enter", False)),
                ),
            )
            for button_spec in row_spec
        ]
        rows.append(InlineKeyboardRow(buttons=buttons))
    return MessageKeyboard(content=InlineKeyboard(rows=rows))
