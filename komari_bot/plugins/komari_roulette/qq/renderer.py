# ruff: noqa: RUF001, RUF003  # ｜ ＝ × 是定稿文案字符；注释中的范围线非连字符
"""Full-Markdown reply renderer seam (TSK-278).

``render_reply`` projects a frozen ``ReplyProjectionContext`` into a
``ReplyProjection`` whose ``body`` follows the TSK-266 final copy and whose
``metadata`` carries the frozen keyboard spec plus the outbound mention pair.
Pure projection: no services, no random, no time.  Result sentences come from
a module-level copy pool (defaults deterministic; TSK-279 will inject
config-driven copy via ``set_sentence_pool``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..command_service import ReplyProjection
from .keyboard import (
    ITEM_CN,
    ITEM_LETTER,
    ITEM_ORDER,
    TERMINAL_LIFECYCLES,
    build_keyboard,
    eligible_lock_targets,
    is_error_result_code,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..command_service import ReplyPlayer, ReplyProjectionContext

# ---------------------------------------------------------------------------
# Copy pool (TSK-266 / TSK-269 文案；TSK-279 注入配置化副本)
# ---------------------------------------------------------------------------

DEFAULT_SENTENCE_POOL: dict[str, str] = {
    "shot": "{name}打出一发{kind}。",
    "started": "游戏开始，{name}先手。",
    "reloaded": "{name}装填了一发。",
    "turn_ended": "{name}结束了回合。",
    "forfeited": "{name}选择弃权。",
    "joined": "你已加入本局。",
    "created": "新局已创建。",
    "left": "你已退出本局。",
    "cancelled": "本局已取消。",
    "item_used": "道具已使用。",
    "item_discarded": "已丢弃道具。",
    "item_choice_pending": "你获得了新道具。",
    "item_choice_updated": "奖励选择已更新。",
    "lock_used": "{actor}对{target}（{tag}）使用了锁。",
    "final": "{eliminated_prefix}{winner}{winner_tag} 获胜，累计胜场 {wins}。",
}

_sentence_pool: dict[str, str] = dict(DEFAULT_SENTENCE_POOL)


def set_sentence_pool(pool: Mapping[str, str] | None) -> None:
    """Replace the result-sentence pool (TSK-279 copy injection seam)."""
    _sentence_pool.clear()
    _sentence_pool.update(pool if pool is not None else DEFAULT_SENTENCE_POOL)


def get_sentence_pool() -> dict[str, str]:
    """Current result-sentence pool, as a mutable copy."""
    return dict(_sentence_pool)


# ---------------------------------------------------------------------------
# Fixed error copy (TSK-266 11.1–11.5; 逐字定稿)
# ---------------------------------------------------------------------------

GENERIC_ERROR_TEXT = "游戏状态异常，本次操作未执行。请联系管理员。"

#: 1E locked-turn restriction line; also the fixed ``locked_turn_restriction``
#: error copy (kept in one place so both paths cannot drift).
LOCKED_TURN_RESTRICTION_TEXT = "你本回合受到锁限制，只能执行一次开枪命令或弃权。"

#: 1B pending-burst hint.  It is shown only while ``pending_burst`` is true; the
#: neutral ``手枪：普通`` line does not exist.
PENDING_BURST_HINT = "手枪：下一次开枪连发"

#: Static usage placeholder for the defensive bare ``/轮盘`` syntax failure.  The
#: renderer never echoes the raw input here.
BARE_INVALID_ARGS_USAGE = "<子命令>"

#: TSK-266 11.3: leaving with "退出" after the game started gets its own copy.
LEAVE_AFTER_START_TEXT = (
    "游戏已经开始，“退出”只用于等候阶段；主动离开请使用 @Bot /轮盘 弃权。"
)

_ERROR_TEXTS: dict[str, str] = {
    "unknown_command": "无法识别这条轮盘命令。发送 .docs 轮盘 查看使用说明。",
    "invalid_player_seq": "玩家编号格式不正确。请填写不带前导零的正整数。",
    "player_seq_not_found": "当前游戏中不存在这个玩家编号，请检查目标编号后重新操作。",
    "invalid_item_letter": "道具字母只能是：A＝放大镜、B＝啤酒、C＝连发器、D＝锁。",
    "invalid_reward_name": "替换的道具名称只能是：放大镜、啤酒、连发器或锁。",
    "game_already_exists": "本群已有一局俄罗斯轮盘，暂时不能创建新局。",
    "no_waiting_game": "本群没有等待开始的俄罗斯轮盘。",
    "already_joined": "你已经加入当前游戏。",
    "game_full": "当前游戏已满员（6/6）。",
    "game_already_started": "游戏已经开始，无法再改变等候阵容。",
    "not_joined": "你尚未加入当前等候局。",
    "not_host": "只有当前局主可以执行这个操作。",
    "not_enough_players": "至少需要 2 名玩家才能开始游戏。",
    "waiting_game_expired": "这局游戏等待太久仍未开始，现已自动结束。",
    "no_active_game": "本群没有进行中的俄罗斯轮盘。",
    "game_completed": "这局俄罗斯轮盘已经结束。",
    "not_participant": "你不是当前游戏的参与者。",
    "player_eliminated": "你已经出局，不能再操作这局游戏。",
    "not_current_player": "现在不是你的回合。",
    "turn_expired": "你的行动时间已经结束，本次命令未执行。",
    "state_conflict": "局面刚刚发生变化，本次操作未执行。请根据机器人最新回复重新操作。",
    "action_not_allowed_in_phase": "当前阶段不能执行这个操作。",
    "locked_turn_restriction": LOCKED_TURN_RESTRICTION_TEXT,
    "invalid_game_state": GENERIC_ERROR_TEXT,
    "chamber_not_ready": "弹仓尚未就绪，本次操作未执行。",
    "chamber_full": "弹仓已满，不能装填。",
    "invalid_chamber_state": "弹仓状态异常，本次操作未执行。",
    "random_source_failed": "随机结果生成失败，本次操作未执行。请重新发送命令。",
    "item_not_owned": "你没有这件道具。",
    "no_pending_item_choice": "当前没有需要处理的道具奖励。",
}

_ERROR_REASON_TEXTS: dict[tuple[str, str], str] = {
    ("invalid_transfer_target", "self"): "不能把局主转让给自己。",
    ("action_not_allowed_in_phase", "item_choice_pending"): "请先处理当前新道具；现在只能丢弃奖励、替换道具或弃权。",
    ("invalid_item_target", "self"): "锁不能对自己使用。",
    ("invalid_item_target", "eliminated"): "目标玩家已经出局。",
    ("item_effect_conflict", "burst_already_pending"): "手枪已经带有连发效果，不能重复使用连发器。",
    ("item_effect_conflict", "target_already_locked"): "目标已经有一把待生效的锁。",
    # TSK-266 11.5 pins ``insufficient_chamber_for_burst``; the real TSK-272
    # domain reason string is ``burst_requires_two_rounds`` (same copy).
    ("item_precondition_failed", "insufficient_chamber_for_burst"): "弹仓至少需要剩余 2 发才能使用连发器。",
    ("item_precondition_failed", "burst_requires_two_rounds"): "弹仓至少需要剩余 2 发才能使用连发器。",
    ("item_precondition_failed", "beer_blocked_by_burst"): "手枪处于待连发状态，不能使用啤酒。",
}

_KIND_CN: dict[str, str] = {"blank": "空弹", "live": "实弹"}
_ELIMINATED_REASON_CN: dict[str, str] = {
    "shot": "打出实弹出局",
    "forfeit": "弃权出局",
    "timeout": "超时出局",
}

#: mention_reason values whose tag sits right after ``**当前：{冻结名}**``.
_CURRENT_POSITION_REASONS: frozenset[str] = frozenset({"turn", "reward"})


def _fixed_error_text(context: ReplyProjectionContext) -> str:
    reason = context.details.get("reason")
    if isinstance(reason, str):
        key = (context.result_code, reason)
        if key in _ERROR_REASON_TEXTS:
            return _ERROR_REASON_TEXTS[key]
    if context.result_code == "invalid_args":
        # A bare ``/轮盘`` must render the static usage copy, never the generic
        # system error (TSK-266 11.1 defensive branch).
        return f"命令参数不正确。正确用法：@Bot /轮盘 {BARE_INVALID_ARGS_USAGE}"
    if context.result_code.startswith("invalid_args:"):
        usage = context.result_code[len("invalid_args:") :]
        return f"命令参数不正确。正确用法：@Bot /轮盘 {usage}"
    return _ERROR_TEXTS.get(context.result_code, GENERIC_ERROR_TEXT)


# ---------------------------------------------------------------------------
# Markdown building blocks
# ---------------------------------------------------------------------------


def _escape_name(name: str) -> str:
    """Freeze a display name as plain text (no Markdown/XML injection)."""
    escaped = name
    for char, replacement in (
        ("\\", "\\\\"),
        ("[", "\\["),
        ("]", "\\]"),
        ("*", "\\*"),
        ("_", "\\_"),
        ("`", "\\`"),
    ):
        escaped = escaped.replace(char, replacement)
    return escaped.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mention_tag(target: ReplyPlayer) -> str:
    return f'<qqbot-at-user id="{target.member_openid}" />'


def _roster_inventory(
    context: ReplyProjectionContext,
    join_seq: int,
) -> tuple[tuple[str, int], ...]:
    for player in context.players:
        if player.join_seq == join_seq:
            return player.inventory_counts
    return ()


def _roster_lines(
    context: ReplyProjectionContext,
    *,
    current_seq: int | None,
) -> list[str]:
    lines: list[str] = []
    for player in context.players:
        if player.alive:
            status = "当前" if player.join_seq == current_seq else "存活"
        else:
            status = "出局"
        name = _escape_name(player.display_name)
        total = sum(count for _item, count in player.inventory_counts)
        row = f"- {name}｜{status}｜道具 {total}"
        if player.pending_lock:
            row += "｜待锁"
        if player.join_seq == current_seq:
            row = row.replace(f"- {name}", f"- **{name}**", 1)
        lines.append(row)
    return lines


def _chamber_line(view: Any) -> str:
    probability = view.hit_probability_percent
    probability_text = f"{probability:.0f}" if probability is not None else "--"
    return (
        f"弹仓：**{view.chamber_remaining_total}/6**｜实弹 **{view.chamber_remaining_live}**"
        f"｜空弹 **{view.chamber_remaining_blank}**｜中弹概率 **{probability_text}%**"
    )


def _current_line(context: ReplyProjectionContext) -> str:
    if context.current_player is None:
        return ""
    line = f"**当前：{_escape_name(context.current_player.display_name)}**"
    if (
        context.mention_target is not None
        and context.mention_reason in _CURRENT_POSITION_REASONS
    ):
        line += f" {_mention_tag(context.mention_target)}"
    return line


def _actor_player(context: ReplyProjectionContext) -> ReplyPlayer | None:
    """Resolve the frozen seat that commanded the action (the actor).

    Rotation results (``end_turn`` / ``forfeit``) move the current player to
    the next seat, so a result sentence must name the actor, never
    ``current_player``.
    """

    actor = context.actor_member_openid
    if actor:
        for player in context.players:
            if player.member_openid == actor:
                return player
    return None


def _sentence_name(context: ReplyProjectionContext) -> str:
    """The actor's frozen display name, falling back to the current player."""

    actor = _actor_player(context)
    if actor is not None:
        return _escape_name(actor.display_name)
    if context.current_player is not None:
        return _escape_name(context.current_player.display_name)
    return ""


