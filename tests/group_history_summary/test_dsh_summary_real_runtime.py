"""TSK-206 Python 总结服务 over the real installed DSH summary preset."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema,
    LayoutParamsSchema,
)
from komari_bot.plugins.group_history_summary.dsh_summary_service import (
    DshSummaryService,
)
from komari_bot.plugins.group_history_summary.execution_service import (
    EMPTY_HISTORY_TEXT,
    execute_group_summary,
)
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
        DshSummaryService(host_getter=lambda: host),
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


async def _execute(bot: _HistoryBot) -> Any:
    return await execute_group_summary(
        bot=cast("Any", bot),
        group_id="123",
        bot_self_id=BOT_ID,
        user_request="总结最近消息",
        config=_config(),
        request_trace_id="summary-real-1",
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
    ) as (host, _server):
        _install_summary_runtime(monkeypatch, host)
        task = asyncio.create_task(_execute(bot))
        await asyncio.wait_for(bot.started.wait(), timeout=15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(bot.cancelled.wait(), timeout=10)
        assert bot.cancelled.is_set()
        assert host.state.value == "ready"
