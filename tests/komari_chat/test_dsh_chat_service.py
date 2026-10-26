"""TSK-203 Python DSH chat orchestration over a controlled host seam."""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.agent import (
    AgentHost,
    AgentHostError,
    AgentTaskResult,
    JsonValue,
    TaskKind,
)
from komari_bot.context.redis_store import MessageSchema
from komari_bot.plugins.komari_chat.services.dsh_chat_service import DshChatService
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext
from komari_bot.plugins.komari_memory import HindsightUnavailableError
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    FakeRecallSession,
    fake_recall_session,
    recall_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from hindsight_client import RecallResponse

    from komari_bot.plugins.komari_memory import HindsightRecallSession

service_module = import_module(
    "komari_bot.plugins.komari_chat.services.dsh_chat_service"
)


def _message(
    index: int,
    *,
    content: str = "消息",
    user_id: str | None = None,
    group_id: str = "g-1",
    is_bot: bool = False,
    timestamp: float | None = None,
) -> MessageSchema:
    return MessageSchema(
        user_id=user_id or f"u-{index}",
        user_nickname=f"用户{index}",
        group_id=group_id,
        content=content,
        timestamp=float(index) if timestamp is None else timestamp,
        message_id=f"m-{index}",
        is_bot=is_bot,
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        bot_nickname="小鞠",
        knowledge_enabled=True,
        knowledge_limit=3,
        agent_max_rounds=5,
        agent_max_tool_calls_per_round=3,
        agent_max_total_tool_calls=8,
    )


def _favorability() -> object:
    return SimpleNamespace(
        user_id="u-0",
        favorability=42,
        stage_index=1,
        stage_name="熟悉",
        stage_prompt="关系逐渐熟悉。",
    )