def _sentence(key: str, **kwargs: object) -> str:
    template = _sentence_pool.get(key, DEFAULT_SENTENCE_POOL.get(key, ""))
    if not template:
        return ""
    return template.format(**kwargs)


def _follow_up_body(context: ReplyProjectionContext) -> str:
    details = context.details
    kind = _KIND_CN.get(str(details.get("consumed_kind", "")), "空弹")
    sentence = _sentence(context.result_code, name=_sentence_name(context), kind=kind)
    current_seq = context.current_player.join_seq if context.current_player else None
    lines = [
        f"> {sentence}",
        "",
        _current_line(context),
        _chamber_line(context.game_view),
    ]
    if context.phase == "locked_turn":
        lines.append(LOCKED_TURN_RESTRICTION_TEXT)
    if context.pending_burst:
        lines.append(PENDING_BURST_HINT)
    lines += ["", "***", "", *_roster_lines(context, current_seq=current_seq)]
    return "\n".join(lines)


def _reward_choice_body(context: ReplyProjectionContext) -> str:
    details = context.details
    reward_player = context.reward_player or context.current_player
    reward_seq = reward_player.join_seq if reward_player is not None else None
    inventory = _roster_inventory(context, reward_seq) if reward_seq is not None else ()
    # The reward phase only exists while the inventory is full (TSK-272 mapper
    # invariant), and TSK-266 6a9bf4b7 always shows the replacement prompt.
    # The reward-phase first line is the same blank-shot sentence frozen for the
    # shot that produced the reward, never the generic "you got a new item".
    lines: list[str] = [f"> {_reward_sentence(context)}", ""]
    if details.get("inventory_full", True):
        lines.append("道具列表已满，选择一项来替换。")
        lines.append("")
    pending_item = details.get("pending_item")
    if pending_item is not None:
        item_name = ITEM_CN.get(str(pending_item), str(pending_item))
        lines.append(f"当前新道具：**{item_name}**")
    held = [
        f"{ITEM_CN[item]} ×{count}"
        for item, count in _inventory_in_order(inventory)
    ]
    if held:
        lines.append(f"已有道具：{'、'.join(held)}")
    pending_count = _pending_reward_count(context)
    if pending_count > 0:
        lines.append(f"后续待处理奖励：**{pending_count} 件**")
    lines += [
        "",
        "***",
        "",
        _current_line(context),
        _chamber_line(context.game_view),
        "",
        "***",
        "",
    ]
    current_seq = context.current_player.join_seq if context.current_player else None
    lines += _roster_lines(context, current_seq=current_seq)
    return "\n".join(lines)


