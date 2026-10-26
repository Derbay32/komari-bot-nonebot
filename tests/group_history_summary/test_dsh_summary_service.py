"""TSK-206 Python DSH 总结编排与可信回调。"""

from __future__ import annotations

import asyncio
from collections.abc import (  # noqa: TC003 - runtime aliases in test fakes
    Callable,
    Mapping,
)
from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.agent import (
    AgentTaskError,
    AgentTaskResult,
    JsonValue,
    TaskKind,
)
from komari_bot.plugins.group_history_summary.admission_gate import (
    SummaryAdmissionDeniedError,
)
from komari_bot.plugins.group_history_summary.dsh_summary_service import (
    DshSummaryService,
    SummaryGenerationError,
)
from komari_bot.plugins.group_history_summary.history_service import (
    HistoryFetchMetadata,
    HistoryMessage,
)
from komari_bot.plugins.group_history_summary.history_tools import (
    RECENT_SOURCE,
    SummaryToolResult,
)
from komari_bot.plugins.group_history_summary.summary_candidate import (
    SummaryResultMismatchError,
    SummaryStaleGenerationError,
)
from komari_bot.plugins.group_history_summary.summary_task_data import (
    FrozenSummaryTaskConfig,
    SummaryLimits,
    SummaryTaskBudget,
)

GROUP_ID = "123"
BOT_ID = "999"


def _frozen() -> FrozenSummaryTaskConfig:
    return FrozenSummaryTaskConfig(
        budget=SummaryTaskBudget(rounds=4, per_round=1, total=4),
        limits=SummaryLimits(
            default_count=30,
            min_count=10,
            max_count=200,
            scan_limit=300,
        ),
        fetch_batch_size=50,
        history_min_coverage_ratio=0.8,
        layout_params={"canvas_width": 1365},
        redis_db=0,
        lock_ttl_seconds=300,
    )


def _message(seq: int, *, content: str | None = None) -> HistoryMessage:
    return HistoryMessage(
        user_id="1001",
        nickname="阿明",
        content=f"消息{seq}" if content is None else content,
        timestamp=seq,
        message_seq=seq,
        message_id=f"m{seq}",
        reply_to_message_id=None,
    )


def _selection(
    messages: list[HistoryMessage],
    *,
    source: str = RECENT_SOURCE,
    metadata: HistoryFetchMetadata | None = None,
) -> SummaryToolResult:
    return SummaryToolResult(
        source=source,
        matched_count=len(messages),
        messages=messages,
        filters={"count": 10},
        history_fetch=metadata,
    )