class _FakeHost:
    def __init__(
        self,
        result: Mapping[str, Any] | Exception,
        callback_sequence: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.result = result
        self.callback_sequence = callback_sequence
        self.requests: list[Any] = []
        self.callbacks: list[dict[str, Any]] = []

    async def execute(self, request: Any, callbacks: dict[str, Any]) -> AgentTaskResult:
        self.requests.append(request)
        self.callbacks.append(callbacks)
        if self.callback_sequence is not None:
            await self.callback_sequence(callbacks)
        if isinstance(self.result, Exception):
            raise self.result
        return AgentTaskResult(
            task_id=request.task_id,
            kind=request.kind,
            result=cast("Mapping[str, JsonValue]", self.result),
        )


async def _invoke(callbacks: dict[str, Any], name: str, data: dict[str, object]) -> dict[str, object]:
    request = SimpleNamespace(name=name, data=data)
    result = await callbacks[name](request)
    assert isinstance(result, dict)
    return result


def _service(
    host: _FakeHost,
    *,
    recall_response_value: RecallResponse | None = None,
    recall_error: HindsightUnavailableError | None = None,
    knowledge_search: Any = None,
    uid_search: Any = None,
    group_bindings: dict[str, str] | None = None,
) -> tuple[DshChatService, FakeHindsightMemory, FakeRecallSession]:
    memory = FakeHindsightMemory(
        response=recall_response_value or recall_response(),
        recall_error=recall_error,
    )
    session = fake_recall_session(memory)
    async def _default_knowledge(**_kwargs: object) -> list[object]:
        return []

    async def _default_uid(_uid: str) -> list[object]:
        return []

    service = DshChatService(
        host_getter=lambda: cast("AgentHost", host),
        knowledge_search=knowledge_search or _default_knowledge,
        uid_search=uid_search or _default_uid,
        favorability_getter=lambda _uid: _as_awaitable(_favorability()),
        group_bindings_getter=lambda _group: group_bindings or {},
    )
    return service, memory, session


def _as_awaitable(value: object) -> Any:
    async def _return() -> object:
        return value

    return _return()


def _generate(service: DshChatService, session: FakeRecallSession, context: ReplyContext | None = None, *, image_urls: list[str] | None = None) -> Any:
    return service.generate(
        message=_message(0, content="当前消息", user_id="u-0"),
        recent_messages=[_message(1), _message(2)],
        reply_context=context,
        recall_session=cast("HindsightRecallSession", session),
        request_trace_id="chat-1",
        config=_config(),
        caller_is_superuser=False,
        image_urls=image_urls,
    )


def test_generates_only_through_host_after_exactly_one_recall_and_verifies_result() -> None:
    async def sequence(callbacks: dict[str, Any]) -> None:
        admission = await _invoke(callbacks, "admission", {"purpose": "conversation"})
        assert admission == {"allow": True}
        first = await _invoke(callbacks, "chat.submit_final_response", {"content": "正文"})
        assert first["accepted"] is True
        assert first["missing"] == ["record_favorability_delta"]
        second = await _invoke(
            callbacks,
            "chat.submit_favorability",
            {"delta": 2, "reason": "开心"},
        )
        assert second["accepted"] is True
        assert second["result"] == {
            "content": "正文",
            "favorability_delta": 2,
            "favorability_reason": "开心",
        }

    host = _FakeHost(
        {
            "content": "正文",
            "favorability_delta": 2,
            "favorability_reason": "开心",
        },
        sequence,
    )
    service, memory, session = _service(host)

    result = asyncio.run(_generate(service, session))

    assert result.content == "正文"
    assert result.favorability_delta == 2
    assert result.favorability_reason == "开心"
    assert result.hindsight_target_fingerprint is None
    assert len(memory.recall_calls) == 1
    assert "当前消息" in memory.recall_calls[0]
    assert len(host.requests) == 1
    request = host.requests[0]
    assert request.kind is TaskKind.CHAT
    assert request.task_id == "chat-1"
    assert request.budget.to_protocol() == {
        "max_rounds": 5,
        "max_tool_calls_per_round": 3,
        "max_total_tool_calls": 8,
    }
    assert set(request.task_data) == {
        "schema",
        "current",
        "recent",
        "quote",
        "memory",
        "knowledge",
        "uid_knowledge",
        "facts",
    }
    assert request.task_data["current"]["content"] == "当前消息"
    assert "image_sources" not in str(request.task_data)
    assert set(host.callbacks[0]) == {
        "admission",
        "chat.submit_final_response",
        "chat.submit_favorability",
    }


def test_empty_redis_still_recalls_and_uses_media_only_marker() -> None:
    host = _FakeHost(
        {
            "content": "正文",
            "favorability_delta": 0,
            "favorability_reason": "平静",
        },
        lambda callbacks: _invoke_sequence(callbacks),
    )
    service, memory, session = _service(host)
    message = _message(0, content="   ", user_id="u-0")

    asyncio.run(
        service.generate(
            message=message,
            recent_messages=[],
            reply_context=None,
            recall_session=cast("HindsightRecallSession", session),
            request_trace_id="chat-empty",
            config=_config(),
            caller_is_superuser=False,
            image_urls=None,
        )
    )

    assert len(memory.recall_calls) == 1
    assert "当前交流" in memory.recall_calls[0]


async def _invoke_sequence(callbacks: dict[str, Any]) -> None:
    await _invoke(callbacks, "admission", {"purpose": "conversation"})
    await _invoke(callbacks, "chat.submit_final_response", {"content": "正文"})
    await _invoke(
        callbacks,
        "chat.submit_favorability",
        {"delta": 0, "reason": "平静"},
    )


def test_recall_failure_is_terminal_and_prevents_host_generation() -> None:
    host = _FakeHost({"content": "never"})
    service, memory, session = _service(
        host,
        recall_error=HindsightUnavailableError("recall_failed"),
    )

    with pytest.raises(HindsightUnavailableError):
        asyncio.run(_generate(service, session))

    assert len(memory.recall_calls) == 1
    assert host.requests == []


def test_raw_knowledge_and_independent_uid_lookup_preserve_separate_queries() -> None:
    knowledge_calls: list[dict[str, object]] = []
    uid_calls: list[str] = []

    async def knowledge(**kwargs: object) -> list[object]:
        knowledge_calls.append(dict(kwargs))
        return [SimpleNamespace(id=11, content="向量知识", source="vector")]

    async def uid(uid_value: str) -> list[object]:
        uid_calls.append(uid_value)
        return [SimpleNamespace(id=12, content=f"{uid_value} 的知识", source="keyword")]

    quote = ReplyContext(
        source_side="user",
        message_id="quoted-1",
        user_id="u-2",
        user_nickname="用户2",
        text="引用",
        image_sources=(),
        image_count=0,
        has_visible_image=False,
    )
    host = _FakeHost(
        {
            "content": "正文",
            "favorability_delta": 0,
            "favorability_reason": "平静",
        },
        lambda callbacks: _invoke_sequence(callbacks),
    )
    service, _memory, session = _service(host, knowledge_search=knowledge, uid_search=uid)

    asyncio.run(_generate(service, session, quote))

    assert knowledge_calls == [
        {
            "query": "当前消息",
            "limit": 3,
            "embedding_guard": knowledge_calls[0]["embedding_guard"],
            "raw_query": True,
        }
    ]
    assert uid_calls == ["u-1", "u-2", "u-0"]
    task_data = host.requests[0].task_data
    assert task_data["knowledge"][0]["source_id"] == "11"
    assert [item["source_id"] for item in task_data["uid_knowledge"]] == [
        "uid:u-1:12",
        "uid:u-2:12",
        "uid:u-0:12",
    ]


def test_uid_enrichment_remains_independent_when_vector_knowledge_is_disabled() -> None:
    uid_calls: list[str] = []

    async def uid(uid_value: str) -> list[object]:
        uid_calls.append(uid_value)
        return []

    host = _FakeHost(
        {
            "content": "正文",
            "favorability_delta": 0,
            "favorability_reason": "平静",
        },
        lambda callbacks: _invoke_sequence(callbacks),
    )
    service, _memory, session = _service(host, uid_search=uid)
    config = _config()
    config.knowledge_enabled = False

    asyncio.run(
        service.generate(
            message=_message(0, content="当前消息", user_id="u-0"),
            recent_messages=[_message(1)],
            reply_context=None,
            recall_session=cast("HindsightRecallSession", session),
            request_trace_id="chat-uid",
            config=config,
            caller_is_superuser=False,
            image_urls=None,
        )
    )

    assert uid_calls == ["u-1", "u-0"]
    assert host.requests[0].task_data["knowledge"] == []


@pytest.mark.parametrize(
    "context",
    [
        ReplyContext(
            source_side="user",
            message_id="quoted-1",
            user_id="u-2",
            user_nickname="用户2",
            text="看这个",
            image_sources=("https://example.invalid/secret-image.png",),
            image_count=1,
            has_visible_image=True,
        ),
    ],
)
def test_image_seams_fail_closed_without_retaining_urls(context: ReplyContext) -> None:
    host = _FakeHost({"content": "never"})
    service, memory, session = _service(host)

    with pytest.raises(service_module.ImageTransportUnavailableError) as exc_info:
        asyncio.run(_generate(service, session, context))

    assert exc_info.value.code == "image_transport_unavailable"
    assert "example.invalid" not in str(exc_info.value)
    # TSK-203 never skips the exactly-once automatic recall for an image-only
    # trigger, but stops at the unmigrated image seam before DSH generation.
    assert len(memory.recall_calls) == 1
    assert "看这个" in memory.recall_calls[0]
    assert host.requests == []


def test_current_image_also_fails_closed_after_the_single_recall() -> None:
    host = _FakeHost({"content": "never"})
    service, memory, session = _service(host)

    with pytest.raises(service_module.ImageTransportUnavailableError):
        asyncio.run(
            _generate(service, session, image_urls=["https://example.invalid/a.png"])
        )
    assert len(memory.recall_calls) == 1
    assert "当前消息" in memory.recall_calls[0]
    assert host.requests == []


def test_host_task_error_is_mapped_to_a_safe_fixed_code() -> None:
    from komari_bot.agent import AgentTaskError

    host = _FakeHost(AgentTaskError("incomplete_result", "safe"))
    service, _memory, session = _service(host)

    with pytest.raises(AgentHostError) as exc_info:
        asyncio.run(_generate(service, session))

    assert exc_info.value.code == "incomplete_result"
    assert "当前消息" not in str(exc_info.value)


def test_fabricated_host_result_is_rejected_against_python_candidate() -> None:
    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "admission", {"purpose": "conversation"})
        await _invoke(callbacks, "chat.submit_final_response", {"content": "真实正文"})
        await _invoke(
            callbacks,
            "chat.submit_favorability",
            {"delta": 0, "reason": "平静"},
        )

    host = _FakeHost(
        {
            "content": "伪造正文",
            "favorability_delta": 0,
            "favorability_reason": "平静",
        },
        sequence,
    )
    service, _memory, session = _service(host)

    with pytest.raises(service_module.ChatResultMismatchError):
        asyncio.run(_generate(service, session))