def _reward_sentence(context: ReplyProjectionContext) -> str:
    """First line of the reward-choice reply: the frozen shot sentence."""

    kind = _KIND_CN.get(str(context.details.get("consumed_kind", "")))
    if kind is not None:
        return _sentence("shot", name=_sentence_name(context), kind=kind)
    return _sentence(context.result_code) or _sentence("item_choice_pending")


def _inventory_in_order(
    inventory: tuple[Any, ...],
) -> list[tuple[str, int]]:
    """Order inventory entries A→B→C→D, accepting both frozen encodings.

    TSK-276 ``_safe_details`` freezes a mapping as ``"item:count"`` strings
    (the real production shape); 2-tuples stay accepted for direct projections.
    """

    counts: dict[str, int] = {}
    for raw in inventory:
        if isinstance(raw, tuple) and len(raw) == 2:
            counts[str(raw[0])] = int(raw[1])
        elif isinstance(raw, str):
            item, separator, count_text = raw.rpartition(":")
            if separator and item and count_text.isdigit():
                counts[item] = int(count_text)
    return [(item, counts[item]) for item in ITEM_ORDER if item in counts]


def _pending_reward_count(context: ReplyProjectionContext) -> int:
    """Rewards still queued *after* the current new item (TSK-272 semantics)."""

    count = context.details.get("pending_item_count")
    if isinstance(count, bool) or not isinstance(count, int):
        return context.pending_reward_count
    return count


