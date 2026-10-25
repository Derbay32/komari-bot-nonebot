"""Test support for the Hindsight chat seams.

A fake task session and service double replace the SDK at the production
boundary; tests still drive the real handler and tool loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hindsight_client import RecallResponse

    from komari_bot.plugins.komari_memory.services.hindsight_retain import (
        HindsightRetainRequest,
    )
    from komari_bot.plugins.komari_memory.services.hindsight_service import (
        HindsightUnavailableError,
    )

DEFAULT_TARGET_FINGERPRINT = "a" * 64


def recall_response(*results: dict[str, Any]) -> "RecallResponse":
    from hindsight_client import RecallResponse

    return RecallResponse.model_validate({"results": list(results)})


RetainHandler = Callable[
    ["FakeRecallSession", "HindsightRetainRequest", Callable[[], bool]],
    Awaitable[None],
]


@dataclass
class FakeRecallSession:
    service: "FakeHindsightMemory"
    target_fingerprint: str = DEFAULT_TARGET_FINGERPRINT
    recall_calls: list[str] = field(default_factory=list)
    retain_calls: list["HindsightRetainRequest"] = field(default_factory=list)
    retain_guards: list[Callable[[], bool]] = field(default_factory=list)
    retain_observations: list[bool] = field(default_factory=list)
    closed: bool = False

    async def recall(self, query: str) -> "RecallResponse":
        self.recall_calls.append(query)
        if self.service.recall_error is not None:
            raise self.service.recall_error
        return self.service.recall_response

    async def retain(
        self,
        request: "HindsightRetainRequest",
        *,
        submission_guard: Callable[[], bool],
    ) -> None:
        self.retain_calls.append(request)
        self.retain_guards.append(submission_guard)
        admitted = submission_guard()
        self.retain_observations.append(admitted)
        if self.service.retain_handler is not None:
            await self.service.retain_handler(self, request, submission_guard)
            return
        if self.service.retain_error is not None:
            raise self.service.retain_error
        if not admitted:
            from komari_bot.plugins.komari_memory.services.hindsight_service import (
                HindsightUnavailableError,
            )

            raise HindsightUnavailableError("submission_not_admitted")

    async def aclose(self) -> None:
        self.closed = True


class FakeSessionContext:
    def __init__(
        self,
        service: "FakeHindsightMemory",
        *,
        expected_target_fingerprint: str | None = None,
    ) -> None:
        self._service = service
        self._expected_target_fingerprint = expected_target_fingerprint
        self.session: FakeRecallSession | None = None

    async def __aenter__(self) -> FakeRecallSession:
        self._service.open_calls += 1
        if self._service.open_error is not None:
            raise self._service.open_error
        if (
            self._expected_target_fingerprint is not None
            and self._expected_target_fingerprint != self._service.target_fingerprint
        ):
            from komari_bot.plugins.komari_memory.services.hindsight_service import (
                HindsightUnavailableError,
            )

            raise HindsightUnavailableError("target_changed")
        self.session = FakeRecallSession(
            service=self._service,
            target_fingerprint=self._service.target_fingerprint,
        )
        self._service.sessions.append(self.session)
        return self.session

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        if self.session is not None:
            await self.session.aclose()


def fake_recall_session(
    memory: "FakeHindsightMemory",
    *,
    target_fingerprint: str | None = None,
) -> FakeRecallSession:
    """Create one already-open session for direct handler seams."""
    session = FakeRecallSession(
        service=memory,
        target_fingerprint=target_fingerprint or memory.target_fingerprint,
    )
    memory.sessions.append(session)
    return session


class FakeHindsightMemory:
    def __init__(
        self,
        *,
        response: "RecallResponse | None" = None,
        open_error: "HindsightUnavailableError | None" = None,
        recall_error: "HindsightUnavailableError | None" = None,
        retain_error: BaseException | None = None,
        retain_handler: RetainHandler | None = None,
        target_fingerprint: str = DEFAULT_TARGET_FINGERPRINT,
    ) -> None:
        self.recall_response = response or recall_response()
        self.open_error = open_error
        self.recall_error = recall_error
        self.retain_error = retain_error
        self.retain_handler = retain_handler
        self.target_fingerprint = target_fingerprint
        self.open_calls = 0
        self.sessions: list[FakeRecallSession] = []

    def open_session(
        self,
        *,
        expected_target_fingerprint: str | None = None,
    ) -> FakeSessionContext:
        return FakeSessionContext(
            self,
            expected_target_fingerprint=expected_target_fingerprint,
        )

    @property
    def recall_calls(self) -> list[str]:
        return [query for session in self.sessions for query in session.recall_calls]

    @property
    def retain_calls(self) -> list["HindsightRetainRequest"]:
        return [
            request for session in self.sessions for request in session.retain_calls
        ]


__all__ = [
    "DEFAULT_TARGET_FINGERPRINT",
    "FakeHindsightMemory",
    "FakeRecallSession",
    "FakeSessionContext",
    "fake_recall_session",
    "recall_response",
]