def test_group_admission_is_rechecked_immediately_before_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost({"content": "never"})
    service, memory, session = _service(host)
    decisions = iter((True, False))
    monkeypatch.setattr(
        service_module,
        "effect_business_admitted",
        lambda **_kwargs: next(decisions),
    )

    result = asyncio.run(_generate(service, session))

    assert result.content == ""
    assert memory.recall_calls == []
    assert host.requests == []


def test_group_admission_denied_before_session_has_no_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _FakeHost({"content": "never"})
    service, memory, session = _service(host)
    monkeypatch.setattr(
        service_module,
        "effect_business_admitted",
        lambda **_kwargs: False,
    )

    result = asyncio.run(_generate(service, session))

    assert result.content == ""
    assert memory.recall_calls == []
    assert host.requests == []


def test_config_cap_budget_and_knowledge_are_frozen_before_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"cap": 3}
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=state["cap"]),
    )
    knowledge_calls: list[dict[str, object]] = []

    async def knowledge(**kwargs: object) -> list[object]:
        knowledge_calls.append(dict(kwargs))
        return []

    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "chat.submit_final_response", {"content": "正文"})
        await _invoke(
            callbacks,
            "chat.submit_favorability",
            {"delta": 3, "reason": "冻结配置"},
        )

    config = _config()
    host = _FakeHost(
        {
            "content": "正文",
            "favorability_delta": 3,
            "favorability_reason": "冻结配置",
        },
        sequence,
    )
    service, memory, session = _service(host, knowledge_search=knowledge)
    original_recall = session.recall

    async def _mutating_recall(query: str) -> Any:
        response = await original_recall(query)
        state["cap"] = 1
        config.agent_max_rounds = 2
        config.agent_max_tool_calls_per_round = 1
        config.agent_max_total_tool_calls = 2
        config.knowledge_limit = 1
        return response

    session.recall = _mutating_recall  # type: ignore[method-assign]

    result = asyncio.run(_generate(service, session))

    assert result.favorability_delta == 3
    assert host.requests[0].budget.to_protocol() == {
        "max_rounds": 5,
        "max_tool_calls_per_round": 3,
        "max_total_tool_calls": 8,
    }
    assert host.requests[0].task_data["facts"]["max_favorability_delta"] == 3
    assert knowledge_calls[0]["limit"] == 3
    assert memory.recall_calls