def _lock_body(context: ReplyProjectionContext) -> str:
    actor = context.current_player
    target = context.lock_target_player
    actor_name = _escape_name(actor.display_name) if actor is not None else ""
    target_name = _escape_name(target.display_name) if target is not None else ""
    tag = ""
    if context.mention_target is not None and context.mention_reason == "lock_target":
        tag = _mention_tag(context.mention_target)
    sentence = _sentence(
        "lock_used",
        actor=actor_name,
        target=target_name,
        tag=tag,
    )
    current_seq = actor.join_seq if actor is not None else None
    return "\n".join(
        [
            f"> {sentence}",
            "",
            _current_line(context),
            _chamber_line(context.game_view),
            "",
            "***",
            "",
            *_roster_lines(context, current_seq=current_seq),
        ]
    )


def _final_outcome_player(context: ReplyProjectionContext) -> ReplyPlayer | None:
    """The seat the final reply is about: the eliminated actor when known.

    A committed terminal elimination (live shot / forfeit / timeout) is always
    commanded by the player who leaves the game, so the commanding member's
    seat is the eliminated one.  Matching the internal sending id is not
    interpolation: the id never reaches the body.
    """

    actor = context.actor_member_openid
    if actor:
        for player in context.players:
            if (
                player.member_openid
                and player.member_openid == actor
                and not player.alive
            ):
                return player
    return next((player for player in context.players if not player.alive), None)


