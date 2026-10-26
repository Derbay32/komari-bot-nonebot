"""TSK-206 Python 总结服务 over the real installed DSH summary preset."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.agent.types import MAX_RESULT_BYTES
from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema,
    LayoutParamsSchema,
)
from komari_bot.plugins.group_history_summary.dsh_summary_service import (
    DshSummaryService,
)
from komari_bot.plugins.group_history_summary.execution_service import (
    EMPTY_HISTORY_TEXT,
    SummaryServiceUnavailableError,
    execute_group_summary,
)
from komari_bot.plugins.group_history_summary.group_lock import GroupSummaryLease
from komari_bot.plugins.group_history_summary.history_service import (
    HistoryFetchMetadata,
    HistoryIncompleteError,
    HistoryMessage,
)
from komari_bot.plugins.group_history_summary.history_tools import SummaryToolResult
from tests.komari_agent.dsh_test_support import (
    MAIN_HOST_SCRIPT,
    HangResponse,
    ToolCallResponse,
    running_dsh,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

BOT_ID = "999"


class _FakeLease:
    def __init__(self, manager: _FakeLockManager, group_id: str) -> None:
        self._manager = manager
        self._group_id = group_id

    async def run(self, operation: Any) -> Any:
        return await operation

    async def close(self) -> None:
        self._manager.running.discard(self._group_id)


class _FakeLockManager:
    def __init__(self) -> None:
        self.running: set[str] = set()

    async def try_acquire(
        self,
        *,
        group_id: str,
        redis_db: int,
        ttl_seconds: int,
    ) -> _FakeLease | None:
        del redis_db, ttl_seconds
        if group_id in self.running:
            return None
        self.running.add(group_id)
        return _FakeLease(self, group_id)


class _HistoryBot:
    self_id = BOT_ID

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        block: bool = False,
    ) -> None:
        self.items = items
        self.block = block
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls: list[dict[str, Any]] = []

    async def call_api(self, api: str, **kwargs: Any) -> dict[str, Any]:
        assert api == "get_group_msg_history"
        self.calls.append(dict(kwargs))
        if self.block:
            self.started.set()
            try:
                await self.released.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return {"messages": self.items}


def _history_items(count: int) -> list[dict[str, Any]]:
    return [
        {
            "user_id": f"1000{index}",
            "time": 1_700_000_000 + index,
            "message_seq": index,
            "message_id": f"m{index}",
            "sender": {"nickname": f"用户{index}"},
            "message": [{"type": "text", "data": {"text": f"第{index}条消息"}}],
        }
        for index in range(1, count + 1)
    ]


def _config() -> DynamicConfigSchema:
    return DynamicConfigSchema(
        version="1.0",
        plugin_enable=True,
        min_summary_count=10,
        max_summary_count=200,
        fetch_batch_size=50,
        summary_default_count=50,
        summary_planning_round_limit=3,
        summary_tool_scan_limit=300,
        history_min_coverage_ratio=0.8,
        layout_params=LayoutParamsSchema(),
    )


def _install_summary_runtime(
    monkeypatch: pytest.MonkeyPatch,
    host: Any,
    *,
    render_calls: list[dict[str, Any]] | None = None,
    service: DshSummaryService | None = None,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module
    from komari_bot.plugins.group_history_summary import history_tools

    monkeypatch.setattr(
        exec_module,
        "_group_lock_manager",
        _FakeLockManager(),
    )
    monkeypatch.setattr(
        exec_module,
        "_dsh_summary_service",
        service or DshSummaryService(host_getter=lambda: host),
    )
    def _resolve_name(
        *,
        group_id: str,
        user_id: str,
        fallback_nickname: str,
    ) -> str:
        del group_id, user_id
        return f"角色-{fallback_nickname}"

    monkeypatch.setattr(
        history_tools,
        "character_binding",
        SimpleNamespace(get_character_name=_resolve_name),
    )

    def _render(**kwargs: Any) -> SimpleNamespace:
        if render_calls is not None:
            render_calls.append(dict(kwargs))
        return SimpleNamespace(images_base64=("image-data",), truncated=False)

    monkeypatch.setattr(exec_module, "render_summary_image_pages_base64", _render)


async def _execute(
    bot: _HistoryBot,
    *,
    request_trace_id: str = "summary-real-1",
) -> Any:
    return await execute_group_summary(
        bot=cast("Any", bot),
        group_id="123",
        bot_self_id=BOT_ID,
        user_request="总结最近消息",
        config=_config(),
        request_trace_id=request_trace_id,
        history_capability_confirmed=True,
    )


async def test_real_dsh_summary_preset_returns_validated_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = (
        ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
        ToolCallResponse("final_summary", {"content": "真实 DSH 总结"}),
    )
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        prompt="冻结的聊天角色提示词。",
        summary_prompt="冻结的总结角色提示词。",
        context_window=32768,
    ) as (host, server):
        _install_summary_runtime(monkeypatch, host, render_calls=render_calls)
        result = await _execute(_HistoryBot(_history_items(20)))

    assert result.summary_text == "真实 DSH 总结"
    assert result.filtered_message_count == 20
    assert result.filter_label == "recent_group_messages"
    assert result.history_fetch is not None
    assert result.image_base64 == "image-data"
    assert len(render_calls) == 1

    assert len(server.requests) == 2
    first = server.requests[0]
    assert set(first.tool_names) == {
        "fetch_recent_group_messages",
        "fetch_messages_by_user",
        "fetch_messages_by_topic",
        "final_summary",
    }
    assert "冻结的总结角色提示词。" in first.system_text
    assert "komari_summary_security_boundary" in first.system_text
    assert "冻结的聊天角色提示词。" not in first.system_text
    assert "总结最近消息" not in first.system_text
    assert len(first.user_texts) == 1
    task_input = first.user_texts[0]
    assert task_input.startswith('<komari_summary_task schema="1">')
    assert '"content":"总结最近消息"' in task_input
    assert not any(message.get("role") == "assistant" for message in first.messages)

    second = server.requests[1]
    tool_text = "\n".join(
        str(message.get("content"))
        for message in second.messages
        if message.get("role") == "tool"
    )
    assert '<untrusted_context source_type="group_history"' in tool_text
    for index in range(1, 21):
        assert f"第{index}条消息" in tool_text
    assert '"rendered_count":20' in tool_text


class _OneRowProvider:
    async def fetch_recent(self, arguments: Any) -> SummaryToolResult:
        del arguments
        return SummaryToolResult(
            source="recent_group_messages",
            matched_count=1,
            messages=[
                HistoryMessage(
                    user_id="1001",
                    nickname="阿明",
                    content="真实历史",
                    timestamp=1,
                    message_seq=1,
                    message_id="m1",
                    reply_to_message_id=None,
                )
            ],
            filters={"count": 10},
            history_fetch=None,
        )

    async def fetch_user(self, arguments: Any) -> SummaryToolResult:
        del arguments
        raise AssertionError

    async def fetch_topic(self, arguments: Any) -> SummaryToolResult:
        del arguments
        raise AssertionError


class _IncompleteProvider:
    def __init__(self, metadata: HistoryFetchMetadata) -> None:
        self.metadata = metadata

    async def fetch_recent(self, arguments: Any) -> SummaryToolResult:
        del arguments
        raise HistoryIncompleteError(self.metadata)

    async def fetch_user(self, arguments: Any) -> SummaryToolResult:
        del arguments
        raise AssertionError

    async def fetch_topic(self, arguments: Any) -> SummaryToolResult:
        del arguments
        raise AssertionError


async def test_real_dsh_incomplete_history_restores_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = HistoryFetchMetadata(
        status="partial",
        requested_count=50,
        retrieved_item_count=10,
        missing_count=40,
        completed_batches=1,
        failed_batch=2,
        failure_code="history_api_error",
    )
    provider = _IncompleteProvider(metadata)
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=(
            ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
        ),
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=16384,
    ) as (host, server):
        service = DshSummaryService(
            host_getter=lambda: host,
            history_provider_factory=lambda *_args: provider,
        )
        _install_summary_runtime(
            monkeypatch,
            host,
            render_calls=render_calls,
            service=service,
        )
        with pytest.raises(HistoryIncompleteError) as exc_info:
            await _execute(_HistoryBot(_history_items(3)))

    assert exc_info.value.metadata == metadata
    assert len(server.requests) == 1
    assert render_calls == []


async def test_real_dsh_final_admission_revocation_prevents_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary.dsh_summary_service as summary_service

    decisions = {"count": 0}

    def _admit(*, group_id: str) -> bool:
        del group_id
        decisions["count"] += 1
        return decisions["count"] <= 3

    monkeypatch.setattr(summary_service, "effect_business_admitted", _admit)
    provider = _OneRowProvider()
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=(
            ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
            ToolCallResponse("final_summary", {"content": "不得完成"}),
        ),
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=16384,
    ) as (host, server):
        service = DshSummaryService(
            host_getter=lambda: host,
            history_provider_factory=lambda *_args: provider,
        )
        _install_summary_runtime(
            monkeypatch,
            host,
            render_calls=render_calls,
            service=service,
        )
        result = await _execute(_HistoryBot(_history_items(3)))

    assert result.summary_text == ""
    assert render_calls == []
    assert len(server.requests) == 2
    assert decisions["count"] == 4


async def _run_large_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> tuple[Any, Any]:
    responses = (
        ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
        ToolCallResponse("final_summary", {"content": content}),
    )
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=2_000_000,
    ) as (host, server):
        _install_summary_runtime(monkeypatch, host, render_calls=render_calls)
        result = await _execute(_HistoryBot(_history_items(3)))

    assert len(server.requests) == 2
    assert result.summary_text == content
    return result, render_calls


async def test_real_dsh_300k_ascii_summary_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "a" * (300 * 1024)
    result, render_calls = await _run_large_final(tmp_path, monkeypatch, content)

    assert len(result.summary_text) == len(content)
    assert result.filtered_message_count == 3
    assert len(render_calls) == 1


async def test_real_dsh_exact_serialized_business_limit_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overhead = len(
        json.dumps(
            {"summary_text": ""},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    content = "a" * (MAX_RESULT_BYTES - overhead)
    result, _render_calls = await _run_large_final(tmp_path, monkeypatch, content)

    assert len(result.summary_text.encode("utf-8")) == MAX_RESULT_BYTES - overhead


async def test_real_dsh_multibyte_summary_near_the_bound_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "汉" * 170_000
    encoded = json.dumps(
        {"summary_text": content},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= MAX_RESULT_BYTES

    result, _render_calls = await _run_large_final(tmp_path, monkeypatch, content)

    assert result.summary_text == content


async def test_real_dsh_above_limit_final_is_correctable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    too_large = "a" * MAX_RESULT_BYTES
    corrected = "修正后的总结"
    responses = (
        ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
        ToolCallResponse("final_summary", {"content": too_large}),
        ToolCallResponse("final_summary", {"content": corrected}),
    )
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=2_000_000,
    ) as (host, server):
        _install_summary_runtime(monkeypatch, host, render_calls=render_calls)
        result = await _execute(_HistoryBot(_history_items(3)))

    assert result.summary_text == corrected
    assert len(server.requests) == 3
    assert len(render_calls) == 1


async def test_real_dsh_empty_read_requires_final_and_returns_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = (
        ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
        ToolCallResponse("final_summary", {"content": "空历史总结"}),
    )
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=8192,
    ) as (host, _server):
        _install_summary_runtime(monkeypatch, host, render_calls=render_calls)
        result = await _execute(_HistoryBot([]))

    assert result.summary_text == EMPTY_HISTORY_TEXT
    assert result.filtered_message_count == 0
    assert result.image_base64 == ""
    assert render_calls == []


async def test_real_dsh_final_before_read_is_corrected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = (
        ToolCallResponse("final_summary", {"content": "太早的总结"}),
        ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
        ToolCallResponse("final_summary", {"content": "补齐后的总结"}),
    )

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=16384,
    ) as (host, server):
        _install_summary_runtime(monkeypatch, host)
        result = await _execute(_HistoryBot(_history_items(3)))

    assert result.summary_text == "补齐后的总结"
    assert len(server.requests) == 3


async def test_real_dsh_admission_denial_is_silent_normal_control_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary.dsh_summary_service as summary_service

    monkeypatch.setattr(
        summary_service,
        "effect_business_admitted",
        lambda **_kwargs: False,
    )
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=(ToolCallResponse("final_summary", {"content": "never"}),),
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=8192,
    ) as (host, server):
        _install_summary_runtime(monkeypatch, host, render_calls=render_calls)
        result = await _execute(_HistoryBot(_history_items(3)))

    assert result.summary_text == ""
    assert result.image_base64 == ""
    assert render_calls == []
    assert server.requests == []


async def test_real_dsh_cancellation_during_model_request_releases_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hang = HangResponse()

    async with running_dsh(
        tmp_path,
        responses=(hang,),
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=8192,
    ) as (host, server):
        _install_summary_runtime(monkeypatch, host)
        task = asyncio.create_task(_execute(_HistoryBot(_history_items(1))))
        try:
            await asyncio.to_thread(server.wait_for_request_count, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            hang.release.set()
        assert host.state.value == "ready"


async def test_real_dsh_cancellation_during_internal_history_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate_started = asyncio.Event()
    gate_cancelled = asyncio.Event()

    async def _gate() -> None:
        gate_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            gate_cancelled.set()
            raise

    provider = _OneRowProvider()
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=(
            ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
        ),
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=16384,
    ) as (host, server):
        service = DshSummaryService(
            host_getter=lambda: host,
            history_provider_factory=lambda *_args: provider,
            history_page_gate=_gate,
        )
        _install_summary_runtime(
            monkeypatch,
            host,
            render_calls=render_calls,
            service=service,
        )
        task = asyncio.create_task(_execute(_HistoryBot(_history_items(3))))
        await asyncio.wait_for(gate_started.wait(), timeout=15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(gate_cancelled.wait(), timeout=10)
        assert host.state.value == "ready"

    assert len(server.requests) == 1
    assert render_calls == []


class _LeaseManager:
    def __init__(self, host: Any, cancel_completed: asyncio.Event) -> None:
        self.host = host
        self.cancel_completed = cancel_completed
        self.release_calls = 0
        self.release_checks: list[dict[str, bool]] = []
        self.renew_calls = 0

    async def release(self, *, key: str, owner_token: str) -> bool:
        del key, owner_token
        self.release_calls += 1
        observed_host = cast("Any", self.host)
        checks = {
            "cancellation_completed": self.cancel_completed.is_set(),
            "no_live_requests": not observed_host._requests,
            "no_live_callbacks": not observed_host._all_callback_tasks,
            "host_ready": self.host.state.value == "ready",
        }
        self.release_checks.append(checks)
        assert checks["cancellation_completed"], "lease released before DSH cancellation drained"
        assert checks["no_live_requests"], "live request remained at lease release"
        assert checks["no_live_callbacks"], "live callback remained at lease release"
        assert checks["host_ready"], "host was not ready at lease release"
        return True

    async def renew(self, *, key: str, owner_token: str, ttl_seconds: int) -> bool:
        del key, owner_token, ttl_seconds
        self.renew_calls += 1
        return True


class _LeaseLockManager:
    def __init__(self, lease: GroupSummaryLease) -> None:
        self._lease = lease

    async def try_acquire(self, **_kwargs: object) -> GroupSummaryLease:
        return self._lease


async def test_real_dsh_lease_loss_cancels_running_task_and_releases_after_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    gate_started = asyncio.Event()

    async def _gate() -> None:
        gate_started.set()
        await asyncio.Future()

    cancel_completed = asyncio.Event()
    manager: _LeaseManager
    lease: GroupSummaryLease
    provider = _OneRowProvider()
    render_calls: list[dict[str, Any]] = []

    async with running_dsh(
        tmp_path,
        responses=(
            ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
        ),
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=16384,
    ) as (host, server):
        real_execute = host.execute

        async def observed_execute(*args: Any, **kwargs: Any) -> Any:
            try:
                return await real_execute(*args, **kwargs)
            except asyncio.CancelledError:
                cancel_completed.set()
                raise

        host.execute = observed_execute  # type: ignore[method-assign]
        manager = _LeaseManager(host, cancel_completed)
        lease = GroupSummaryLease(
            manager=cast("Any", manager),
            key="real-lease-loss",
            owner_token="owner",
            ttl_seconds=60,
        )
        first_service = DshSummaryService(
            host_getter=lambda: host,
            history_provider_factory=lambda *_args: provider,
            history_page_gate=_gate,
        )
        _install_summary_runtime(
            monkeypatch,
            host,
            render_calls=render_calls,
            service=first_service,
        )
        monkeypatch.setattr(
            exec_module,
            "_group_lock_manager",
            _LeaseLockManager(lease),
        )
        task = asyncio.create_task(_execute(_HistoryBot(_history_items(3))))
        await asyncio.wait_for(gate_started.wait(), timeout=15)
        lease._lost_event.set()
        with pytest.raises(SummaryServiceUnavailableError):
            await task

        assert manager.release_calls == 1
        assert manager.release_checks == [
            {
                "cancellation_completed": True,
                "no_live_requests": True,
                "no_live_callbacks": True,
                "host_ready": True,
            }
        ]
        assert host.state.value == "ready"

        server.enqueue(
            ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
            ToolCallResponse("final_summary", {"content": "恢复后的总结"}),
        )
        monkeypatch.setattr(
            exec_module,
            "_dsh_summary_service",
            DshSummaryService(
                host_getter=lambda: host,
                history_provider_factory=lambda *_args: provider,
            ),
        )
        monkeypatch.setattr(exec_module, "_group_lock_manager", _FakeLockManager())
        result = await _execute(
            _HistoryBot(_history_items(3)),
            request_trace_id="summary-recovered-1",
        )

    assert result.summary_text == "恢复后的总结"
    assert result.filtered_message_count == 1
    assert len(server.requests) == 3
    assert render_calls != []


async def test_real_dsh_cancellation_during_history_read_releases_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = (
        ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
    )
    bot = _HistoryBot(_history_items(20), block=True)

    async with running_dsh(
        tmp_path,
        responses=responses,
        entry_script=MAIN_HOST_SCRIPT,
        summary_prompt="总结角色。",
        context_window=8192,
    ) as (host, server):
        _install_summary_runtime(monkeypatch, host)
        task = asyncio.create_task(_execute(bot))
        await asyncio.wait_for(bot.started.wait(), timeout=15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(bot.cancelled.wait(), timeout=10)
        assert bot.cancelled.is_set()
        assert host.state.value == "ready"
        server.enqueue(
            ToolCallResponse("fetch_recent_group_messages", {"count": 20}),
            ToolCallResponse("final_summary", {"content": "取消后恢复"}),
        )
        recovered = await _execute(
            _HistoryBot(_history_items(3)),
            request_trace_id="summary-recovered-2",
        )
        assert recovered.summary_text == "取消后恢复"
        assert host.state.value == "ready"
