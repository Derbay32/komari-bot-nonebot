"""TSK-203 trusted chat submission candidate receipts."""

from __future__ import annotations

from importlib import import_module

import pytest

candidate_module = import_module(
    "komari_bot.plugins.komari_chat.services.chat_candidate"
)


def _candidate(*, cap: int = 5) -> object:
    return candidate_module.ChatCandidate(max_favorability_delta=cap)


def test_final_response_stages_without_completing_and_reports_missing_other() -> None:
    candidate = _candidate()
    receipt = candidate.submit_final_response({"content": "  收到啦  "})

    assert receipt.to_dict() == {
        "accepted": True,
        "code": None,
        "missing": ["record_favorability_delta"],
        "result": None,
    }
    assert candidate.is_complete is False
    assert candidate.missing_requirements == ("record_favorability_delta",)


def test_either_submission_order_can_complete_only_after_both_components() -> None:
    final_first = _candidate()
    final_first.submit_favorability({"delta": 2, "reason": "聊得很开心"})
    receipt = final_first.submit_final_response({"content": "今天也一起玩吧"})

    assert receipt.accepted is True
    assert receipt.code is None
    assert receipt.missing == ()
    assert receipt.result == {
        "content": "今天也一起玩吧",
        "favorability_delta": 2,
        "favorability_reason": "聊得很开心",
    }
    assert final_first.is_complete is True

    favor_first = _candidate()
    favor_first.submit_final_response({"content": "今天也一起玩吧"})
    receipt = favor_first.submit_favorability({"delta": 2, "reason": "聊得很开心"})

    assert receipt.accepted is True
    assert receipt.result == {
        "content": "今天也一起玩吧",
        "favorability_delta": 2,
        "favorability_reason": "聊得很开心",
    }


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "invalid_content"),
        ({"content": "   "}, "invalid_content"),
        ({"content": "x" * 2001}, "invalid_content"),
        ({"content": "ok", "extra": 1}, "invalid_content"),
        ({"content": 3}, "invalid_content"),
    ],
)
def test_invalid_final_response_never_overwrites_a_valid_staged_value(
    payload: dict[str, object],
    code: str,
) -> None:
    candidate = _candidate()
    first = candidate.submit_final_response({"content": "有效正文"})
    assert first.accepted is True

    receipt = candidate.submit_final_response(payload)

    assert receipt.accepted is False
    assert receipt.code == code
    assert receipt.missing == ("record_favorability_delta",)
    assert receipt.result is None
    completed = candidate.submit_favorability({"delta": 1, "reason": "好"})
    assert completed.result is not None
    assert completed.result["content"] == "有效正文"


@pytest.mark.parametrize(
    ("payload", "code", "cap"),
    [
        ({}, "invalid_favorability", 5),
        ({"delta": True, "reason": "好"}, "invalid_favorability", 5),
        ({"delta": 1.5, "reason": "好"}, "invalid_favorability", 5),
        ({"delta": 4, "reason": "好"}, "invalid_favorability", 5),
        ({"delta": -6, "reason": "好"}, "invalid_favorability", 5),
        ({"delta": 3, "reason": ""}, "invalid_favorability", 5),
        ({"delta": 3, "reason": "x" * 201}, "invalid_favorability", 5),
        ({"delta": 3, "reason": "好", "extra": 1}, "invalid_favorability", 5),
        ({"delta": 3, "reason": "好"}, "invalid_favorability", 2),
    ],
)
def test_invalid_favorability_is_correctable_and_preserves_valid_final(
    payload: dict[str, object],
    code: str,
    cap: int,
) -> None:
    candidate = _candidate(cap=cap)
    candidate.submit_final_response({"content": "保留正文"})

    receipt = candidate.submit_favorability(payload)

    assert receipt.accepted is False
    assert receipt.code == code
    assert receipt.missing == ("record_favorability_delta",)
    assert receipt.result is None
    completed = candidate.submit_favorability({"delta": min(3, cap), "reason": "好"})
    assert completed.result is not None
    assert completed.result["content"] == "保留正文"


def test_completed_pair_rejects_replacements_and_keeps_original_result() -> None:
    candidate = _candidate()
    candidate.submit_final_response({"content": "正文"})
    completed = candidate.submit_favorability({"delta": -2, "reason": "有点担心"})
    assert completed.result is not None

    for receipt in (
        candidate.submit_final_response({"content": "另一个正文"}),
        candidate.submit_favorability({"delta": 3, "reason": "另一个理由"}),
    ):
        assert receipt.accepted is False
        assert receipt.code == "already_completed"
        assert receipt.missing == ()
        assert receipt.result == completed.result


def test_verify_host_result_requires_exact_staged_pair_and_rejects_fabrication() -> None:
    candidate = _candidate()
    candidate.submit_final_response({"content": "正文"})
    candidate.submit_favorability({"delta": 2, "reason": "理由"})

    verified = candidate.verify_host_result(
        {
            "content": "正文",
            "favorability_delta": 2,
            "favorability_reason": "理由",
        }
    )
    assert verified == (
        "正文",
        2,
        "理由",
    )

    for fabricated in (
        {"content": "正文", "favorability_delta": 2},
        {
            "content": "正文",
            "favorability_delta": 2,
            "favorability_reason": "理由",
            "extra": True,
        },
        {
            "content": "伪造",
            "favorability_delta": 2,
            "favorability_reason": "理由",
        },
        {
            "content": "正文",
            "favorability_delta": 3,
            "favorability_reason": "理由",
        },
    ):
        with pytest.raises(candidate_module.ChatResultMismatchError):
            candidate.verify_host_result(fabricated)


def test_verify_host_result_rejects_incomplete_pair() -> None:
    candidate = _candidate()
    candidate.submit_final_response({"content": "正文"})

    with pytest.raises(candidate_module.ChatResultMismatchError):
        candidate.verify_host_result(
            {
                "content": "正文",
                "favorability_delta": 2,
                "favorability_reason": "理由",
            }
        )