class _FakeProvider:
    def __init__(self) -> None:
        self.recent = _selection([_message(1), _message(2)])
        self.user = _selection([_message(3)], source="messages_by_user")
        self.topic = _selection([_message(4)], source="messages_by_topic")
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.failure: Exception | None = None

    async def fetch_recent(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        self.calls.append(("recent", arguments))
        if self.failure is not None:
            raise self.failure
        return self.recent

    async def fetch_user(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        self.calls.append(("user", arguments))
        if self.failure is not None:
            raise self.failure
        return self.user

    async def fetch_topic(self, arguments: Mapping[str, object]) -> SummaryToolResult:
        self.calls.append(("topic", arguments))
        if self.failure is not None:
            raise self.failure
        return self.topic


class _FakeHost:
    def __init__(
        self,
        sequence: Callable[[dict[str, Any]], Any],
        *,
        result: Mapping[str, Any] | Exception | None = None,
    ) -> None:
        self.sequence = sequence
        self.result = result
        self.requests: list[Any] = []
        self.callbacks: list[dict[str, Any]] = []

    async def execute(self, request: Any, callbacks: dict[str, Any]) -> AgentTaskResult:
        self.requests.append(request)
        self.callbacks.append(callbacks)
        await self.sequence(callbacks)
        if isinstance(self.result, Exception):
            raise self.result
        value = self.result if self.result is not None else {"summary_text": "总结完成"}
        return AgentTaskResult(
            task_id=request.task_id,
            kind=request.kind,
            result=cast("Mapping[str, JsonValue]", value),
        )


async def _invoke(
    callbacks: dict[str, Any],
    name: str,
    data: Mapping[str, object],
) -> dict[str, Any]:
    request = SimpleNamespace(name=name, data=data)
    result = await callbacks[name](request)
    assert isinstance(result, dict)
    return result


def _service(
    host: _FakeHost,
    provider: _FakeProvider,
) -> DshSummaryService:
    def _factory(
        _bot: object,
        _group_id: str,
        _bot_self_id: str,
        _config: FrozenSummaryTaskConfig,
    ) -> _FakeProvider:
        return provider

    return DshSummaryService(
        host_getter=lambda: cast("Any", host),
        history_provider_factory=_factory,
        admission_checker=lambda _group_id: True,
    )


async def _generate(service: DshSummaryService) -> Any:
    return await service.generate(
        bot=SimpleNamespace(),
        group_id=GROUP_ID,
        bot_self_id=BOT_ID,
        user_request="总结最近消息",
        request_trace_id="group-summary-test",
        config=_frozen(),
    )


@pytest.mark.asyncio
async def test_task_data_budget_and_callback_names_are_exact() -> None:
    provider = _FakeProvider()

    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "admission", {"purpose": "conversation", "attempt": 1})
        await _invoke(callbacks, "summary.fetch_recent", {"count": 10})
        await _invoke(callbacks, "summary.final_summary", {"content": "真实总结"})

    host = _FakeHost(sequence, result={"summary_text": "真实总结"})
    generation = await _generate(_service(host, provider))

    assert generation.summary_text == "真实总结"
    assert generation.selection is not None
    request = host.requests[0]
    assert request.kind is TaskKind.SUMMARY
    assert request.task_id == "group-summary-test"
    assert request.task_data == {
        "schema": 1,
        "request": {
            "source_type": "group_history",
            "source_id": "group-summary-test",
            "trust_level": "untrusted",
            "content": "总结最近消息",
        },
        "limits": {
            "default_count": 30,
            "min_count": 10,
            "max_count": 200,
            "scan_limit": 300,
        },
    }
    assert request.budget.to_protocol() == {
        "max_rounds": 4,
        "max_tool_calls_per_round": 1,
        "max_total_tool_calls": 4,
    }
    assert set(host.callbacks[0]) == {
        "admission",
        "summary.fetch_recent",
        "summary.fetch_user",
        "summary.fetch_topic",
        "summary.history_page",
        "summary.final_summary",
    }
    assert provider.calls == [("recent", {"count": 10})]


@pytest.mark.asyncio
async def test_history_page_is_not_a_model_tool_and_stale_generation_is_rejected() -> None:
    provider = _FakeProvider()

    async def sequence(callbacks: dict[str, Any]) -> None:
        first = await _invoke(callbacks, "summary.fetch_recent", {"count": 10})
        generation = first["generation"]
        page = await _invoke(
            callbacks,
            "summary.history_page",
            {"generation": generation, "offset": 0},
        )
        assert page["total"] == 2
        assert page["rows"][0]["message_id"] == "m1"
        await _invoke(callbacks, "summary.fetch_user", {"count": 10})
        with pytest.raises(SummaryStaleGenerationError):
            await _invoke(
                callbacks,
                "summary.history_page",
                {"generation": generation, "offset": 0},
            )
        await _invoke(callbacks, "summary.final_summary", {"content": "总结"})

    host = _FakeHost(sequence, result={"summary_text": "总结"})
    generation = await _generate(_service(host, provider))

    assert generation.selection is not None
    assert generation.selection.source == "messages_by_user"


@pytest.mark.asyncio
async def test_empty_read_is_valid_but_final_is_still_required() -> None:
    provider = _FakeProvider()
    provider.recent = _selection([])

    async def sequence(callbacks: dict[str, Any]) -> None:
        early = await _invoke(callbacks, "summary.final_summary", {"content": "太早"})
        assert early["code"] == "history_required"
        assert early["missing"] == ["history_read", "final_summary"]
        read = await _invoke(callbacks, "summary.fetch_recent", {"count": 10})
        assert read["selected_count"] == 0
        page = await _invoke(
            callbacks,
            "summary.history_page",
            {"generation": read["generation"], "offset": 0},
        )
        assert page["total"] == 0
        assert page["rows"] == []
        final = await _invoke(callbacks, "summary.final_summary", {"content": "空历史"})
        assert final == {
            "accepted": True,
            "code": None,
            "missing": [],
            "result": {"summary_text": "空历史"},
        }

    host = _FakeHost(sequence, result={"summary_text": "空历史"})
    generation = await _generate(_service(host, provider))

    assert generation.selection is not None
    assert generation.selection.messages == ()
    assert generation.summary_text == "空历史"


@pytest.mark.asyncio
async def test_malformed_history_arguments_are_correctable_before_read() -> None:
    provider = _FakeProvider()

    async def sequence(callbacks: dict[str, Any]) -> None:
        for name, payload in (
            ("summary.fetch_recent", {"count": True}),
            ("summary.fetch_recent", {"count": 10, "extra": 1}),
            ("summary.fetch_user", {"count": 10, "user_id": 1}),
            ("summary.fetch_topic", {"count": 10, "keywords": "alpha"}),
            ("summary.fetch_topic", {"count": 10, "keywords": [1]}),
        ):
            receipt = await _invoke(callbacks, name, payload)
            assert receipt == {
                "error": "invalid_arguments",
                "generation": 0,
            }
        assert provider.calls == []

        read = await _invoke(callbacks, "summary.fetch_recent", {"count": 10})
        rejected = await _invoke(
            callbacks,
            "summary.fetch_recent",
            {"count": 10, "extra": True},
        )
        assert rejected["generation"] == read["generation"]
        accepted = await _invoke(callbacks, "summary.final_summary", {"content": "保持选择"})
        assert accepted["accepted"] is True

    host = _FakeHost(sequence, result={"summary_text": "保持选择"})
    await _generate(_service(host, provider))

    assert provider.calls == [("recent", {"count": 10})]


@pytest.mark.asyncio
async def test_terminal_history_failure_propagates() -> None:
    provider = _FakeProvider()
    provider.failure = RuntimeError("platform failed")

    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "summary.fetch_recent", {"count": 10})

    host = _FakeHost(sequence, result={"summary_text": "never"})
    with pytest.raises(RuntimeError, match="platform failed"):
        await _generate(_service(host, provider))


