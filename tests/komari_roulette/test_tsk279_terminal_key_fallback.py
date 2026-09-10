"""TSK-279: pin the renderer's missing-reason terminal fallback.

The Stage-A acceptance requires the three closed terminal copy keys
(``shot`` / ``forfeit`` / ``timeout``) to be reached from **real** domain
outcomes (``tests/komari_roulette/test_tsk279_stage_a_pg.py``).  This pure
module records the *non-conforming* fallback the renderer still applies when a
projection carries neither ``eliminated_reason`` nor ``completion_reason``: it
renders ``DEFAULT_FINAL_COPY_KEY`` (``"shot"``).

Pinning it here keeps the fallback visible so it cannot be mistaken for a valid
terminal outcome, and so the real three-branch coverage above stays honest.
"""

from __future__ import annotations

from typing import Any

from komari_bot.plugins.komari_roulette.copy_pool import (
    DEFAULT_ACTION_COPY_POOL,
    compile_copy_pool,
)
from komari_bot.plugins.komari_roulette.qq.renderer import (
    DEFAULT_FINAL_COPY_KEY,
    build_reply_projector,
)

from .tsk278_support import context, game_view, player
from .tsk279_support import ScriptedCopyRandom

FINAL_MARKERS: dict[str, str] = {
    "shot": "[FINAL-shot]{event}{winner} {wins}。",
    "forfeit": "[FINAL-forfeit]{event}{winner} {wins}。",
    "timeout": "[FINAL-timeout]{event}{winner} {wins}。",
}


def _projector() -> Any:
    snapshot = compile_copy_pool(
        action_copy_pool={key: list(value) for key, value in DEFAULT_ACTION_COPY_POOL.items()},
        final_copy_pool={key: [value] for key, value in FINAL_MARKERS.items()},
    )
    return build_reply_projector(snapshot=snapshot, random_source=ScriptedCopyRandom())


def _completed_context(details: dict[str, Any]) -> Any:
    return context(
        result_code="forfeited",
        lifecycle="completed",
        phase=None,
        details=details,
        players=(player(1, name="少年A", alive=False), player(2, name="小红")),
        winner=player(2, name="小红"),
        winner_group_wins=1,
        mention_target=player(2, name="小红"),
        mention_reason="winner",
        view=game_view(remaining_total=0, remaining_live=0, remaining_blank=0, hit_percent=0.0),
    )


def test_missing_reason_falls_back_to_default_final_key() -> None:
    """Documented, non-conforming fallback: no reason ⇒ ``shot`` slot."""

    assert DEFAULT_FINAL_COPY_KEY == "shot"
    rendered = _projector()(_completed_context({}))
    assert "[FINAL-shot]" in rendered.body
    assert "[FINAL-forfeit]" not in rendered.body
    assert "[FINAL-timeout]" not in rendered.body
    # The unknown reason also degrades the event copy to the generic "出局".
    assert "少年A出局，" in rendered.body


def test_present_reason_overrides_fallback() -> None:
    """A real domain reason selects its own closed key, never the fallback."""

    forfeit = _projector()(_completed_context({"eliminated_reason": "forfeit"}))
    assert "[FINAL-forfeit]" in forfeit.body
    assert "少年A弃权出局，" in forfeit.body

    timeout = _projector()(_completed_context({"eliminated_reason": "timeout"}))
    assert "[FINAL-timeout]" in timeout.body
    assert "少年A超时出局，" in timeout.body

    shot = _projector()(_completed_context({"eliminated_reason": "shot"}))
    assert "[FINAL-shot]" in shot.body
    assert "少年A打出实弹出局，" in shot.body
