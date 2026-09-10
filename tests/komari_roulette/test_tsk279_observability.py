"""TSK-279 Stage-B: safe observability projection and fault redaction.

GREEN probe: the existing ``_safe_details`` allow-list already drops every
identity / body / secret / hidden-chamber / future-reward key from reply
details, so the new observability layer can reuse that discipline.

RED: the missing ``komari_bot.plugins.komari_roulette.observability`` seam must
expose a fixed-key, low-cardinality projection with closed reason codes, a
bounded pending count, and a fault redactor that never leaks a malicious
exception's message.  Those cases fail with ``ModuleNotFoundError``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from .command_support import PG_REQUIRED
from .tsk279_support import (
    OBSERVABILITY_MODULE,
    Tsk279Harness,
    harness_fixture_body,
    load_module,
    load_symbol,
    seed_aged_receipt,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_LEAKY_TOKENS = (
    "SECRET-OPENID-123",
    "SECRET-BODY-456",
    "SECRET-KEY-789",
    "SECRET-CHAMBER",
    "SECRET-REWARD",
)


def _observability_api() -> dict[str, Any]:
    return {
        name: load_symbol(OBSERVABILITY_MODULE, name)
        for name in (
            "OBSERVATION_REASON_CODES",
            "RouletteObservation",
            "RouletteObservability",
            "safe_fault_projection",
        )
    }


@pytest.fixture
async def harness() -> AsyncIterator[Tsk279Harness]:
    async for current in harness_fixture_body():
        yield current


def _leaky_round_message() -> str:
    return (
        f"member_openid={_LEAKY_TOKENS[0]} body={_LEAKY_TOKENS[1]} "
        f"api_key={_LEAKY_TOKENS[2]} chamber={_LEAKY_TOKENS[3]} "
        f"reward={_LEAKY_TOKENS[4]}\nline-2\nline-3"
    )


# ---------------------------------------------------------------------------
# GREEN probe: the real reply-detail redactor already drops the sensitive keys
# ---------------------------------------------------------------------------


def test_existing_reply_details_redaction_drops_hidden_and_secret_keys() -> None:
    service_module = load_module("komari_bot.plugins.komari_roulette.command_service")
    redact = service_module._safe_details
    safe = redact(
        {
            "member_openid": _LEAKY_TOKENS[0],
            "raw_message": _LEAKY_TOKENS[1],
            "api_key": _LEAKY_TOKENS[2],
            "ordered_chamber": (_LEAKY_TOKENS[3], _LEAKY_TOKENS[3]),
            "future_rewards": (_LEAKY_TOKENS[4],),
            "reason": "forfeited",
            "remaining_live": 1,
            "rewards": ("magnifier",),
        }
    )
    assert set(safe) == {"reason", "remaining_live", "rewards"}
    rendered = json.dumps(safe, ensure_ascii=False)
    for token in _LEAKY_TOKENS:
        assert token not in rendered


# ---------------------------------------------------------------------------
# RED: fixed-key projection, closed reason codes and bounded pending count
# ---------------------------------------------------------------------------


def test_observability_seam_exposes_fixed_projection() -> None:
    api = _observability_api()
    assert api["RouletteObservation"] is not None
    assert api["OBSERVATION_REASON_CODES"]
    assert isinstance(api["OBSERVATION_REASON_CODES"], frozenset)


def test_fault_projection_strips_a_malicious_exception() -> None:
    api = _observability_api()
    error = RuntimeError(_leaky_round_message())
    projection = api["safe_fault_projection"](error)
    rendered = json.dumps(projection, ensure_ascii=False)
    for token in _LEAKY_TOKENS:
        assert token not in rendered
    assert projection["error_type"] == "RuntimeError"
    assert projection["reason_code"] in api["OBSERVATION_REASON_CODES"]


def test_fault_projection_collapses_multiline_payload_to_one_record() -> None:
    api = _observability_api()
    observability = api["RouletteObservability"]()
    for _ in range(2):
        observability.note_fault(RuntimeError(_leaky_round_message()))
    observation = observability.snapshot()
    # One aggregated record per fixed reason, never one record per log line.
    assert sum(count for _reason, count in observation.fault_counts) == 2
    assert "\n" not in json.dumps(observation.as_dict(), ensure_ascii=False)
    assert len(observation.as_dict()) == len(api["RouletteObservation"].FIELDS)


@PG_REQUIRED
async def test_pending_count_reflects_real_receipts(
    harness: Tsk279Harness,
) -> None:
    api = _observability_api()
    async with harness.scope("observe-pending") as current:
        for index in range(2):
            await seed_aged_receipt(
                harness.session_factory,
                current,
                inbound_msg_id=f"observe-{index}-{uuid4().hex}",
                age_seconds=30,
            )
        observability = api["RouletteObservability"](
            session_factory=harness.session_factory
        )
        pending = await observability.refresh_pending()
        assert pending >= 2
        assert isinstance(observability.snapshot().pending_receipts, int)