@pytest.mark.asyncio
async def test_model_request_failure_is_terminal() -> None:
    provider = _FakeProvider()

    async def sequence(_callbacks: dict[str, Any]) -> None:
        return None

    host = _FakeHost(sequence, result=AgentTaskError("agent_error", "provider failed"))
    with pytest.raises(SummaryGenerationError) as exc_info:
        await _generate(_service(host, provider))

    assert exc_info.value.code == "agent_error"


@pytest.mark.asyncio
async def test_admission_denial_is_normal_summary_control_flow() -> None:
    provider = _FakeProvider()
    host = _FakeHost(lambda _callbacks: None, result=AgentTaskError("agent_error"))

    def _deny(_group_id: str) -> bool:
        return False

    service = DshSummaryService(
        host_getter=lambda: cast("Any", host),
        history_provider_factory=lambda *_args: provider,
        admission_checker=_deny,
    )

    async def sequence(callbacks: dict[str, Any]) -> None:
        decision = await _invoke(callbacks, "admission", {"purpose": "conversation"})
        assert decision == {"allow": False}

    host.sequence = sequence
    with pytest.raises(SummaryAdmissionDeniedError):
        await _generate(service)


@pytest.mark.asyncio
async def test_page_admission_denial_is_normal_summary_control_flow() -> None:
    provider = _FakeProvider()

    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "summary.fetch_recent", {"count": 10})
        await _invoke(callbacks, "summary.history_page", {"generation": 1, "offset": 0})

    host = _FakeHost(sequence, result=AgentTaskError("callback_failed"))
    service = DshSummaryService(
        host_getter=lambda: cast("Any", host),
        history_provider_factory=lambda *_args: provider,
        admission_checker=lambda _group_id: False,
    )
    with pytest.raises(SummaryAdmissionDeniedError):
        await _generate(service)


@pytest.mark.asyncio
async def test_host_result_must_match_python_candidate() -> None:
    provider = _FakeProvider()

    async def sequence(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "summary.fetch_recent", {"count": 10})
        await _invoke(callbacks, "summary.final_summary", {"content": "真实总结"})

    host = _FakeHost(sequence, result={"summary_text": "伪造总结"})
    with pytest.raises(SummaryResultMismatchError):
        await _generate(_service(host, provider))


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_swallowed() -> None:
    provider = _FakeProvider()
    started = asyncio.Event()

    async def sequence(_callbacks: dict[str, Any]) -> None:
        started.set()
        await asyncio.Future()

    host = _FakeHost(sequence)
    task = asyncio.create_task(_generate(_service(host, provider)))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_invalid_trace_id_fails_before_host_task() -> None:
    provider = _FakeProvider()
    host = _FakeHost(lambda _callbacks: None)
    service = _service(host, provider)

    with pytest.raises(SummaryGenerationError):
        await service.generate(
            bot=SimpleNamespace(),
            group_id=GROUP_ID,
            bot_self_id=BOT_ID,
            user_request="总结",
            request_trace_id="bad id with spaces",
            config=_frozen(),
        )
    assert host.requests == []
