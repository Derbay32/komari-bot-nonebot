"""Focused helpers for the TSK-195 delivered-memory commitment tests.

The helper freezes a real ``HindsightRetainRequest`` so each fixture gets a
strictly valid serialized request, while the runtime session double keeps the
shared caller boundary observable without any network or provider access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from komari_bot.plugins.komari_memory.services.hindsight_retain import (
    HindsightRetainRequest,
)
from komari_bot.plugins.komari_memory.services.hindsight_service import (
    HindsightUnavailableError,
)

TARGET_FINGERPRINT = "a" * 64


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def reply_document(
    *,
    reply_content: str = "回复正文",
    group_id: str = "group-1",
    bot_self_id: str = "bot-1",
    adapter_name: str = "onebot.v11",
    trigger_message_id: str = "message-1",
    trigger_user_id: str = "user-1",
    reply_target_message_id: str = "message-1",
    generated_at: float = 2.0,
) -> str:
    return canonical_json(
        {
            "adapter_name": adapter_name,
            "bot_self_id": bot_self_id,
            "generated_at": datetime.fromtimestamp(generated_at, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "group_id": group_id,
            "reply_content": reply_content,
            "reply_target_message_id": reply_target_message_id,
            "trigger_message_id": trigger_message_id,
            "trigger_user_id": trigger_user_id,
        }
    )


def serialized_retain_request(
    *,
    fulfillment_id: str = "reply-" + "b" * 64,
    target_fingerprint: str = TARGET_FINGERPRINT,
    reply_content: str = "回复正文",
    group_id: str = "group-1",
    bot_self_id: str = "bot-1",
    adapter_name: str = "onebot.v11",
    trigger_message_id: str = "message-1",
    trigger_user_id: str = "user-1",
    reply_target_message_id: str = "message-1",
    generated_at: float = 2.0,
    bot_nickname: str = "小鞠",
) -> str:
    request = HindsightRetainRequest.create(
        target_fingerprint=target_fingerprint,
        source_id=f"reply:{fulfillment_id}",
        content=reply_document(
            reply_content=reply_content,
            group_id=group_id,
            bot_self_id=bot_self_id,
            adapter_name=adapter_name,
            trigger_message_id=trigger_message_id,
            trigger_user_id=trigger_user_id,
            reply_target_message_id=reply_target_message_id,
            generated_at=generated_at,
        ),
        timestamp=datetime.fromtimestamp(generated_at, UTC),
        metadata={"bot_nickname": bot_nickname},
    )
    return canonical_json(request.to_json())


def memory_payload(
    fulfillment_id: str = "reply-" + "b" * 64,
    **overrides: Any,
) -> dict[str, str]:
    return {
        "request": serialized_retain_request(
            fulfillment_id=fulfillment_id,
            **overrides,
        )
    }


@dataclass
class RetainSessionDouble:
    service: "HindsightServiceDouble"
    target_fingerprint: str

    async def retain(
        self,
        request: HindsightRetainRequest,
        *,
        submission_guard: Any,
    ) -> None:
        self.service.retain_calls.append(request)
        await self.service.retain_hook(request, submission_guard)


@dataclass
class HindsightServiceDouble:
    """Task-session double at the accepted shared caller boundary."""

    target_fingerprint: str = TARGET_FINGERPRINT
    open_calls: list[str | None] = field(default_factory=list)
    sessions: list[RetainSessionDouble] = field(default_factory=list)
    retain_calls: list[HindsightRetainRequest] = field(default_factory=list)
    open_error: BaseException | None = None
    retain_error: BaseException | None = None
    require_guard: bool = False

    async def retain_hook(
        self,
        request: HindsightRetainRequest,
        submission_guard: Any,
    ) -> None:
        del request
        if self.require_guard and not submission_guard():
            raise HindsightUnavailableError("submission_not_admitted")
        if self.retain_error is not None:
            raise self.retain_error

    def open_session(
        self,
        *,
        expected_target_fingerprint: str | None = None,
    ) -> "SessionContextDouble":
        return SessionContextDouble(self, expected_target_fingerprint)


@dataclass
class SessionContextDouble:
    service: HindsightServiceDouble
    expected_target_fingerprint: str | None

    async def __aenter__(self) -> RetainSessionDouble:
        self.service.open_calls.append(self.expected_target_fingerprint)
        if self.service.open_error is not None:
            raise self.service.open_error
        if (
            self.expected_target_fingerprint is not None
            and self.expected_target_fingerprint != self.service.target_fingerprint
        ):
            raise HindsightUnavailableError("target_changed")
        session = RetainSessionDouble(
            service=self.service,
            target_fingerprint=self.service.target_fingerprint,
        )
        self.service.sessions.append(session)
        return session

    async def __aexit__(
        self,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None


__all__ = [
    "TARGET_FINGERPRINT",
    "HindsightServiceDouble",
    "RetainSessionDouble",
    "SessionContextDouble",
    "canonical_json",
    "memory_payload",
    "reply_document",
    "serialized_retain_request",
]