def _final_body(context: ReplyProjectionContext) -> str:
    winner = context.winner
    winner_name = _escape_name(winner.display_name) if winner is not None else ""
    tag = ""
    if context.mention_target is not None and context.mention_reason == "winner":
        tag = _mention_tag(context.mention_target)
    winner_tag = f" {tag}" if tag else ""
    eliminated_prefix = ""
    eliminated = _final_outcome_player(context)
    if eliminated is not None:
        reason = context.details.get("eliminated_reason")
        if reason is None:
            reason = context.details.get("completion_reason")
        reason_text = _ELIMINATED_REASON_CN.get(str(reason), "出局")
        eliminated_prefix = f"{_escape_name(eliminated.display_name)}{reason_text}，"
    wins = context.winner_group_wins
    return _sentence(
        "final",
        eliminated_prefix=eliminated_prefix,
        winner=winner_name,
        winner_tag=winner_tag,
        wins=wins if wins is not None else 0,
    )


def _leaderboard_body(context: ReplyProjectionContext) -> str:
    entries = context.details.get("leaderboard")
    if not isinstance(entries, tuple):
        entries = ()
    if not entries:
        return "本群还没有俄罗斯轮盘胜者。"
    parsed: list[tuple[str, int]] = []
    for entry in entries:
        name, wins_text = str(entry).rsplit(":", 1)
        parsed.append((name, int(wins_text)))
    lines = ["**本群俄罗斯轮盘排行榜｜前 10 名**"]
    for rank, (name, wins) in enumerate(parsed[:10], start=1):
        lines.append(f"{rank}. {_escape_name(name)}｜{wins} 胜")
    lines.append(f"共有 {len(parsed)} 名玩家取得过胜利。")
    return "\n".join(lines)


def _panel_body(context: ReplyProjectionContext) -> str:
    current = context.current_player
    current_name = _escape_name(current.display_name) if current is not None else ""
    lines = [f"**{current_name}的道具**"]
    inventory = context.details.get("inventory")
    if not isinstance(inventory, tuple):
        inventory = ()
    ordered = _inventory_in_order(inventory)
    for item, count in ordered:
        lines.append(f"- {ITEM_LETTER[item]}｜{ITEM_CN[item]} ×{count}")
    held = dict(ordered)
    if held.get("lock", 0) > 0:
        # The lock region lists only legal targets: alive, not self, and not
        # already pending a lock.  ``game_view.pending_lock_players`` is the
        # exclusion set and must never be rendered as the target list.
        lines += ["", "**可上锁的玩家**"]
        targets = eligible_lock_targets(context)
        if targets:
            for player in targets:
                lines.append(f"- {player.join_seq}｜{_escape_name(player.display_name)}")
            lines += ["", "使用锁时，在“使用”后补上 D 和目标编号。"]
        else:
            lines.append("当前没有可上锁的玩家。")
    return "\n".join(lines)


