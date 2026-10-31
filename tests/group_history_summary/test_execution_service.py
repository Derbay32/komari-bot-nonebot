"""TSK-206 总结执行入口：能力、租约、DSH 结果与渲染投影。"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from komari_bot.plugins.group_history_summary.admission_gate import (
    SummaryAdmissionDeniedError,
)
from komari_bot.plugins.group_history_summary.config_schema import (
    DynamicConfigSchema,
    LayoutParamsSchema,
)
from komari_bot.plugins.group_history_summary.dsh_summary_service import (
    SummaryGeneration,
    SummaryGenerationError,
)
from komari_bot.plugins.group_history_summary.execution_service import (
    CapabilityNotSupportedError,
    SummaryBusyError,
    SummaryServiceUnavailableError,
    execute_group_summary,
)
from komari_bot.plugins.group_history_summary.group_lock import (
    GroupSummaryLockLostError,
)
from komari_bot.plugins.group_history_summary.history_service import (
    HistoryFetchMetadata,
    HistoryMessage,
)
from komari_bot.plugins.group_history_summary.summary_candidate import SummarySelection


class _FakeSummaryLease:
    def __init__(self, manager: _FakeSummaryLockManager, group_id: str) -> None:
        self._manager = manager
        self._group_id = group_id

    async def run(self, operation: Any) -> Any:
        return await operation

    async def close(self) -> None:
        self._manager.running_groups.discard(self._group_id)


class _FakeSummaryLockManager:
    def __init__(self) -> None:
        self.running_groups: set[str] = set()

    async def try_acquire(
        self,
        *,
        group_id: str,
        redis_db: int,
        ttl_seconds: int,
    ) -> _FakeSummaryLease | None:
        assert redis_db >= 0
        assert ttl_seconds > 0
        if group_id in self.running_groups:
            return None
        self.running_groups.add(group_id)
        return _FakeSummaryLease(self, group_id)


class _FakeDshSummaryService:
    def __init__(
        self,
        *,
        selection: SummarySelection | None = None,
        summary_text: str = "今天讨论了测试。",
        error: Exception | None = None,
        wait: asyncio.Event | None = None,
        started: asyncio.Event | None = None,
    ) -> None:
        self.selection = selection
        self.summary_text = summary_text
        self.error = error
        self.wait = wait
        self.started = started
        self.calls: list[dict[str, Any]] = []
        self.cancelled = False

    async def generate(self, **kwargs: Any) -> SummaryGeneration:
        self.calls.append(dict(kwargs))
        try:
            if self.started is not None:
                self.started.set()
            if self.wait is not None:
                await self.wait.wait()
            if self.error is not None:
                raise self.error
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return SummaryGeneration(
            summary_text=self.summary_text,
            selection=self.selection,
        )


@pytest.fixture(autouse=True)
def _use_in_memory_group_lock(monkeypatch: pytest.MonkeyPatch) -> _FakeSummaryLockManager:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    lock = _FakeSummaryLockManager()
    monkeypatch.setattr(exec_module, "_group_lock_manager", lock)
    return lock


@pytest.fixture(autouse=True)
def _install_fake_dsh_service(monkeypatch: pytest.MonkeyPatch) -> _FakeDshSummaryService:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    service = _FakeDshSummaryService()
    monkeypatch.setattr(exec_module, "_dsh_summary_service", service)
    return service


def _build_config(**overrides: Any) -> DynamicConfigSchema:
    defaults: dict[str, Any] = {
        "version": "1.0",
        "summary_enabled": True,
        "min_summary_count": 10,
        "max_summary_count": 200,
        "fetch_batch_size": 50,
        "summary_default_count": 50,
        "summary_planning_round_limit": 3,
        "summary_tool_scan_limit": 300,
        "history_min_coverage_ratio": 0.8,
        "layout_params": LayoutParamsSchema(),
    }
    defaults.update(overrides)
    return DynamicConfigSchema(**defaults)


def _message(
    *,
    user_id: str = "1001",
    nickname: str = "阿明",
    content: str = "hello",
    timestamp: int = 1,
    message_seq: int = 1,
) -> HistoryMessage:
    return HistoryMessage(
        user_id=user_id,
        nickname=nickname,
        content=content,
        timestamp=timestamp,
        message_seq=message_seq,
        message_id=str(message_seq),
        reply_to_message_id=None,
    )


def _selection(
    messages: list[HistoryMessage],
    *,
    source: str = "recent_group_messages",
    metadata: HistoryFetchMetadata | None = None,
) -> SummarySelection:
    return SummarySelection(
        generation=1,
        source=source,
        matched_count=len(messages),
        messages=tuple(messages),
        filters={"count": 10},
        history_fetch=metadata,
    )


async def _async_return_true(*_args: Any, **_kwargs: Any) -> bool:
    return True


async def _async_return_false(*_args: Any, **_kwargs: Any) -> bool:
    return False


@pytest.mark.asyncio
async def test_capability_is_checked_before_task_creation(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _async_return_false
    )

    with pytest.raises(CapabilityNotSupportedError):
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="123",
            bot_self_id="999",
            user_request="总结",
            config=_build_config(),
        )
    assert _install_fake_dsh_service.calls == []


@pytest.mark.asyncio
async def test_confirmed_capability_is_not_checked_twice(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    async def _fail_capability_check(_bot: object) -> bool:
        raise AssertionError

    monkeypatch.setattr(
        exec_module, "check_group_history_supported", _fail_capability_check
    )
    _install_fake_dsh_service.selection = _selection([])

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="123",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
        history_capability_confirmed=True,
    )

    assert result.filtered_message_count == 0
    assert len(_install_fake_dsh_service.calls) == 1


@pytest.mark.asyncio
async def test_busy_group_does_not_start_a_dsh_task(
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    lock = cast("_FakeSummaryLockManager", exec_module._group_lock_manager)
    lock.running_groups.add("123")

    with pytest.raises(SummaryBusyError):
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="123",
            bot_self_id="999",
            user_request="总结",
            config=_build_config(),
        )
    assert _install_fake_dsh_service.calls == []


@pytest.mark.asyncio
async def test_lock_backend_failure_is_fail_closed_before_capability(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    class _FailingLockManager:
        async def try_acquire(self, **_kwargs: object) -> None:
            raise ConnectionError

    capability_checked = False

    async def _track_capability(_bot: object) -> bool:
        nonlocal capability_checked
        capability_checked = True
        return True

    monkeypatch.setattr(exec_module, "_group_lock_manager", _FailingLockManager())
    monkeypatch.setattr(exec_module, "check_group_history_supported", _track_capability)

    with pytest.raises(SummaryServiceUnavailableError):
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="123",
            bot_self_id="999",
            user_request="总结",
            config=_build_config(),
        )

    assert capability_checked is False
    assert _install_fake_dsh_service.calls == []


@pytest.mark.asyncio
async def test_non_empty_selection_renders_existing_output_fields(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    messages = [
        _message(user_id="1001", nickname="阿明", timestamp=100, message_seq=1),
        _message(
            user_id="1002",
            nickname="小红",
            content="world",
            timestamp=200,
            message_seq=2,
        ),
    ]
    _install_fake_dsh_service.selection = _selection(messages)
    _install_fake_dsh_service.summary_text = "今天讨论了 hello 和 world。"
    rendered: list[dict[str, Any]] = []

    def _fake_render(**kwargs: Any) -> SimpleNamespace:
        rendered.append(dict(kwargs))
        return SimpleNamespace(
            images_base64=("page-a", "page-b"),
            truncated=True,
        )

    monkeypatch.setattr(
        exec_module, "render_summary_image_pages_base64", _fake_render
    )

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="456",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
        requested_count=30,
        request_trace_id="e2e-trace",
    )

    assert result.summary_text == "今天讨论了 hello 和 world。"
    assert result.filtered_message_count == 2
    assert result.filter_label == "recent_group_messages"
    assert result.image_base64 == "page-a"
    assert result.image_pages_base64 == ("page-a", "page-b")
    assert result.image_truncated is True
    assert result.selection is not None
    assert any("今天讨论了" in line for line in rendered[0]["body_lines"])
    assert rendered[0]["title"] == "小鞠的总结时间到！"
    assert rendered[0]["subtitle"].startswith("recent_group_messages 2 条 | ")
    call = _install_fake_dsh_service.calls[0]
    assert call["request_trace_id"] == "e2e-trace"
    assert call["config"].limits.default_count == 30


@pytest.mark.asyncio
async def test_empty_selection_returns_deterministic_notice_without_image(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    _install_fake_dsh_service.selection = _selection([])
    _install_fake_dsh_service.summary_text = "空历史的模型文本"
    render_calls: list[object] = []

    def _fake_render(**kwargs: Any) -> object:
        render_calls.append(kwargs)
        raise AssertionError("空历史不得渲染图片")

    monkeypatch.setattr(
        exec_module, "render_summary_image_pages_base64", _fake_render
    )

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="789",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
    )

    assert "文本记录太少" in result.summary_text
    assert result.summary_text != "空历史的模型文本"
    assert result.filtered_message_count == 0
    assert result.filter_label == "无"
    assert result.time_range == "无"
    assert result.image_base64 == ""
    assert render_calls == []


@pytest.mark.asyncio
async def test_allowed_partial_history_keeps_notice_metadata_and_image(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    metadata = HistoryFetchMetadata(
        status="partial",
        requested_count=50,
        retrieved_item_count=40,
        missing_count=10,
        completed_batches=2,
        failed_batch=3,
        failure_code="history_api_error",
    )
    _install_fake_dsh_service.selection = _selection(
        [_message(timestamp=1, message_seq=1)],
        metadata=metadata,
    )
    _install_fake_dsh_service.summary_text = "总结内容"
    rendered_lines: list[str] = []

    def _fake_render(**kwargs: Any) -> SimpleNamespace:
        rendered_lines.extend(kwargs["body_lines"])
        return SimpleNamespace(images_base64=("image-data",), truncated=False)

    monkeypatch.setattr(
        exec_module, "render_summary_image_pages_base64", _fake_render
    )

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="partial-history-group",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
    )

    assert result.history_fetch is metadata
    assert result.summary_text.startswith("⚠ 历史第 3 批读取失败")
    assert any("约缺 10 条记录" in line for line in rendered_lines)


@pytest.mark.asyncio
async def test_image_rendering_runs_outside_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    _install_fake_dsh_service.selection = _selection([_message()])
    render_thread_id = 0

    def _fake_render(**_kwargs: Any) -> SimpleNamespace:
        nonlocal render_thread_id
        render_thread_id = threading.get_ident()
        return SimpleNamespace(images_base64=("image-data",), truncated=False)

    monkeypatch.setattr(
        exec_module, "render_summary_image_pages_base64", _fake_render
    )
    event_loop_thread_id = threading.get_ident()

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="render-thread-group",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
    )

    assert result.image_base64 == "image-data"
    assert render_thread_id != event_loop_thread_id


@pytest.mark.asyncio
async def test_admission_denial_before_render_is_silent_normal_control_flow(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    _install_fake_dsh_service.selection = _selection([_message()])
    render_calls: list[object] = []

    def _deny(**_kwargs: object) -> None:
        raise SummaryAdmissionDeniedError

    def _fake_render(**kwargs: Any) -> object:
        render_calls.append(kwargs)
        raise AssertionError("准入拒绝不得渲染")

    monkeypatch.setattr(exec_module, "ensure_effect_admitted", _deny)
    monkeypatch.setattr(
        exec_module, "render_summary_image_pages_base64", _fake_render
    )

    result = await execute_group_summary(
        bot=cast("Any", SimpleNamespace()),
        group_id="denied-group",
        bot_self_id="999",
        user_request="总结",
        config=_build_config(),
    )

    assert result.summary_text == ""
    assert result.filtered_message_count == 0
    assert result.selection is None
    assert result.image_base64 == ""
    assert render_calls == []


@pytest.mark.asyncio
async def test_generation_failure_propagates_without_render(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    _install_fake_dsh_service.error = SummaryGenerationError("agent_error")
    render_calls: list[object] = []

    def _fake_render(**kwargs: Any) -> object:
        render_calls.append(kwargs)
        raise AssertionError("生成失败不得渲染")

    monkeypatch.setattr(
        exec_module, "render_summary_image_pages_base64", _fake_render
    )

    with pytest.raises(SummaryGenerationError) as exc_info:
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="failed-group",
            bot_self_id="999",
            user_request="总结",
            config=_build_config(),
        )

    assert exc_info.value.code == "agent_error"
    assert render_calls == []


@pytest.mark.asyncio
async def test_lease_loss_cancels_dsh_task_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    started = asyncio.Event()
    never = asyncio.Event()
    _install_fake_dsh_service.wait = never
    _install_fake_dsh_service.started = started
    release_check = asyncio.Event()

    class _LostLease(_FakeSummaryLease):
        async def run(self, operation: Any) -> Any:
            task = asyncio.create_task(operation)
            await started.wait()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise GroupSummaryLockLostError

        async def close(self) -> None:
            release_check.set()
            await super().close()

    class _LostLockManager(_FakeSummaryLockManager):
        async def try_acquire(self, **_kwargs: object) -> _LostLease:
            return _LostLease(self, "lost-group")

    monkeypatch.setattr(exec_module, "_group_lock_manager", _LostLockManager())

    with pytest.raises(SummaryServiceUnavailableError):
        await execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="lost-group",
            bot_self_id="999",
            user_request="总结",
            config=_build_config(),
        )

    await release_check.wait()
    assert _install_fake_dsh_service.cancelled is True


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_releases_lease(
    _install_fake_dsh_service: _FakeDshSummaryService,
) -> None:
    import komari_bot.plugins.group_history_summary.execution_service as exec_module

    never = asyncio.Event()
    started = asyncio.Event()
    _install_fake_dsh_service.wait = never
    _install_fake_dsh_service.started = started
    lock = cast("_FakeSummaryLockManager", exec_module._group_lock_manager)

    task = asyncio.create_task(
        execute_group_summary(
            bot=cast("Any", SimpleNamespace()),
            group_id="cancel-group",
            bot_self_id="999",
            user_request="总结",
            config=_build_config(),
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert _install_fake_dsh_service.cancelled is True
    assert lock.running_groups == set()
