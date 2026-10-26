"""TSK-204 closed chat query-tool callbacks and trusted validation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.agent import AgentCallbackRequest
from komari_bot.plugins.komari_chat.services.chat_query_tools import (
    ChatQueryAdmissionError,
    ChatQueryTools,
)
from komari_bot.plugins.komari_chat.services.chat_task_data import (
    ChatQueryCapabilities,
)
from komari_bot.plugins.komari_memory import HindsightUnavailableError
from tests.komari_chat.hindsight_recall_support import (
    FakeHindsightMemory,
    fake_recall_session,
    recall_response,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


pytestmark = pytest.mark.asyncio


def _request(name: str, data: Mapping[str, object]) -> AgentCallbackRequest:
    return AgentCallbackRequest(
        name=name,
        data=cast("Any", dict(data)),
        task=cast("Any", SimpleNamespace()),
    )


def _callbacks(tools: ChatQueryTools) -> dict[str, Any]:
    return cast("dict[str, Any]", tools.callbacks())


def _tools(
    *,
    capabilities: ChatQueryCapabilities | None = None,
    recall_response_value: Any | None = None,
    recall_error: BaseException | None = None,
    knowledge: Any = None,
    search: Any = None,
    fetch: Any = None,
    admitted: Any = None,
) -> tuple[ChatQueryTools, FakeHindsightMemory, list[str], list[dict[str, object]]]:
    memory = FakeHindsightMemory(
        response=recall_response_value or recall_response(),
        recall_error=cast("Any", recall_error),
    )
    session = fake_recall_session(memory)
    calls: list[str] = []
    knowledge_calls: list[dict[str, object]] = []

    async def default_knowledge(**kwargs: object) -> list[object]:
        knowledge_calls.append(dict(kwargs))
        return []

    async def default_search(query: str, *, request_trace_id: str | None = None) -> str:
        calls.append(f"search:{query}:{request_trace_id}")
        return "搜索正文"

    async def default_fetch(urls: list[str], *, request_trace_id: str | None = None) -> str:
        calls.append(f"fetch:{','.join(urls)}:{request_trace_id}")
        return "抓取正文"

    tools = ChatQueryTools(
        group_id="g-1",
        request_trace_id="trace-1",
        knowledge_limit=3,
        capabilities=capabilities or ChatQueryCapabilities(
            knowledge=True,
            search=True,
            fetch=True,
        ),
        recall_session=cast("Any", session),
        admission=admitted or (lambda: True),
        knowledge_search=knowledge or default_knowledge,
        search_web=search or default_search,
        fetch_page=fetch or default_fetch,
    )
    return tools, memory, calls, knowledge_calls


async def test_recall_uses_the_same_session_and_returns_closed_structured_data() -> None:
    tools, memory, _calls, _knowledge_calls = _tools(
        recall_response_value=recall_response(
            {"id": "mem-1", "text": "真实记忆"},
            {"id": "mem-2", "text": ""},
        )
    )

    payload = await _callbacks(tools)["chat.recall_memory"](
        _request("chat.recall_memory", {"query": "过去"})
    )

    assert memory.recall_calls == ["过去"]
    assert payload == {
        "ok": True,
        "result": {
            "source_type": "memory",
            "items": [
                {"source_id": "mem-1", "content": "真实记忆"},
                {"source_id": "mem-2", "content": ""},
            ],
            "truncated": False,
            "omitted_items": 0,
        },
    }


async def test_empty_recall_is_a_successful_empty_result() -> None:
    tools, _memory, _calls, _knowledge_calls = _tools(
        recall_response_value=recall_response()
    )

    payload = await _callbacks(tools)["chat.recall_memory"](
        _request("chat.recall_memory", {"query": "nothing"})
    )

    assert payload["ok"] is True
    assert payload["result"]["items"] == []
    assert payload["result"]["truncated"] is False


async def test_hindsight_failure_stays_an_exception() -> None:
    failure = HindsightUnavailableError("recall_failed")
    tools, _memory, _calls, _knowledge_calls = _tools(recall_error=failure)

    with pytest.raises(HindsightUnavailableError) as captured:
        await _callbacks(tools)["chat.recall_memory"](
            _request("chat.recall_memory", {"query": "failure"})
        )

    assert captured.value.reason_code == "recall_failed"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("chat.recall_memory", {"query": "x", "bank_id": "forged"}),
        ("chat.recall_memory", {"query": 1}),
        ("chat.recall_memory", {"query": "   "}),
        ("chat.search_knowledge", {"query": "x", "limit": 10}),
        ("chat.search_web", {"query": "x", "provider": "forged"}),
        ("chat.fetch_page", {"urls": ["https://example.com"], "limit": 1}),
        ("chat.fetch_page", {"urls": []}),
        ("chat.fetch_page", {"urls": ["https://example.com", 1]}),
        ("chat.fetch_page", {"urls": ["not a url"]}),
        ("chat.fetch_page", {"urls": ["https://[invalid"]}),
        ("chat.fetch_page", {"urls": ["https://example.com\uff0fevil"]}),
        (
            "chat.fetch_page",
            {"urls": [f"https://example.com/{index}" for index in range(6)]},
        ),
    ],
)
async def test_closed_model_arguments_are_correctable_without_backend_calls(
    name: str,
    data: dict[str, object],
) -> None:
    tools, memory, calls, knowledge_calls = _tools()

    payload = await _callbacks(tools)[name](_request(name, data))

    assert payload == {"ok": False, "code": "invalid_arguments"}
    assert memory.recall_calls == []
    assert knowledge_calls == []
    assert calls == []


async def test_query_validation_does_not_silently_truncate_over_budget_input() -> None:
    tools, memory, _calls, _knowledge_calls = _tools()

    payload = await _callbacks(tools)["chat.recall_memory"](
        _request("chat.recall_memory", {"query": "x" * 513})
    )

    assert payload == {"ok": False, "code": "invalid_arguments"}
    assert memory.recall_calls == []


async def test_search_web_keeps_its_two_hundred_character_model_limit() -> None:
    tools, _memory, calls, _knowledge_calls = _tools()

    accepted = await _callbacks(tools)["chat.search_web"](
        _request("chat.search_web", {"query": "x" * 200})
    )
    rejected = await _callbacks(tools)["chat.search_web"](
        _request("chat.search_web", {"query": "x" * 201})
    )

    assert accepted["ok"] is True
    assert rejected == {"ok": False, "code": "invalid_arguments"}
    assert calls == ["search:" + "x" * 200 + ":trace-1"]


async def test_knowledge_uses_the_frozen_limit_raw_query_and_embedding_guard() -> None:
    guard_values: list[bool] = []

    async def knowledge(**kwargs: object) -> list[object]:
        guard = kwargs["embedding_guard"]
        assert callable(guard)
        guard_values.append(bool(guard()))
        return [SimpleNamespace(id=11, content="知识正文", source="vector")]

    tools, _memory, _calls, _knowledge_calls = _tools(knowledge=knowledge)

    payload = await _callbacks(tools)["chat.search_knowledge"](
        _request("chat.search_knowledge", {"query": "原文你"})
    )

    assert payload == {
        "ok": True,
        "result": {
            "source_type": "knowledge",
            "items": [{"source_id": "11", "content": "知识正文"}],
            "truncated": False,
            "omitted_items": 0,
        },
    }
    assert guard_values == [True]


async def test_knowledge_result_without_identifier_gets_an_explicit_synthetic_id() -> None:
    async def knowledge(**_kwargs: object) -> list[object]:
        return [SimpleNamespace(id=None, content="无标识知识", source="keyword")]

    tools, _memory, _calls, _knowledge_calls = _tools(knowledge=knowledge)

    payload = await _callbacks(tools)["chat.search_knowledge"](
        _request("chat.search_knowledge", {"query": "query"})
    )

    assert payload["result"]["items"] == [
        {"source_id": "knowledge:index:0", "content": "无标识知识"}
    ]


async def test_web_results_preserve_business_error_text_as_untrusted_data() -> None:
    async def search(_query: str, *, request_trace_id: str | None = None) -> str:
        assert request_trace_id == "trace-1"
        return "[搜索失败：UPSTREAM_ERROR]"

    async def fetch(_urls: list[str], *, request_trace_id: str | None = None) -> str:
        assert request_trace_id == "trace-1"
        return "[抓取失败：DISABLED]"

    tools, _memory, _calls, _knowledge_calls = _tools(search=search, fetch=fetch)

    search_payload = await _callbacks(tools)["chat.search_web"](
        _request("chat.search_web", {"query": "query"})
    )
    fetch_payload = await _callbacks(tools)["chat.fetch_page"](
        _request("chat.fetch_page", {"urls": ["https://example.com/a"]})
    )

    assert search_payload["result"]["source_type"] == "web"
    assert search_payload["result"]["items"][0]["content"] == "[搜索失败：UPSTREAM_ERROR]"
    assert fetch_payload["result"]["source_type"] == "web"
    assert fetch_payload["result"]["items"][0]["content"] == "[抓取失败：DISABLED]"


async def test_query_callbacks_recheck_admission_immediately_before_each_effect() -> None:
    decisions = {"allow": True}

    def admitted() -> bool:
        return decisions["allow"]

    tools, memory, calls, knowledge_calls = _tools(admitted=admitted)

    for callback, data in (
        ("chat.recall_memory", {"query": "memory"}),
        ("chat.search_knowledge", {"query": "knowledge"}),
        ("chat.search_web", {"query": "web"}),
        ("chat.fetch_page", {"urls": ["https://example.com"]}),
    ):
        decisions["allow"] = False
        with pytest.raises(ChatQueryAdmissionError):
            await _callbacks(tools)[callback](_request(callback, data))
        decisions["allow"] = True

    assert memory.recall_calls == []
    assert knowledge_calls == []
    assert calls == []


async def test_frozen_false_capability_blocks_a_forged_callback_without_backend() -> None:
    tools, memory, calls, knowledge_calls = _tools(
        capabilities=ChatQueryCapabilities(
            knowledge=False,
            search=False,
            fetch=False,
        )
    )

    for callback, data in (
        ("chat.search_knowledge", {"query": "knowledge"}),
        ("chat.search_web", {"query": "web"}),
        ("chat.fetch_page", {"urls": ["https://example.com"]}),
    ):
        payload = await _callbacks(tools)[callback](_request(callback, data))
        assert payload == {"ok": False, "code": "capability_disabled"}

    assert memory.recall_calls == []
    assert knowledge_calls == []
    assert calls == []


async def test_cancelled_error_is_propagated_unchanged() -> None:
    async def knowledge(**_kwargs: object) -> list[object]:
        raise asyncio.CancelledError

    tools, _memory, _calls, _knowledge_calls = _tools(knowledge=knowledge)

    with pytest.raises(asyncio.CancelledError):
        await _callbacks(tools)["chat.search_knowledge"](
            _request("chat.search_knowledge", {"query": "cancel"})
        )


async def test_callback_result_is_bounded_before_protocol_serialization() -> None:
    async def knowledge(**_kwargs: object) -> list[object]:
        return [
            SimpleNamespace(id=index, content="知" * 8_000, source="vector")
            for index in range(32)
        ]

    tools, _memory, _calls, _knowledge_calls = _tools(knowledge=knowledge)

    payload = await _callbacks(tools)["chat.search_knowledge"](
        _request("chat.search_knowledge", {"query": "bounded"})
    )

    assert payload["ok"] is True
    result = payload["result"]
    assert len(result["items"]) < 32
    assert result["truncated"] is True
    assert result["omitted_items"] > 0
    assert len(str(payload).encode("utf-8")) < 128 * 1024


async def test_knowledge_embedding_guard_observes_revocation_after_lookup_started() -> None:
    decisions = {"allow": True}
    guard_values: list[bool] = []

    async def knowledge(**kwargs: object) -> list[object]:
        decisions["allow"] = False
        guard = kwargs["embedding_guard"]
        assert callable(guard)
        guard_values.append(bool(guard()))
        return []

    tools, _memory, _calls, _knowledge_calls = _tools(
        knowledge=knowledge,
        admitted=lambda: decisions["allow"],
    )

    payload = await _callbacks(tools)["chat.search_knowledge"](
        _request("chat.search_knowledge", {"query": "revoked"})
    )

    assert payload["ok"] is True
    assert guard_values == [False]
