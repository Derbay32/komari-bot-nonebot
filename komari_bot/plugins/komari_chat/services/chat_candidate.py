"""Trusted task-local staging of chat completion submissions.

The model-visible tool schemas are declared by the DSH chat preset. This module
owns the Python-side validation and candidate state: an invalid submission can
never erase an already valid component, and only a valid pair may produce the
structured result that the DSH task completes with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

MissingRequirement = Literal["final_response", "record_favorability_delta"]
SubmissionCode = Literal[
    "invalid_content",
    "invalid_favorability",
    "already_completed",
]

MAX_FINAL_CONTENT_CHARS = 2_000
MAX_FAVORABILITY_REASON_CHARS = 200
FAVORABILITY_TOOL_MIN = -5
FAVORABILITY_TOOL_MAX = 3
_REQUIREMENTS: tuple[MissingRequirement, ...] = (
    "final_response",
    "record_favorability_delta",
)


class InvalidFavorabilityCapError(ValueError):
    """The configured absolute favorability cap is not a nonnegative integer."""


class ChatSubmissionError(RuntimeError):
    """Base class for fixed, safe chat submission failures."""

    code = "chat_submission_invalid"


class ChatResultMismatchError(ChatSubmissionError):
    """The host returned a result that does not match the trusted candidate."""

    code = "chat_result_mismatch"


@dataclass(frozen=True)
class ChatSubmissionReceipt:
    """The exact receipt returned by either trusted submission callback."""

    accepted: bool
    code: SubmissionCode | None
    missing: tuple[MissingRequirement, ...]
    result: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "code": self.code,
            "missing": list(self.missing),
            "result": None if self.result is None else dict(self.result),
        }


class ChatCandidate:
    """One task's private staged final response and favorability proposal."""

    __slots__ = (
        "_completed",
        "_content",
        "_delta",
        "_max_favorability_delta",
        "_reason",
    )

    def __init__(self, *, max_favorability_delta: int) -> None:
        if (
            isinstance(max_favorability_delta, bool)
            or not isinstance(max_favorability_delta, int)
            or max_favorability_delta < 0
        ):
            raise InvalidFavorabilityCapError
        self._max_favorability_delta = max_favorability_delta
        self._content: str | None = None
        self._delta: int | None = None
        self._reason: str | None = None
        self._completed = False

    @property
    def is_complete(self) -> bool:
        return self._completed

    @property
    def missing_requirements(self) -> tuple[MissingRequirement, ...]:
        if self._completed:
            return ()
        missing: list[MissingRequirement] = []
        if self._content is None:
            missing.append("final_response")
        if self._delta is None or self._reason is None:
            missing.append("record_favorability_delta")
        return tuple(missing)

    @property
    def complete_result(self) -> dict[str, object] | None:
        if not self._completed:
            return None
        assert self._content is not None
        assert self._delta is not None
        assert self._reason is not None
        return {
            "content": self._content,
            "favorability_delta": self._delta,
            "favorability_reason": self._reason,
        }

    def submit_final_response(self, payload: Mapping[str, object]) -> ChatSubmissionReceipt:
        """Validate and synchronously stage one final response submission."""
        if self._completed:
            return self._already_completed_receipt()

        content: str | None = None
        if set(payload) == {"content"}:
            raw_content = payload.get("content")
            if isinstance(raw_content, str):
                stripped = raw_content.strip()
                if stripped and len(stripped) <= MAX_FINAL_CONTENT_CHARS:
                    content = stripped
        if content is None:
            return self._invalid_receipt("invalid_content")
        self._content = content
        return self._accepted_or_complete_receipt()

    def submit_favorability(self, payload: Mapping[str, object]) -> ChatSubmissionReceipt:
        """Validate and synchronously stage one favorability proposal."""
        if self._completed:
            return self._already_completed_receipt()

        delta: int | None = None
        reason: str | None = None
        if set(payload) == {"delta", "reason"}:
            raw_delta = payload.get("delta")
            raw_reason = payload.get("reason")
            if (
                isinstance(raw_delta, int)
                and not isinstance(raw_delta, bool)
                and FAVORABILITY_TOOL_MIN <= raw_delta <= FAVORABILITY_TOOL_MAX
                and abs(raw_delta) <= self._max_favorability_delta
            ):
                delta = raw_delta
            if isinstance(raw_reason, str):
                stripped_reason = raw_reason.strip()
                if stripped_reason and len(stripped_reason) <= MAX_FAVORABILITY_REASON_CHARS:
                    reason = stripped_reason
        if delta is None or reason is None:
            return self._invalid_receipt("invalid_favorability")
        self._delta = delta
        self._reason = reason
        return self._accepted_or_complete_receipt()

    def verify_host_result(self, result: Mapping[str, object]) -> tuple[str, int, str]:
        """Require the host result to be exactly the pair staged by this candidate."""
        if not self._completed:
            raise ChatResultMismatchError
        expected = self.complete_result
        if expected is None or set(result) != set(expected):
            raise ChatResultMismatchError
        content = result.get("content")
        delta = result.get("favorability_delta")
        reason = result.get("favorability_reason")
        if (
            not isinstance(content, str)
            or isinstance(delta, bool)
            or not isinstance(delta, int)
            or not isinstance(reason, str)
            or content != expected["content"]
            or delta != expected["favorability_delta"]
            or reason != expected["favorability_reason"]
        ):
            raise ChatResultMismatchError
        return content, delta, reason

    def _accepted_or_complete_receipt(self) -> ChatSubmissionReceipt:
        if self._content is not None and self._delta is not None and self._reason is not None:
            self._completed = True
            return ChatSubmissionReceipt(
                accepted=True,
                code=None,
                missing=(),
                result=self.complete_result,
            )
        return ChatSubmissionReceipt(
            accepted=True,
            code=None,
            missing=self.missing_requirements,
            result=None,
        )

    def _invalid_receipt(self, code: SubmissionCode) -> ChatSubmissionReceipt:
        return ChatSubmissionReceipt(
            accepted=False,
            code=code,
            missing=self.missing_requirements,
            result=None,
        )

    def _already_completed_receipt(self) -> ChatSubmissionReceipt:
        return ChatSubmissionReceipt(
            accepted=False,
            code="already_completed",
            missing=(),
            result=self.complete_result,
        )


__all__ = [
    "FAVORABILITY_TOOL_MAX",
    "FAVORABILITY_TOOL_MIN",
    "MAX_FAVORABILITY_REASON_CHARS",
    "MAX_FINAL_CONTENT_CHARS",
    "ChatCandidate",
    "ChatResultMismatchError",
    "ChatSubmissionError",
    "ChatSubmissionReceipt",
    "InvalidFavorabilityCapError",
]
