"""Test support for the TSK-193 Hindsight caller seam.

A fake task session and service double replace the SDK at the production
boundary; tests still drive the real handler and tool loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hindsight_client import RecallResponse

    from komari_bot.plugins.komari_memory.services.hindsight_service import (
        HindsightUnavailableError,
    )


def recall_response(*results: dict[str, Any]) -> "RecallResponse":
    from hindsight_client import RecallResponse

    return RecallResponse.model_validate({"results": list(results)})


@dataclass
class FakeRecallSession:
    service: "FakeHindsightMemory"
    recall_calls: list[str] = field(default_factory=list)
    retain_calls: list[Any] = field(default_factory=list)
    closed: bool = False
    target_fingerprint: str = "a" * 64

    async def recall(self, query: str) -> "RecallResponse":
        self.recall_calls.append(query)
        if self.service.recall_error is not None:
            raise self.service.recall_error
        return self.service.recall_response

    async def retain(
        self,
        request: object,
        *,
        submission_guard: object = None,
    ) -> None:
        del submission_guard
        self.retain_calls.append(request)

    async def aclose(self) -> None:
        self.closed = True


class FakeSessionContext:
    def __init__(self, service: "FakeHindsightMemory") -> None:
        self._service = service
        self.session: FakeRecallSession | None = None

    async def __aenter__(self) -> FakeRecallSession:
        self._service.open_calls += 1
        if self._service.open_error is not None:
            raise self._service.open_error
        self.session = FakeRecallSession(service=self._service)
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


class FakeHindsightMemory:
    def __init__(
        self,
        *,
        response: "RecallResponse | None" = None,
        open_error: "HindsightUnavailableError | None" = None,
        recall_error: "HindsightUnavailableError | None" = None,
    ) -> None:
        self.recall_response = response or recall_response()
        self.open_error = open_error
        self.recall_error = recall_error
        self.open_calls = 0
        self.sessions: list[FakeRecallSession] = []

    def open_session(
        self,
        *,
        expected_target_fingerprint: str | None = None,
    ) -> FakeSessionContext:
        del expected_target_fingerprint
        return FakeSessionContext(self)

    @property
    def recall_calls(self) -> list[str]:
        return [query for session in self.sessions for query in session.recall_calls]


__all__ = [
    "FakeHindsightMemory",
    "FakeRecallSession",
    "FakeSessionContext",
    "recall_response",
]
