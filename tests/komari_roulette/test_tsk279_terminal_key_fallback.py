"""TSK-279: the terminal reason is a closed, mandatory projection input.

The Stage-A acceptance reaches the three closed terminal copy keys
(``shot`` / ``forfeit`` / ``timeout``) from **real** domain outcomes
(``tests/komari_roulette/test_tsk279_stage_a_pg.py``).  This pure module pins
the projector-side contract those real outcomes depend on, without pretending
the old fallback is acceptable:

* a frozen terminal projection must carry exactly one closed terminal reason;
* a projection that carries no reason, an unknown reason, or two mutually
  incompatible closed reasons is rejected with ``ValueError`` **before** any
  copy is drawn -- the renderer never guesses a ``shot`` terminal out of
  nothing and never fabricates an elimination event;
* the rejection message is safe: it never echoes the raw receipt details;
* the three real reasons still select their own closed key.

Guessing is not the fallback: the three-branch PostgreSQL acceptance remains
the authority for the domain -> projector wiring, and this module only pins the
projector-side contract.  The TSK-278 pseudo-contexts that omit the reason are
listed in ``TSK-279-contract.md`` §6 and are owned by their own ticket; they are
deliberately not edited here.
"""

from __future__ import annotations

from typing import Any

import pytest

from komari_bot.plugins.komari_roulette.copy_pool import (
    DEFAULT_ACTION_COPY_POOL,
    compile_copy_pool,
)
from komari_bot.plugins.komari_roulette.qq.renderer import (
    build_reply_projector,
    render_reply,
)

from .tsk278_support import context, game_view, player
from .tsk279_support import ScriptedCopyRandom

FINAL_MARKERS: dict[str, str] = {
    "shot": "[FINAL-shot]{event}{winner} {wins}。",
    "forfeit": "[FINAL-forfeit]{event}{winner} {wins}。",
    "timeout": "[FINAL-timeout]{event}{winner} {wins}。",
}

#: The frozen event copy ``_ELIMINATED_REASON_CN`` renders per closed reason.
REASON_EVENT_TEXT: dict[str, str] = {
    "shot": "打出实弹出局",
    "forfeit": "弃权出局",
    "timeout": "超时出局",
}

#: A value that is not a closed terminal reason.  If the renderer ever echoed
#: it, the rejection message would leak receipt details.
UNKNOWN_REASON = "leaky-unknown-reason-<b>"


def _projector(rng: Any) -> Any:
    snapshot = compile_copy_pool(
        action_copy_pool={
            key: list(value) for key, value in DEFAULT_ACTION_COPY_POOL.items()
        },
        final_copy_pool={key: [value] for key, value in FINAL_MARKERS.items()},
    )
    return build_reply_projector(snapshot=snapshot, random_source=rng)


def _completed_context(details: dict[str, Any]) -> Any:
    return context(
        result_code="forfeited",
        lifecycle="completed",
        phase=None,
        details=dict(details),
        players=(player(1, name="少年A", alive=False), player(2, name="小红")),
        winner=player(2, name="小红"),
        winner_group_wins=1,
        mention_target=player(2, name="小红"),
        mention_reason="winner",
        view=game_view(
            remaining_total=0, remaining_live=0, remaining_blank=0, hit_percent=0.0
        ),
    )


# ---------------------------------------------------------------------------
# Missing / unknown / contradictory reasons are rejected, not guessed
# ---------------------------------------------------------------------------

REJECTED_TERMINAL_DETAILS: list[dict[str, Any]] = [
    {},
    {"eliminated_reason": UNKNOWN_REASON},
    {"eliminated_reason": "forfeit", "completion_reason": UNKNOWN_REASON},
    {"eliminated_reason": "shot", "completion_reason": "forfeit"},
]


@pytest.mark.parametrize(
    "details",
    REJECTED_TERMINAL_DETAILS,
    ids=["missing", "unknown", "unknown-secondary", "contradictory"],
)
def test_terminal_reason_is_rejected_without_drawing_copy(
    details: dict[str, Any],
) -> None:
    """No closed reason => ``ValueError`` and no template is ever drawn."""

    rng = ScriptedCopyRandom()
    with pytest.raises(ValueError):
        _projector(rng)(_completed_context(details))
    assert not rng.calls, "a rejected projection must not draw any copy"


def test_rejection_message_does_not_echo_raw_details() -> None:
    """The refusal is a safe fixed message, never a receipt dump."""

    rng = ScriptedCopyRandom()
    with pytest.raises(ValueError) as excinfo:
        _projector(rng)(_completed_context({"eliminated_reason": UNKNOWN_REASON}))
    message = str(excinfo.value)
    assert message.strip(), "rejection must carry a non-empty safe message"
    assert UNKNOWN_REASON not in message
    assert not rng.calls


def test_default_projection_rejects_missing_reason() -> None:
    """The public ``render_reply`` seam has no reason-less terminal either."""

    with pytest.raises(ValueError):
        render_reply(_completed_context({}))


# ---------------------------------------------------------------------------
# The three real reasons still select their own closed key
# ---------------------------------------------------------------------------

VALID_TERMINAL_DETAILS: list[tuple[str, dict[str, Any]]] = [
    # ``shot``: the live-shot terminal stamps only ``completion_reason``.
    ("shot", {"completion_reason": "shot", "winner_seq": 2}),
    # ``forfeit`` / ``timeout``: ``_eliminate_current`` stamps both fields equal.
    (
        "forfeit",
        {
            "completion_reason": "forfeit",
            "eliminated_reason": "forfeit",
            "winner_seq": 2,
        },
    ),
    (
        "timeout",
        {
            "completion_reason": "timeout",
            "eliminated_reason": "timeout",
            "winner_seq": 2,
        },
    ),
]


@pytest.mark.parametrize(
    ("reason", "details"),
    VALID_TERMINAL_DETAILS,
    ids=["shot", "forfeit", "timeout"],
)
def test_closed_terminal_reason_selects_its_own_key(
    reason: str,
    details: dict[str, Any],
) -> None:
    rng = ScriptedCopyRandom()
    rendered = _projector(rng)(_completed_context(details))
    body = rendered.body

    assert f"[FINAL-{reason}]" in body
    for other in FINAL_MARKERS:
        if other != reason:
            assert f"[FINAL-{other}]" not in body
    assert f"少年A{REASON_EVENT_TEXT[reason]}，" in body
    assert "小红" in body
    assert rng.draw_count == 1