def _waiting_body(context: ReplyProjectionContext) -> str:
    view = context.game_view
    host_seq = view.host_seq if view is not None else None
    capacity_value = context.details.get("capacity", 6)
    capacity = capacity_value if isinstance(capacity_value, int) else 6
    lines = [
        "**俄罗斯轮盘 · 等候中**",
        f"在席 {len(context.players)}/{capacity} 人",
        "",
    ]
    for player in context.players:
        row = f"- {_escape_name(player.display_name)}"
        if player.join_seq == host_seq:
            row += "｜局主"
        lines.append(row)
    candidates = [p for p in context.players if p.join_seq != host_seq]
    if candidates:
        lines += ["", "**可转让给**"]
        for player in sorted(candidates, key=lambda p: p.join_seq):
            lines.append(f"- {player.join_seq}｜{_escape_name(player.display_name)}")
        lines += ["", "仅局主可转让，使用 /轮盘 转让 玩家编号。"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public seam
# ---------------------------------------------------------------------------


def _rotation_body(context: ReplyProjectionContext) -> str:
    """The authoritative board after a committed rotation (TSK-266 11.3).

    Used when an active timeout eliminated the current player but the game
    continues: the fixed timeout notice is followed by this board instead of
    the ordinary action sentence (there is no action result to quote).
    """

    current_seq = context.current_player.join_seq if context.current_player else None
    return "\n".join(
        [
            _current_line(context),
            _chamber_line(context.game_view),
            "",
            "***",
            "",
            *_roster_lines(context, current_seq=current_seq),
        ]
    )


def _waiting_end_body(context: ReplyProjectionContext) -> str:
    """TSK-266 1G: name whoever ended a waiting game, without the number."""

    reason = context.details.get("waiting_end_reason")
    name = context.details.get("waiting_end_actor_name")
    if isinstance(name, str) and name:
        if reason == "host_cancelled":
            return f"{_escape_name(name)}取消了这局游戏，等候中的玩家已经全部离席。"
        if reason == "last_player_left":
            return f"{_escape_name(name)}离开后，等候局已自动结束。"
    return _sentence("cancelled")


# ---------------------------------------------------------------------------
# Public seam
# ---------------------------------------------------------------------------


def render_reply(context: ReplyProjectionContext) -> ReplyProjection:  # noqa: PLR0911
    """Project a frozen context into the full Markdown reply projection."""
    if context.result_code == "leaderboard":
        body = _leaderboard_body(context)
        return _project(context, body, allow_mention=False)
    if context.result_code == "turn_expired":
        # TSK-266 11.3: the timeout elimination is already committed, so one
        # reply carries the fixed timeout notice followed by the final outcome
        # (terminal) or the next authoritative board (still active).
        fixed = _fixed_error_text(context)
        if context.lifecycle == "completed":
            return _project(
                context,
                f"{fixed}\n\n{_final_body(context)}",
                allow_mention=True,
            )
        return _project(
            context,
            f"{fixed}\n\n{_rotation_body(context)}",
            allow_mention=True,
        )
    if context.lifecycle == "cancelled" and context.result_code == "cancelled":
        # TSK-266 1G: the waiting game ended without a winner.
        return _project(context, _waiting_end_body(context), allow_mention=False)
    if context.result_code == "game_already_started" and context.intent == "leave":
        # TSK-266 11.3: "退出" after start has its own copy; other waiting-roster
        # commands with the same code keep the generic copy below.
        return _project(context, LEAVE_AFTER_START_TEXT, allow_mention=False)
    if is_error_result_code(context.result_code):
        return _project(context, _fixed_error_text(context), allow_mention=False)
    if context.lifecycle == "completed":
        return _project(context, _final_body(context), allow_mention=True)
    if context.lifecycle in TERMINAL_LIFECYCLES:
        return _project(context, _fixed_error_text(context), allow_mention=False)
    if context.phase == "item_choice":
        return _project(context, _reward_choice_body(context), allow_mention=True)
    if context.result_code == "panel_opened":
        return _project(context, _panel_body(context), allow_mention=True)
    if context.lifecycle == "waiting":
        return _project(context, _waiting_body(context), allow_mention=True)
    if context.result_code == "item_used" and context.details.get("item") == "lock":
        return _project(context, _lock_body(context), allow_mention=True)
    return _project(context, _follow_up_body(context), allow_mention=True)


def _project(
    context: ReplyProjectionContext,
    body: str,
    *,
    allow_mention: bool,
) -> ReplyProjection:
    metadata: dict[str, Any] = {"keyboard": build_keyboard(context)}
    if allow_mention and context.mention_target is not None:
        metadata["mention_member_openid"] = context.mention_target.member_openid
        metadata["mention_display_name"] = context.mention_target.display_name
    return ReplyProjection(body=body, metadata=metadata)