def test_submission_admission_denied_before_first_submission_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = iter((True, True, False))
    monkeypatch.setattr(
        service_module,
        "effect_business_admitted",
        lambda **_kwargs: next(decisions),
    )

    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "chat.submit_final_response", {"content": "不应提交"})

    host = _FakeHost(
        {
            "content": "不应提交",
            "favorability_delta": 0,
            "favorability_reason": "无",
        },
        sequence,
    )
    service, memory, session = _service(host)

    with pytest.raises(service_module.ChatSubmissionAdmissionError):
        asyncio.run(_generate(service, session))

    assert memory.retain_calls == []
    assert host.requests


def test_submission_admission_denied_between_valid_submissions_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = iter((True, True, True, False))
    monkeypatch.setattr(
        service_module,
        "effect_business_admitted",
        lambda **_kwargs: next(decisions),
    )

    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "chat.submit_final_response", {"content": "先提交"})
        await _invoke(
            callbacks,
            "chat.submit_favorability",
            {"delta": 1, "reason": "后提交"},
        )

    host = _FakeHost(
        {
            "content": "先提交",
            "favorability_delta": 1,
            "favorability_reason": "后提交",
        },
        sequence,
    )
    service, memory, session = _service(host)

    with pytest.raises(service_module.ChatSubmissionAdmissionError):
        asyncio.run(_generate(service, session))

    assert memory.retain_calls == []
    assert host.requests


def test_mandatory_quote_identity_budget_failure_never_calls_recall() -> None:
    host = _FakeHost({"content": "never"})
    service, memory, session = _service(host)
    quote = ReplyContext(
        source_side="user",
        message_id="quoted-1",
        user_id="u-1",
        user_nickname="用户1",
        text="引用",
        image_sources=(),
        image_count=0,
        has_visible_image=False,
    )

    with pytest.raises(service_module.RecallQueryBudgetError):
        asyncio.run(
            service.generate(
                message=_message(0, content="触" * 400, user_id="u-0"),
                recent_messages=[],
                reply_context=quote,
                recall_session=cast("HindsightRecallSession", session),
                request_trace_id="chat-budget-quote",
                config=_config(),
            )
        )

    assert memory.recall_calls == []
    assert host.requests == []


def test_twenty_selected_model_rows_survive_while_recall_uses_ten() -> None:
    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke_sequence(callbacks)

    host = _FakeHost(
        {
            "content": "正文",
            "favorability_delta": 0,
            "favorability_reason": "平静",
        },
        sequence,
    )
    service, memory, session = _service(host)
    recent = [_message(index, content=f"消息{index}") for index in range(1, 21)]

    asyncio.run(
        service.generate(
            message=_message(100, content="当前消息", user_id="u-0"),
            recent_messages=recent,
            reply_context=None,
            recall_session=cast("HindsightRecallSession", session),
            request_trace_id="chat-context-20",
            config=_config(),
        )
    )

    task_data = host.requests[0].task_data
    assert len(task_data["recent"]) == 20
    assert [row["message_id"] for row in task_data["recent"]] == [
        f"m-{index}" for index in range(1, 21)
    ]
    assert len(memory.recall_calls) == 1
    query = memory.recall_calls[0]
    assert query.count("[history ") <= 10
    assert "消息1\n" not in query
    assert "消息20" in query
