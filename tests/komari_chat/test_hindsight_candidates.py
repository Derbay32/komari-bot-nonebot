"""TSK-194B candidate integration at the production message entry.

The tests drive the real ``MessageHandler.process_message`` seam and the real
Hindsight service/session seam.  Only external I/O (SDK transport, chat
generation, platform calls, persistence adapter for decision rows) is replaced.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from hindsight_client import RecallResponse
from hindsight_client_api.exceptions import NotFoundException
from hindsight_client_api.models.retain_response import RetainResponse
from nonebot.adapters.onebot.v11 import Message

from komari_bot.decision.dispatch import DecisionDispatch
from komari_bot.decision.jev import (
    DecisionMessage,
    DecisionRequest,
    DecisionResult,
    EventIdentity,
)
from komari_bot.plugins.komari_chat.handlers import message_handler as mh
from komari_bot.plugins.komari_memory.services import hindsight_service
from komari_bot.plugins.komari_memory.services.hindsight_service import (
    HindsightMemoryService,
    HindsightUnavailableError,
)
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
)

BANK_ID = "shared-bank-1"
TARGET_FINGERPRINT = "a" * 64
USER_ID = "67890"
GROUP_ID = "700100"
MESSAGE_ID = "900001"
BOT_SELF_ID = "12345"
NOW = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def _candidate_module() -> Any:
    from komari_bot.plugins.komari_chat.services import memory_candidate

    return memory_candidate


# ---------------------------------------------------------------------------
# Shared production-entry doubles
# ---------------------------------------------------------------------------


class _Redis:
    def __init__(self, buffer: list[Any] | None = None) -> None:
        self.buffer = list(buffer or [])
        self.pushed: list[Any] = []
        self.redis = _CooldownRedis()

    async def push_message(self, _group_id: str, message: Any) -> None:
        self.pushed.append(message)
        self.buffer.append(message)

    async def get_buffer(self, _group_id: str, *, limit: int = 10) -> list[Any]:
        del limit
        return list(self.buffer)

    async def get_global_interaction_buffer(
        self, _user_id: str, *, limit: int = 10
    ) -> list[dict[str, object]]:
        del limit
        return []


class _BlockingRedis(_Redis):
    """_Redis whose existing-buffer read can be paused by the test."""

    def __init__(self, buffer: list[Any] | None = None) -> None:
        super().__init__(buffer)
        self.read_started = asyncio.Event()
        self.read_release = asyncio.Event()

    async def get_buffer(self, group_id: str, *, limit: int = 10) -> list[Any]:
        self.read_started.set()
        await self.read_release.wait()
        return await super().get_buffer(group_id, limit=limit)


class _CooldownRedis:
    async def set(self, *_args: object, **_kwargs: object) -> bool:
        return True


class _Fulfillment:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.pending: list[Any] = []
        self.duplicate_checks: list[str] = []

    async def is_duplicate_event(self, fulfillment_id: str) -> bool:
        self.duplicate_checks.append(fulfillment_id)
        return self.duplicate

    async def fulfill(self, pending_reply: Any, **_kwargs: object) -> bool:
        self.pending.append(pending_reply)
        return True


class _Lease:
    async def __aenter__(self) -> "_Lease":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def handoff(self) -> object:
        return object()


class _Reservation:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def reserve(self, group_id: str, reservation_id: str) -> _Lease:
        self.calls.append((group_id, reservation_id))
        return _Lease()


class _Rewrite:
    def __init__(self, rewritten: str = "改写后的查询") -> None:
        self.rewritten = rewritten
        self.calls: list[str] = []

    async def rewrite_query(self, current_query: str, **_kwargs: object) -> str:
        self.calls.append(current_query)
        return self.rewritten


class _UserData:
    def get_config(self) -> SimpleNamespace:
        return SimpleNamespace(max_favorability_delta_per_reply=5)

    async def get_user_favorability(self, _user_id: str) -> SimpleNamespace:
        return SimpleNamespace(favorability=0)


class _Bot:
    def __init__(self) -> None:
        self.self_id = BOT_SELF_ID
        self.type = "OneBot V11"
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, api: str, **data: object) -> object:
        self.calls.append((api, data))
        return {"message_id": 424242}

    async def get_msg(self, *, message_id: int) -> dict[str, object]:
        self.calls.append(("get_msg", {"message_id": message_id}))
        return {"message_id": message_id}


class _Event:
    def __init__(
        self,
        *,
        text: str = "今天谁值班？",
        message_id: str = MESSAGE_ID,
        group_id: str = GROUP_ID,
        user_id: str = USER_ID,
        nickname: str = "群友",
        to_me: bool = False,
    ) -> None:
        self.user_id = int(user_id)
        self.group_id = int(group_id)
        self.message_id = message_id
        self.self_id = int(BOT_SELF_ID)
        self.to_me = to_me
        self.reply = None
        self.message = Message(text)
        self.sender = SimpleNamespace(
            user_id=int(user_id),
            nickname=nickname,
            card=nickname,
        )


def _chat_config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "chat_enabled": True,
        "proactive_enabled": False,
        "context_messages_limit": 10,
        "context_max_utf8_bytes": 24_000,
        "context_max_estimated_tokens": 6_000,
        "summary_max_buffer_size": 500,
        "memory_search_limit": 3,
        "bot_nickname": "小鞠",
        "bot_aliases": ["小鞠", "小鞠知花", "komari"],
        "error_notify_enabled": False,
        "face_reaction_enabled": False,
        "face_reaction_id": None,
        "agent_max_rounds": 10,
        "agent_max_tool_calls_per_round": 4,
        "agent_max_total_tool_calls": 20,
        "agent_tool_call_mode": "required",
        "image_understanding_mode": "native",
        "llm_model_chat": "chat-model",
        "llm_temperature_chat": 0.7,
        "llm_max_tokens_chat": 1024,
        "llm_thinking_mode_chat": False,
        "llm_reasoning_effort_chat": "",
        "llm_request_api_chat": "chat_completions",
        "llm_stream_enabled_chat": False,
        "knowledge_enabled": False,
        "vision_image_download_max_count": 4,
        "vision_image_download_max_bytes": 8 * 1024 * 1024,
        "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
        "vision_image_download_max_pixels": 40_000_000,
        "vision_image_download_concurrency": 2,
        "vision_image_download_connect_timeout_seconds": 5.0,
        "vision_image_download_read_timeout_seconds": 30.0,
        "vision_image_download_total_timeout_seconds": 45.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeDshGeneration:
    """Trusted generation seam double; does not rebuild DSH business logic."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls
        self.error: BaseException | None = None
        self.result = import_module(
            "komari_bot.plugins.komari_chat.services.chat_result"
        ).ReplyResult(
            content="回复正文",
            favorability_delta=0,
            favorability_reason="测试",
        )

    async def generate(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def _wire_entry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: SimpleNamespace | None = None,
    generate_calls: list[dict[str, Any]] | None = None,
    prompt_calls: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    chat_config = config or _chat_config()
    generate_calls = generate_calls if generate_calls is not None else []
    # Prompt assembly now belongs to the DSH preset. Handler-level tests
    # observe the task data passed to the generation service instead.
    prompt_calls = generate_calls

    service = _FakeDshGeneration(generate_calls)

    def _service_factory() -> _FakeDshGeneration:
        return service

    async def _not_superuser(_bot: object, _event: object) -> bool:
        return False

    monkeypatch.setattr(mh, "get_config", lambda: chat_config)
    monkeypatch.setattr(mh, "get_context_config", lambda: chat_config)
    monkeypatch.setattr(mh, "DshChatService", _service_factory)
    monkeypatch.setattr(mh, "SUPERUSER", _not_superuser)
    return SimpleNamespace(
        config=chat_config,
        generate_calls=generate_calls,
        prompt_calls=prompt_calls,
        service=service,
    )


def _handler(
    memory: Any,
    *,
    redis: _Redis | None = None,
    fulfillment: _Fulfillment | None = None,
) -> Any:
    handler = mh.MessageHandler.__new__(mh.MessageHandler)
    handler.redis = redis or _Redis()  # type: ignore[assignment]
    handler.hindsight_memory = memory
    handler.query_rewrite = _Rewrite()  # type: ignore[assignment]
    handler.reply_fulfillment = fulfillment or _Fulfillment()  # type: ignore[assignment]
    handler.proactive_reservation = _Reservation()  # type: ignore[assignment]
    handler.decision_engine = SimpleNamespace()  # type: ignore[assignment]
    handler._reaction_tasks = set()
    return handler


class _ScriptedDispatch:
    def __init__(
        self,
        request: DecisionRequest,
        result: DecisionResult,
        *,
        claim_allowed: bool = True,
    ) -> None:
        self.request = request
        self.result = result
        self.claim_allowed = claim_allowed
        self.claim_routes: list[str] = []
        self.evaluate_calls = 0

    async def evaluate(
        self,
        _engine: object,
        *,
        summary_candidate: bool,
    ) -> DecisionResult:
        assert summary_candidate is False
        self.evaluate_calls += 1
        return self.result

    async def claim_handoff(self, route: str) -> bool:
        self.claim_routes.append(route)
        return self.claim_allowed


def _request(
    *,
    text: str = "今天谁值班？",
    message_id: str = MESSAGE_ID,
    group_id: str = GROUP_ID,
    user_id: str = USER_ID,
    nickname: str = "群友",
    reference_ids: tuple[str, ...] = (),
    known_references: tuple[DecisionMessage, ...] = (),
    received_at: datetime = NOW,
    dispatch_id: str | None = None,
) -> DecisionRequest:
    identity = EventIdentity(
        protocol="onebot_v11",
        account_id=BOT_SELF_ID,
        group_id=group_id,
        message_id=message_id,
    )
    return DecisionRequest(
        event=identity,
        target=DecisionMessage(
            message_id=message_id,
            group_id=group_id,
            sender_id=user_id,
            text=text,
            timestamp=NOW,
            display_name=nickname,
            reference_ids=reference_ids,
        ),
        received_at=received_at,
        dispatch_id=dispatch_id or f"dispatch-{message_id}",
        known_references=known_references,
    )


def _result(
    *,
    route: str = "none",
    memory_candidate: bool = True,
    force_reply: bool = False,
    status: str = "success",
    reason_code: str = "classified",
) -> DecisionResult:
    return DecisionResult(
        decision_id="decision-1",
        status=status,  # type: ignore[arg-type]
        reason_code=reason_code,  # type: ignore[arg-type]
        route=route,  # type: ignore[arg-type]
        force_reply=force_reply,
        effective_reply="required" if force_reply else "silent",
        retain_recent=memory_candidate,
        memory_candidate=memory_candidate,
    )


async def _run(
    handler: Any,
    dispatch: Any,
    *,
    reply_allowed: bool = True,
) -> Any:
    return await handler.process_message(
        _Bot(),  # type: ignore[arg-type]
        _Event(),  # type: ignore[arg-type]
        decision_dispatch=cast("Any", dispatch),
        reply_allowed=reply_allowed,
    )


def _retained_document(session: Any) -> dict[str, Any]:
    assert len(session.retain_calls) == 1
    return json.loads(session.retain_calls[0].content)


# ---------------------------------------------------------------------------
# Import boundary
# ---------------------------------------------------------------------------


def test_candidate_builder_uses_memory_top_level_contract_only() -> None:
    from pathlib import Path

    source = Path(_candidate_module().__file__).read_text(encoding="utf-8")
    assert (
        "from komari_bot.plugins.komari_memory import HindsightRetainRequest" in source
    )
    assert "komari_bot.plugins.komari_memory.services" not in source
    assert "hindsight_client" not in source


def test_message_handler_keeps_memory_imports_top_level() -> None:
    from pathlib import Path

    source = Path(mh.__file__).read_text(encoding="utf-8")
    assert "komari_bot.plugins.komari_memory.services" not in source
    assert "hindsight_client" not in source


# ---------------------------------------------------------------------------
# Frozen candidate document (pure builder)
# ---------------------------------------------------------------------------


def _reference(
    *,
    message_id: str,
    group_id: str = GROUP_ID,
    sender_id: str,
    text: str,
    timestamp: datetime = NOW,
    reference_ids: tuple[str, ...] = (),
    display_name: str = "群友",
) -> DecisionMessage:
    return DecisionMessage(
        message_id=message_id,
        group_id=group_id,
        sender_id=sender_id,
        text=text,
        timestamp=timestamp,
        display_name=display_name,
        reference_ids=reference_ids,
    )


def test_candidate_document_carries_explicit_identity_and_platform_time() -> None:
    memory_candidate = _candidate_module()
    request = _request(
        text="平台原文",
        nickname="同名",
        reference_ids=("ref-1",),
        known_references=(
            _reference(
                message_id="ref-1",
                sender_id="user-ref",
                text="引用原文",
                display_name="同名",
            ),
        ),
    )

    document = memory_candidate.build_candidate_document(request)

    candidate = document["candidate"]
    assert candidate["protocol"] == "onebot_v11"
    assert candidate["account_id"] == BOT_SELF_ID
    assert candidate["group_id"] == GROUP_ID
    assert candidate["sender_id"] == USER_ID
    assert candidate["message_id"] == MESSAGE_ID
    assert candidate["text"] == "平台原文"
    assert candidate["timestamp"] == "2026-01-02T03:04:05.123456Z"
    assert candidate["display_name"] == "同名"
    assert candidate["is_bot"] is False
    assert candidate["mention_ids"] == []
    assert candidate["reference_ids"] == ["ref-1"]
    assert candidate["image_present"] is False
    assert "image_description" not in candidate

    evidence = document["candidate_evidence"]
    assert evidence["complete"] is False
    nodes = evidence["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["message_id"] == "ref-1"
    assert nodes[0]["sender_id"] == "user-ref"
    assert nodes[0]["timestamp"] == "2026-01-02T03:04:05.123456Z"
    assert nodes[0]["display_name"] == "同名"


def test_candidate_document_uses_only_reachable_initial_references() -> None:
    memory_candidate = _candidate_module()
    request = _request(
        reference_ids=("reachable",),
        known_references=(
            _reference(
                message_id="reachable",
                sender_id="u1",
                text="可到达",
                reference_ids=("nested",),
            ),
            _reference(
                message_id="nested",
                sender_id="u2",
                text="嵌套",
            ),
            _reference(
                message_id="unreachable",
                sender_id="u3",
                text="不可达",
            ),
        ),
    )

    document = memory_candidate.build_candidate_document(request)
    nodes = document["candidate_evidence"]["nodes"]

    assert [node["message_id"] for node in nodes] == ["reachable", "nested"]
    assert all(node["group_id"] == GROUP_ID for node in nodes)


def test_candidate_document_records_missing_and_cycle_edges() -> None:
    memory_candidate = _candidate_module()
    target = _request(
        message_id="target",
        reference_ids=("known", "missing"),
        known_references=(
            _reference(
                message_id="known",
                sender_id="u1",
                text="已知",
                reference_ids=("target",),
            ),
        ),
    )
    document = memory_candidate.build_candidate_document(target)
    evidence = document["candidate_evidence"]

    assert evidence["unresolved_reference_ids"] == ["missing"]
    assert evidence["cycle_edges"] == [
        {"from_message_id": "known", "to_message_id": "target"}
    ]


def test_candidate_document_skips_cross_group_and_future_evidence() -> None:
    memory_candidate = _candidate_module()
    request = _request(
        reference_ids=("other-group", "future"),
        received_at=NOW,
        known_references=(
            _reference(
                message_id="other-group",
                group_id="different-group",
                sender_id="u1",
                text="别的群",
            ),
            _reference(
                message_id="future",
                sender_id="u2",
                text="迟到",
                timestamp=datetime(2026, 1, 2, 3, 4, 6, tzinfo=UTC),
            ),
        ),
    )

    document = memory_candidate.build_candidate_document(request)

    assert document["candidate_evidence"]["nodes"] == []


def test_candidate_document_only_carries_present_image_description() -> None:
    memory_candidate = _candidate_module()
    identity = EventIdentity(
        protocol="onebot_v11",
        account_id=BOT_SELF_ID,
        group_id=GROUP_ID,
        message_id="image-1",
    )
    request = DecisionRequest(
        event=identity,
        target=DecisionMessage(
            message_id="image-1",
            group_id=GROUP_ID,
            sender_id=USER_ID,
            text="看图",
            timestamp=NOW,
            image_present=True,
            image_description="画面中有一只猫",
        ),
        received_at=NOW,
    )

    content = memory_candidate.build_candidate_retain_request(
        request=request,
        target_fingerprint=TARGET_FINGERPRINT,
    ).content
    document = json.loads(content)

    assert document["candidate"]["image_present"] is True
    assert document["candidate"]["image_description"] == "画面中有一只猫"
    assert "data:image" not in content
    assert "base64" not in content
    assert "http://" not in content
    assert "https://" not in content


def test_candidate_source_id_and_metadata_are_stable_and_identity_exact() -> None:
    memory_candidate = _candidate_module()
    first = _request(message_id="m-1", user_id="u-1", nickname="同名")
    second = _request(message_id="m-2", user_id="u-2", nickname="同名")

    first_request = memory_candidate.build_candidate_retain_request(
        request=first,
        target_fingerprint=TARGET_FINGERPRINT,
    )
    second_request = memory_candidate.build_candidate_retain_request(
        request=second,
        target_fingerprint=TARGET_FINGERPRINT,
    )

    canonical_identity = json.dumps(
        {
            "account_id": BOT_SELF_ID,
            "group_id": GROUP_ID,
            "message_id": "m-1",
            "protocol": "onebot_v11",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert first_request.source_id == f"candidate:{canonical_identity}"
    assert first_request.source_id != second_request.source_id
    assert first_request.timestamp == NOW
    assert first_request.metadata == {
        "schema_version": "1",
        "source_type": "chat_candidate",
    }
    assert first_request.target_fingerprint == TARGET_FINGERPRINT


# ---------------------------------------------------------------------------
# Candidate selection and ordering at the production message entry
# ---------------------------------------------------------------------------


async def test_silent_candidate_retains_once_without_generation_or_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(_request(), _result(route="none"))

    result = await _run(handler, dispatch)

    assert result is None
    assert len(redis.pushed) == 1
    assert len(memory.sessions) == 1
    session = memory.sessions[0]
    assert session.closed is True
    assert len(session.retain_calls) == 1
    assert session.retain_observations == [True]
    document = _retained_document(session)
    assert document["candidate"]["message_id"] == MESSAGE_ID
    assert wired.generate_calls == []
    assert wired.prompt_calls == []
    assert dispatch.claim_routes == []
    assert handler.reply_fulfillment.pending == []


async def test_optional_not_selected_candidate_retains_without_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    handler = _handler(memory)
    dispatch = _ScriptedDispatch(_request(), _result(route="none"))

    result = await _run(handler, dispatch)

    assert result is None
    assert len(memory.retain_calls) == 1
    assert wired.generate_calls == []
    assert dispatch.claim_routes == []


async def test_silent_noise_never_retains_or_generates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(
        _request(), _result(route="none", memory_candidate=False)
    )

    result = await _run(handler, dispatch)

    assert result is None
    assert memory.open_calls == 0
    assert memory.retain_calls == []
    assert redis.pushed == []
    assert wired.generate_calls == []


async def test_chat_ban_retains_candidate_without_reply_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    result = await _run(handler, dispatch, reply_allowed=False)

    assert result is None
    assert len(redis.pushed) == 1
    assert len(memory.retain_calls) == 1
    assert wired.generate_calls == []
    assert dispatch.claim_routes == []
    assert handler.reply_fulfillment.pending == []


async def test_chat_candidate_submits_before_generation_and_reuses_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()

    async def _retain(
        _session: Any,
        _request: Any,
        _guard: Any,
    ) -> None:
        order.append("retain")

    memory.retain_handler = _retain
    service = wired.service

    async def _generate(**kwargs: Any) -> Any:
        order.append("generate")
        assert kwargs["recall_session"] is memory.sessions[0]
        return await _FakeDshGeneration([]).generate(**kwargs)

    service.generate = _generate  # type: ignore[method-assign]
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    pending = await _run(handler, dispatch)

    assert pending is not None
    assert order == ["retain", "generate"]
    assert len(memory.sessions) == 1
    assert memory.sessions[0].closed is True
    assert len(memory.retain_calls) == 1
    assert len(redis.pushed) == 1
    assert dispatch.claim_routes == ["chat"]


async def test_prompt_recent_context_excludes_current_trigger_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    previous = SimpleNamespace(
        user_id="u-0",
        user_nickname="旧人",
        group_id=GROUP_ID,
        content="上一条",
        timestamp=1.0,
        message_id="old-1",
    )
    memory = FakeHindsightMemory()
    redis = _Redis([previous])
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    pending = await _run(handler, dispatch)

    assert pending is not None
    recent = wired.generate_calls[0]["recent_messages"]
    assert [message.message_id for message in recent] == ["old-1"]
    assert len(redis.pushed) == 1
    assert redis.pushed[0].message_id == MESSAGE_ID


async def test_fulfillment_duplicate_stops_before_session_and_retain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    redis = _Redis()
    fulfillment = _Fulfillment(duplicate=True)
    handler = _handler(memory, redis=redis, fulfillment=fulfillment)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    result = await _run(handler, dispatch)

    assert result is None
    assert memory.open_calls == 0
    assert memory.retain_calls == []
    assert redis.pushed == []
    assert wired.generate_calls == []
    assert dispatch.claim_routes == []


async def test_refused_chat_handoff_stops_before_session_and_retain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(
        _request(), _result(route="chat", force_reply=True), claim_allowed=False
    )

    result = await _run(handler, dispatch)

    assert result is None
    assert dispatch.claim_routes == ["chat"]
    assert memory.open_calls == 0
    assert memory.retain_calls == []
    assert redis.pushed == []
    assert wired.generate_calls == []


async def test_duplicate_or_cancelled_decision_has_no_memory_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _handler(memory, redis=redis)

    for status in ("duplicate", "cancelled"):
        dispatch = _ScriptedDispatch(
            _request(
                message_id=f"{status}-1",
            ),
            _result(
                route="none",
                memory_candidate=True,
                status=status,
                reason_code="duplicate_event"
                if status == "duplicate"
                else "interrupted",
            ),
        )
        result = await _run(handler, dispatch)
        assert result is None

    assert memory.open_calls == 0
    assert memory.retain_calls == []
    assert redis.pushed == []
    assert wired.generate_calls == []


async def test_admitted_candidate_submission_failure_stores_once_and_stops_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admitted positive control: one rejected retain keeps context once."""
    wired = _wire_entry(monkeypatch)
    memory = FakeHindsightMemory(
        retain_error=HindsightUnavailableError("response_invalid")
    )
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    decisions: list[dict[str, object]] = []
    handler._log_decision = decisions.append
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    result = await _run(handler, dispatch)

    assert result is None
    assert len(redis.pushed) == 1
    assert wired.generate_calls == []
    assert handler.reply_fulfillment.pending == []
    assert decisions[-1]["reply_action"] == "memory_failed"
    assert decisions[-1]["memory_failure_reason"] == "response_invalid"


async def test_memory_failure_before_generation_emits_no_group_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    memory = FakeHindsightMemory(
        open_error=HindsightUnavailableError("bank_unavailable")
    )
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    bot = _Bot()
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    result = await handler.process_message(
        bot,  # type: ignore[arg-type]
        _Event(),  # type: ignore[arg-type]
        decision_dispatch=cast("Any", dispatch),
        reply_allowed=True,
    )

    assert result is None
    assert len(redis.pushed) == 1
    assert bot.calls == []
    assert handler.reply_fulfillment.pending == []


async def test_admission_revoked_before_chat_session_open_skips_all_memory_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    monkeypatch.setattr(mh, "effect_business_admitted", lambda **_kwargs: False)
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    result = await _run(handler, dispatch)

    assert result is None
    assert memory.open_calls == 0
    assert memory.retain_calls == []
    assert redis.pushed == []


@pytest.mark.parametrize("state", ["restricted", "failed"])
async def test_post_handoff_admission_loss_never_stores_or_opens_memory(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    from tests.group_admission.chat_admission_support import (
        ScriptedAdjudicate,
        install_scripted_adjudicate,
    )

    wired = _wire_entry(monkeypatch)
    admission = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, admission)
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))
    original_claim = dispatch.claim_handoff

    async def _claim(route: str) -> bool:
        claimed = await original_claim(route)
        admission.set_state(state)
        return claimed

    monkeypatch.setattr(dispatch, "claim_handoff", _claim)

    result = await _run(handler, dispatch)

    assert result is None
    assert dispatch.claim_routes == ["chat"]
    assert memory.open_calls == 0
    assert memory.retain_calls == []
    assert redis.pushed == []
    assert wired.generate_calls == []
    assert handler.reply_fulfillment.pending == []


@pytest.mark.parametrize("state", ["restricted", "failed"])
async def test_session_open_failure_after_admission_loss_never_stores(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    from tests.group_admission.chat_admission_support import (
        ScriptedAdjudicate,
        install_scripted_adjudicate,
    )

    _wire_entry(monkeypatch)
    admission = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, admission)
    memory = FakeHindsightMemory(
        open_error=HindsightUnavailableError("bank_unavailable")
    )
    original_open = memory.open_session

    def _open_session(**kwargs: Any) -> Any:
        admission.set_state(state)
        return original_open(**kwargs)

    monkeypatch.setattr(memory, "open_session", _open_session)
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    result = await _run(handler, dispatch)

    assert result is None
    assert memory.open_calls == 1
    assert memory.retain_calls == []
    assert redis.pushed == []
    assert handler.reply_fulfillment.pending == []


@pytest.mark.parametrize("state", ["restricted", "failed"])
async def test_retain_failure_after_admission_loss_never_stores_retry_context(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    from tests.group_admission.chat_admission_support import (
        ScriptedAdjudicate,
        install_scripted_adjudicate,
    )

    wired = _wire_entry(monkeypatch)
    admission = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, admission)
    memory = FakeHindsightMemory()

    async def _retain(_session: Any, _request: Any, _guard: Any) -> None:
        admission.set_state(state)
        raise HindsightUnavailableError("retain_failed")

    memory.retain_handler = _retain
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    decisions: list[dict[str, object]] = []
    handler._log_decision = decisions.append
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    result = await _run(handler, dispatch)

    assert result is None
    assert len(memory.retain_calls) == 1
    assert memory.sessions[0].retain_observations == [True]
    assert redis.pushed == []
    assert wired.generate_calls == []
    assert handler.reply_fulfillment.pending == []
    assert decisions[-1]["reply_action"] == "memory_failed"
    assert decisions[-1]["memory_failure_reason"] == "retain_failed"


@pytest.mark.parametrize("state", ["restricted", "failed"])
async def test_admission_loss_while_buffer_read_awaits_reports_not_stored(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    from tests.group_admission.chat_admission_support import (
        ScriptedAdjudicate,
        install_scripted_adjudicate,
    )

    wired = _wire_entry(monkeypatch)
    admission = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, admission)
    memory = FakeHindsightMemory()
    redis = _BlockingRedis()
    handler = _handler(memory, redis=redis)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))
    attempts: list[tuple[Any, bool, Any]] = []
    original_attempt = handler._attempt_reply

    async def _attempt(**kwargs: Any) -> Any:
        outcome = await original_attempt(**kwargs)
        attempts.append(outcome)
        return outcome

    monkeypatch.setattr(handler, "_attempt_reply", _attempt)

    task = asyncio.create_task(_run(handler, dispatch))
    await asyncio.wait_for(redis.read_started.wait(), timeout=2)
    admission.set_state(state)
    redis.read_release.set()
    result = await asyncio.wait_for(task, timeout=2)

    assert result is None
    assert attempts and attempts[0][1] is False
    assert redis.pushed == []
    assert len(memory.retain_calls) == 1
    assert wired.generate_calls == []
    assert handler.reply_fulfillment.pending == []


async def test_candidate_engine_fault_is_never_reported_as_normal_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    memory = FakeHindsightMemory(
        retain_error=HindsightUnavailableError("retain_failed")
    )
    handler = _handler(memory)
    decisions: list[dict[str, object]] = []
    handler._log_decision = decisions.append

    non_chat = _ScriptedDispatch(_request(), _result(route="none"))
    await _run(handler, non_chat)

    assert decisions[-1]["reply_action"] == "memory_failed"
    assert decisions[-1]["memory_failure_reason"] == "retain_failed"


async def test_unknown_exception_reason_and_text_never_reach_candidate_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "synthetic-private-body-not-a-fixed-code"

    class _ForeignError(RuntimeError):
        def __init__(self) -> None:
            self.reason_code = canary
            super().__init__(f"private body {canary}")

    _wire_entry(monkeypatch)
    memory = FakeHindsightMemory(retain_error=_ForeignError())
    redis = _Redis()
    handler = _handler(memory, redis=redis)
    decisions: list[dict[str, object]] = []
    handler._log_decision = decisions.append
    dispatch = _ScriptedDispatch(_request(), _result(route="none"))

    result = await _run(handler, dispatch)

    assert result is None
    assert len(redis.pushed) == 1
    assert decisions[-1]["reply_action"] == "memory_failed"
    assert decisions[-1]["memory_failure_reason"] == "memory_candidate_failed"
    rendered = json.dumps(decisions, ensure_ascii=False)
    assert canary not in rendered


async def test_candidate_evidence_never_fetches_extra_platform_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    handler = _handler(memory)
    bot = _Bot()
    reference = _reference(
        message_id="ref-1",
        sender_id="user-ref",
        text="引用原文",
    )
    request = _request(
        reference_ids=("ref-1",),
        known_references=(reference,),
    )
    dispatch = _ScriptedDispatch(
        request,
        _result(route="chat", force_reply=True),
    )

    pending = await handler.process_message(
        bot,  # type: ignore[arg-type]
        cast("Any", _Event()),  # type: ignore[arg-type]
        decision_dispatch=cast("Any", dispatch),
        reply_allowed=True,
    )

    assert pending is not None
    assert bot.calls == []
    document = _retained_document(memory.sessions[0])
    assert [node["message_id"] for node in document["candidate_evidence"]["nodes"]] == [
        "ref-1"
    ]


# ---------------------------------------------------------------------------
# Debug path and session plumbing
# ---------------------------------------------------------------------------


async def test_debug_recall_opens_session_but_never_retains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    memory = FakeHindsightMemory()
    handler = _handler(memory)

    result = await handler.generate_debug_reply(
        group_id=GROUP_ID,
        user_id=USER_ID,
        user_nickname="群友",
        content="调试",
    )

    assert result.reply == "回复正文"
    assert memory.open_calls == 1
    assert memory.retain_calls == []


# ---------------------------------------------------------------------------
# Real HindsightMemoryService with SDK boundary doubles
# ---------------------------------------------------------------------------


@dataclass
class _RetainBackend:
    bank_id: str = BANK_ID
    version: str = "1.1.0"
    document: object | None = None
    retain_response: Any = None
    retain_error: BaseException | None = None
    recall_error: BaseException | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    forbidden_calls: list[str] = field(default_factory=list)
    on_document: Any = None


class _FakeHindsightClient:
    def __init__(self, backend: _RetainBackend) -> None:
        self.backend = backend
        self.closed = 0

    async def aget_version(self) -> object:
        self.backend.calls.append(("version", {}))
        return SimpleNamespace(api_version=self.backend.version)

    async def arecall(
        self,
        *,
        bank_id: str,
        query: str,
        budget: str = "mid",
        max_tokens: int = 4096,
    ) -> RecallResponse:
        self.backend.calls.append(
            (
                "recall",
                {
                    "bank_id": bank_id,
                    "query": query,
                    "budget": budget,
                    "max_tokens": max_tokens,
                },
            )
        )
        if self.backend.recall_error is not None:
            raise self.backend.recall_error
        return RecallResponse.model_validate({"results": []})

    async def aretain_batch(self, **kwargs: Any) -> Any:
        self.backend.calls.append(("retain", dict(kwargs)))
        if self.backend.retain_error is not None:
            raise self.backend.retain_error
        if self.backend.retain_response is not None:
            return self.backend.retain_response
        return RetainResponse.model_validate(
            {
                "success": True,
                "bank_id": self.backend.bank_id,
                "items_count": 1,
                "async": True,
                "operation_id": kwargs["operation_id"],
            }
        )

    async def aclose(self) -> None:
        self.closed += 1

    @property
    def banks(self) -> Any:
        async def _get_bank_config(
            bank_id: str, *, _request_timeout: float | None = None
        ) -> object:
            self.backend.calls.append(
                ("bank", {"bank_id": bank_id, "timeout": _request_timeout})
            )
            return SimpleNamespace(bank_id=self.backend.bank_id)

        return SimpleNamespace(get_bank_config=_get_bank_config)

    @property
    def documents(self) -> Any:
        async def _get_document(
            bank_id: str,
            document_id: str,
            *,
            _request_timeout: float | None = None,
        ) -> object:
            self.backend.calls.append(
                (
                    "document",
                    {
                        "bank_id": bank_id,
                        "document_id": document_id,
                        "timeout": _request_timeout,
                    },
                )
            )
            if self.backend.on_document is not None:
                self.backend.on_document()
            if self.backend.document is None:
                raise NotFoundException(status=404)
            return self.backend.document

        return SimpleNamespace(get_document=_get_document)

    @property
    def operations(self) -> Any:
        async def _forbidden(*_args: object, **_kwargs: object) -> object:
            self.backend.forbidden_calls.append("operations")
            raise AssertionError("candidate retains must never poll operations")  # noqa: TRY003

        return SimpleNamespace(get_operation_status=_forbidden)


def _service_config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "chat_enabled": True,
        "hindsight_base_url": "https://hindsight.example.invalid",
        "hindsight_api_key": "secret-value",
        "hindsight_bank_id": BANK_ID,
        "hindsight_request_timeout_seconds": 0.5,
        "hindsight_recovery_interval_seconds": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _real_service(
    monkeypatch: pytest.MonkeyPatch,
    backend: _RetainBackend,
    *,
    config: SimpleNamespace | None = None,
) -> tuple[HindsightMemoryService, _FakeHindsightClient]:
    monkeypatch.setattr(
        hindsight_service,
        "Hindsight",
        lambda **_kwargs: _FakeHindsightClient(backend),
    )

    async def _config_getter() -> SimpleNamespace:
        return config or _service_config()

    service = HindsightMemoryService(config_getter=_config_getter)
    return service, _FakeHindsightClient(backend)


async def test_real_service_accepts_async_receipt_without_polling_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    backend = _RetainBackend()
    service, _client = _real_service(monkeypatch, backend)
    handler = _handler(service)
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    pending = await _run(handler, dispatch)

    assert pending is not None
    retain_calls = [call for call in backend.calls if call[0] == "retain"]
    assert len(retain_calls) == 1
    assert retain_calls[0][1]["bank_id"] == BANK_ID
    assert retain_calls[0][1]["retain_async"] is True
    assert backend.forbidden_calls == []


@pytest.mark.parametrize(
    ("backend_kwargs", "expected_code"),
    [
        (
            {
                "retain_response": RetainResponse.model_validate(
                    {
                        "success": True,
                        "bank_id": BANK_ID,
                        "items_count": 1,
                        "async": True,
                        "operation_id": "00000000-0000-0000-0000-000000000000",
                    }
                )
            },
            "response_invalid",
        ),
        ({"retain_error": httpx2.ConnectError("lost")}, "retain_failed"),
        ({"retain_error": TimeoutError()}, "request_timeout"),
    ],
)
async def test_real_service_invalid_lost_or_timeout_stops_generation_without_persistence(
    monkeypatch: pytest.MonkeyPatch,
    backend_kwargs: dict[str, Any],
    expected_code: str,
) -> None:
    _wire_entry(monkeypatch)
    backend = _RetainBackend(**backend_kwargs)
    service, _client = _real_service(monkeypatch, backend)
    redis = _Redis()
    handler = _handler(service, redis=redis)
    decisions: list[dict[str, object]] = []
    handler._log_decision = decisions.append
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    pending = await _run(handler, dispatch)

    assert pending is None
    assert len(redis.pushed) == 1
    assert decisions[-1]["reply_action"] == "memory_failed"
    assert decisions[-1]["memory_failure_reason"] == expected_code
    assert backend.forbidden_calls == []


async def test_real_service_group_revocation_during_preflight_blocks_actual_retain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.group_admission.chat_admission_support import (
        ScriptedAdjudicate,
        install_scripted_adjudicate,
    )

    _wire_entry(monkeypatch)
    admission = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, admission)
    backend = _RetainBackend()

    def _revoke_during_document_read() -> None:
        admission.set_state("restricted")

    backend.on_document = _revoke_during_document_read
    service, _client = _real_service(monkeypatch, backend)
    redis = _Redis()
    handler = _handler(service, redis=redis)
    decisions: list[dict[str, object]] = []
    handler._log_decision = decisions.append
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    pending = await _run(handler, dispatch)

    assert pending is None
    # Revocation is checked again immediately before the recent-context write;
    # a denied candidate must not leave a standalone retry copy behind.
    assert redis.pushed == []
    assert not [call for call in backend.calls if call[0] == "retain"]
    assert decisions[-1]["reply_action"] == "memory_failed"
    assert decisions[-1]["memory_failure_reason"] == "submission_not_admitted"


async def test_real_service_keeps_one_config_snapshot_across_candidate_and_recalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wired = _wire_entry(monkeypatch)
    backend = _RetainBackend()
    state = {"config": _service_config()}
    client = _FakeHindsightClient(backend)
    monkeypatch.setattr(hindsight_service, "Hindsight", lambda **_kwargs: client)

    async def _config_getter() -> SimpleNamespace:
        return state["config"]

    service = HindsightMemoryService(config_getter=_config_getter)
    handler = _handler(service)
    frozen_target = hindsight_service.derive_target_fingerprint(
        base_url=state["config"].hindsight_base_url,
        bank_id=BANK_ID,
    )

    def _rotate_during_candidate() -> None:
        state["config"] = _service_config(hindsight_bank_id="rotated-bank")

    backend.on_document = _rotate_during_candidate

    async def _generate(**kwargs: Any) -> Any:
        session = kwargs["recall_session"]
        await session.recall("tool recall")
        return import_module("komari_bot.plugins.komari_chat.services.chat_result").ReplyResult(
            content="回复正文",
            favorability_delta=0,
            favorability_reason="测试",
        )

    wired.service.generate = _generate  # type: ignore[method-assign]
    dispatch = _ScriptedDispatch(_request(), _result(route="chat", force_reply=True))

    pending = await _run(handler, dispatch)

    assert pending is not None
    assert state["config"].hindsight_bank_id == "rotated-bank"
    assert pending.hindsight_target_fingerprint == frozen_target
    assert pending.reply_result.hindsight_target_fingerprint == frozen_target
    recall_banks = [call[1]["bank_id"] for call in backend.calls if call[0] == "recall"]
    retain_banks = [call[1]["bank_id"] for call in backend.calls if call[0] == "retain"]
    assert recall_banks
    assert retain_banks == [BANK_ID]
    assert set(recall_banks) == {BANK_ID}
    assert wired.generate_calls == []


async def test_successful_new_message_recovers_without_replaying_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    attempts: list[str] = []

    async def _retain(
        _session: Any,
        request: Any,
        _guard: Any,
    ) -> None:
        attempts.append(request.source_id)
        if len(attempts) == 1:
            raise HindsightUnavailableError("retain_failed")

    memory = FakeHindsightMemory(retain_handler=_retain)
    redis = _Redis()
    handler = _handler(memory, redis=redis)

    failed = _ScriptedDispatch(
        _request(message_id="failed-1"), _result(route="chat", force_reply=True)
    )
    first = await _run(handler, failed)
    assert first is None
    assert len(redis.pushed) == 1

    recovered = _ScriptedDispatch(
        _request(message_id="ok-2"), _result(route="chat", force_reply=True)
    )
    second = await _run(handler, recovered)

    assert second is not None
    assert len(attempts) == 2
    assert attempts[0] != attempts[1]
    assert len(redis.pushed) == 2
    assert len(memory.sessions) == 2


# ---------------------------------------------------------------------------
# Real decision engine with an in-memory persistence adapter
# ---------------------------------------------------------------------------


class _FakeIngress:
    def __init__(self, repository: "_FakeDecisionRepository") -> None:
        self.repository = repository

    async def __aenter__(self) -> "_FakeIngress":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def reconcile(self) -> None:
        return None

    async def reserve(self, *, request_identity: EventIdentity, plan: Any) -> Any:
        from komari_bot.plugins.komari_decision.repositories.jev_repository import (
            Reservation,
            ReservationKind,
        )

        self.repository.reserve_calls.append((request_identity, plan.dispatch_id))
        existing = self.repository.reserved.get(request_identity)
        if existing is not None and existing != plan.dispatch_id:
            return Reservation(kind=ReservationKind.DUPLICATE)
        self.repository.reserved[request_identity] = plan.dispatch_id
        return Reservation(
            kind=ReservationKind.ENQUEUED,
            row_id=len(self.repository.reserved),
        )

    async def finish_reserved(
        self, *, request_identity: EventIdentity, plan: Any
    ) -> Any:
        from komari_bot.plugins.komari_decision.repositories.jev_repository import (
            Reservation,
            ReservationKind,
        )

        self.repository.reserved.setdefault(request_identity, plan.dispatch_id)
        return Reservation(
            kind=ReservationKind.TERMINAL,
            reason_code=plan.terminal_reason,
        )

    async def settlement_facts(self, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def revoke_unhanded_messages(self, **_kwargs: Any) -> None:
        return None


class _FakeDecisionRepository:
    def __init__(self, *, handoff_allowed: bool = True) -> None:
        self.ready = True
        self.last_observation = None
        self.last_observation_at = None
        self.handoff_allowed = handoff_allowed
        self.reserved: dict[EventIdentity, str] = {}
        self.reserve_calls: list[tuple[EventIdentity, str]] = []
        self.finalized: list[dict[str, Any]] = []
        self.handoffs: list[str] = []
        self.revocations: list[EventIdentity] = []
        self._ingress = _FakeIngress(self)

    @property
    def pseudonym_salt(self) -> bytes:
        return b"x" * 32

    @property
    def process_id(self) -> int:
        return 1

    def ingress(self) -> _FakeIngress:
        return self._ingress

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def check_lease(self) -> bool:
        return True

    async def is_current(self, **_kwargs: Any) -> bool:
        return True

    async def observe_owned_work(self, **_kwargs: Any) -> list[Any]:
        return []

    async def persist_revocation(
        self, *, identity: EventIdentity, **_kwargs: Any
    ) -> None:
        self.revocations.append(identity)

    async def wait_for_grant(self, **_kwargs: Any) -> Any:
        from komari_bot.plugins.komari_decision.repositories.jev_repository import (
            GrantOutcome,
        )

        return GrantOutcome(
            granted=True,
            state="running",
            queue_ms=0.0,
            granted_monotonic=time.monotonic(),
        )

    async def release_slot(self, **_kwargs: Any) -> None:
        return None

    async def finalize(self, **kwargs: Any) -> bool:
        self.finalized.append(dict(kwargs))
        return True

    async def claim_handoff(self, **kwargs: Any) -> bool:
        if not self.handoff_allowed:
            return False
        effect_check = kwargs.get("effect_check")
        if callable(effect_check) and not effect_check():
            return False
        self.handoffs.append(kwargs["route"])
        return True

    async def claim_notification(self, **_kwargs: Any) -> bool:
        return True

    async def mark_failure_notification(self, **_kwargs: Any) -> bool:
        return True


class _FakeRecent:
    async def freeze_for_target(self, **_kwargs: Any) -> tuple[Any, ...]:
        return ()

    async def provisional_ids(self, **_kwargs: Any) -> set[str]:
        return set()

    async def settle(self, **_kwargs: Any) -> None:
        return None

    async def resolve(self, **_kwargs: Any) -> None:
        return None

    async def observe_confirmed(self, **_kwargs: Any) -> None:
        return None


class _FakePromptLoader:
    async def get_versioned_snapshot_async(self) -> Any:
        return SimpleNamespace(
            revision=1,
            prompt_data={
                "role_description": "判定角色：你是群里的角色。",
                "reply_eligibility": "回应资格：明确呼叫必须回应，随意参与可选。",
                "retention_purpose": "保留用途：有交流作用的闲聊可以成为记忆候选。",
                "summary_recognition": "总结识别：当消息要求总结本群聊天时才算总结请求。",
            },
        )


class _FakeLogRepository:
    async def record(self, *_args: object, **_kwargs: object) -> None:
        return None


class _FakeAsyncRedis:
    async def aclose(self) -> None:
        return None


def _real_engine(
    *,
    responder: Any = None,
    handoff_allowed: bool = True,
    admission: Any = None,
    random_value: float = 0.99,
) -> tuple[Any, _FakeDecisionRepository, Any]:
    import nonebot.message as nonebot_message

    original_preprocessor = nonebot_message.event_preprocessor
    nonebot_message.event_preprocessor = lambda function: function
    try:
        from komari_bot.plugins.komari_decision.services.jev_engine import (
            JevDecisionEngine,
        )
        from komari_bot.plugins.komari_decision.services.jev_input import (
            DecisionPseudonymizer,
        )
    finally:
        nonebot_message.event_preprocessor = original_preprocessor
    from tests.komari_decision.jev_test_support import (
        ConfigSource,
        ScriptedTransport,
        build_answers,
    )

    transport = ScriptedTransport(
        responder
        or (
            lambda request, _index: build_answers(
                json.loads(request.content)["questions"]
            )
        )
    )
    repository = _FakeDecisionRepository(handoff_allowed=handoff_allowed)
    engine = JevDecisionEngine(
        config_source=ConfigSource({}),
        prompt_loader=cast("Any", _FakePromptLoader()),
        redis=cast("Any", object()),
        reference_reader=_no_reference,
        admission=admission or (lambda _identity: True),
        session_factory=cast("Any", lambda: None),
        transport_factory=lambda: transport,
        random_source=lambda: random_value,
    )
    engine._repository = repository  # type: ignore[assignment]
    engine._recent = _FakeRecent()  # type: ignore[assignment]
    engine._log_repository = _FakeLogRepository()  # type: ignore[assignment]
    engine._redis = _FakeAsyncRedis()  # type: ignore[assignment]
    engine._pseudonymizer = DecisionPseudonymizer(b"x" * 32)
    engine._initialized = True
    engine._leased = True
    return engine, repository, transport


async def _no_reference(
    _identity: EventIdentity, _message_id: str
) -> DecisionMessage | None:
    return None


def _real_handler(engine: Any, memory: Any, *, redis: _Redis | None = None) -> Any:
    handler = _handler(memory, redis=redis)
    handler.decision_engine = engine
    return handler


async def test_real_engine_retention_matrix_drives_candidate_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch, config=_chat_config(proactive_enabled=True))
    cases = [
        ("required", "candidate", 0.99, True, True),
        ("required", "noise", 0.99, True, True),
        ("optional", "candidate", 0.0, True, True),
        ("optional", "candidate", 0.99, False, True),
        ("silent", "candidate", 0.99, False, True),
        ("silent", "noise", 0.99, False, False),
    ]
    for reply, retention, random_value, chat_route, retain in cases:
        from tests.komari_decision.jev_test_support import build_answers

        engine, _repository, _transport = _real_engine(
            responder=lambda request,
            _index,
            reply=reply,
            retention=retention: build_answers(
                json.loads(request.content)["questions"],
                reply=reply,
                retention=retention,
            ),
            random_value=random_value,
        )
        memory = FakeHindsightMemory()
        redis = _Redis()
        handler = _real_handler(engine, memory, redis=redis)
        request = _request(message_id=f"matrix-{reply}-{retention}-{random_value}")
        dispatch = DecisionDispatch(request)

        pending = await handler.process_message(
            _Bot(),  # type: ignore[arg-type]
            _Event(message_id=request.event.message_id),  # type: ignore[arg-type]
            decision_dispatch=dispatch,
            reply_allowed=True,
        )

        if chat_route:
            assert pending is not None, (reply, retention, random_value)
            assert len(memory.sessions) == 1
            assert len(memory.retain_calls) == 1
            assert len(redis.pushed) == 1
        else:
            assert pending is None, (reply, retention, random_value)
            assert len(memory.sessions) == (1 if retain else 0)
            assert len(memory.retain_calls) == (1 if retain else 0)
            assert len(redis.pushed) == (1 if retain else 0)
        await engine.close()


async def test_real_engine_duplicate_event_has_no_memory_or_reply_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    engine, _repository, _transport = _real_engine()
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _real_handler(engine, memory, redis=redis)
    first = DecisionDispatch(_request(message_id="dup-1"))
    event = _Event(message_id="dup-1")
    await handler.process_message(
        _Bot(),  # type: ignore[arg-type]
        event,  # type: ignore[arg-type]
        decision_dispatch=first,
        reply_allowed=True,
    )
    assert len(memory.retain_calls) == 1
    assert len(redis.pushed) == 1

    second = DecisionDispatch(
        _request(message_id="dup-1", dispatch_id="dispatch-dup-2")
    )
    pending = await handler.process_message(
        _Bot(),  # type: ignore[arg-type]
        event,  # type: ignore[arg-type]
        decision_dispatch=second,
        reply_allowed=True,
    )

    assert pending is None
    assert len(memory.retain_calls) == 1
    assert len(redis.pushed) == 1
    await engine.close()


async def test_real_engine_decision_failure_stores_once_without_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    engine, _repository, _transport = _real_engine(
        responder=lambda _request, _index: (_ for _ in ()).throw(
            httpx2.ConnectError("down")
        )
    )
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _real_handler(engine, memory, redis=redis)
    dispatch = DecisionDispatch(_request(message_id="failed-decision"))

    pending = await handler.process_message(
        _Bot(),  # type: ignore[arg-type]
        _Event(message_id="failed-decision"),  # type: ignore[arg-type]
        decision_dispatch=dispatch,
        reply_allowed=True,
    )

    assert pending is None
    assert len(redis.pushed) == 1
    assert memory.open_calls == 0
    assert memory.retain_calls == []
    await engine.close()


async def test_real_engine_ban_retains_but_never_hands_off_or_generates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    engine, repository, _transport = _real_engine()
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _real_handler(engine, memory, redis=redis)
    dispatch = DecisionDispatch(_request(message_id="ban-1"))

    pending = await handler.process_message(
        _Bot(),  # type: ignore[arg-type]
        _Event(message_id="ban-1"),  # type: ignore[arg-type]
        decision_dispatch=dispatch,
        reply_allowed=False,
    )

    assert pending is None
    assert len(memory.retain_calls) == 1
    assert len(redis.pushed) == 1
    assert repository.handoffs == []
    assert handler.reply_fulfillment.pending == []
    await engine.close()


async def test_real_engine_refused_handoff_stops_before_memory_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_entry(monkeypatch)
    engine, repository, _transport = _real_engine(handoff_allowed=False)
    memory = FakeHindsightMemory()
    redis = _Redis()
    handler = _real_handler(engine, memory, redis=redis)
    dispatch = DecisionDispatch(_request(message_id="refused-handoff"))

    pending = await handler.process_message(
        _Bot(),  # type: ignore[arg-type]
        _Event(message_id="refused-handoff"),  # type: ignore[arg-type]
        decision_dispatch=dispatch,
        reply_allowed=True,
    )

    assert pending is None
    assert memory.open_calls == 0
    assert memory.retain_calls == []
    assert redis.pushed == []
    assert repository.handoffs == []
    await engine.close()
