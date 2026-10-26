"""End-to-end task-adapter tests over the real installed DSH runtime."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import TYPE_CHECKING

import pytest

from komari_bot.agent import (
    AgentCallbackHandler,
    AgentCallbackRequest,
    AgentHostUnavailableError,
    AgentTaskBudget,
    AgentTaskError,
    AgentTaskRequest,
    JsonValue,
    ReadinessState,
    TaskKind,
)
from tests.komari_agent.dsh_test_support import (
    MAIN_HOST_SCRIPT,
    EmptyResponse,
    HangResponse,
    HttpErrorResponse,
    TextResponse,
    ToolCallResponse,
    Usage,
    allowed_profile_files,
    chat_task_data,
    default_budget,
    host_for,
    running_dsh,
)

if TYPE_CHECKING:
    from pathlib import Path


def _task(
    task_id: str = "task-1",
    message: str = "hello",
    *,
    budget: AgentTaskBudget | None = None,
) -> AgentTaskRequest:
    return AgentTaskRequest(
        task_id=task_id,
        kind=TaskKind.CHAT,
        task_data=chat_task_data(message),
        budget=budget or default_budget(),
    )


def _callbacks(
    admissions: list[AgentCallbackRequest],
    *,
    admission: AgentCallbackHandler | None = None,
    fixture: AgentCallbackHandler | None = None,
) -> dict[str, AgentCallbackHandler]:
    async def default_admission(request: AgentCallbackRequest) -> JsonValue:
        admissions.append(request)
        return {"allow": True}

    async def default_fixture(request: AgentCallbackRequest) -> JsonValue:
        return {"ok": True, "value": request.data.get("value")}

    return {
        "admission": admission or default_admission,
        "fixture_result": fixture or default_fixture,
    }


async def test_task_completes_through_business_callback(tmp_path: Path) -> None:
    responses = (ToolCallResponse("fixture_complete", {"value": "hello"}),)
    async with running_dsh(tmp_path, responses=responses) as (host, server):
        admissions: list[AgentCallbackRequest] = []
        result = await host.execute(_task(), _callbacks(admissions))

        assert result.task_id == "task-1"
        assert result.kind is TaskKind.CHAT
        assert result.result == {"ok": True, "value": "hello"}
        assert host.readiness.state is ReadinessState.READY
        assert [request.name for request in admissions] == ["admission"]
        assert admissions[0].data["purpose"] == "conversation"
        assert admissions[0].data["attempt"] == 1

        assert len(server.requests) == 1
        request = server.requests[0]
        assert set(request.tool_names) == {"fixture_complete", "fixture_followup", "fixture_update_prompt"}
        assert request.system_text == "You are a fixture."
        assert len(request.user_texts) == 1
        assert request.user_texts[0].startswith('<komari_chat_task schema="1">')
        assert "<current_message>" in request.user_texts[0]
        assert '"content":"hello"' in request.user_texts[0]
        assert not any(name in {"bash", "read", "write", "edit", "run_code"} for name in request.tool_names)

    assert allowed_profile_files(tmp_path / "data") == {
        "dsh/package.json",
        "dsh/cordis.patch.yml",
    }


async def test_running_task_is_not_limited_by_shutdown_drain_timeout(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def held_callback(request: AgentCallbackRequest) -> JsonValue:
        started.set()
        await release.wait()
        return {"ok": True, "value": request.data["value"]}

    async with running_dsh(
        tmp_path,
        responses=(ToolCallResponse("fixture_complete", {"value": "slow-but-valid"}),),
    ) as (host, _server):
        task = asyncio.create_task(host.execute(_task(), _callbacks([], fixture=held_callback)))
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            # The shutdown drain deadline must not become a task execution deadline.
            # Keep the real callback behind a barrier beyond that ten-second window.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=11)
            release.set()
            result = await task
            assert result.result == {"ok": True, "value": "slow-but-valid"}
        finally:
            release.set()
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


async def test_second_task_does_not_inherit_first_task_history(tmp_path: Path) -> None:
    responses = (
        ToolCallResponse("fixture_complete", {"value": "one"}),
        ToolCallResponse("fixture_complete", {"value": "two"}),
    )
    async with running_dsh(tmp_path, responses=responses) as (host, server):
        first = await host.execute(_task("task-a", "first-message"), _callbacks([]))
        second = await host.execute(_task("task-b", "second-message"), _callbacks([]))

        assert first.result == {"ok": True, "value": "one"}
        assert second.result == {"ok": True, "value": "two"}
        assert len(server.requests) == 2
        second_request = server.requests[1]
        assert len(second_request.user_texts) == 1
        assert "second-message" in second_request.user_texts[0]
        assert "first-message" not in json.dumps(second_request.messages)
        assert not any(
            message.get("role") == "assistant"
            for message in second_request.messages
            if isinstance(message, dict)
        )


async def test_missing_result_is_an_explicit_incomplete_error(tmp_path: Path) -> None:
    async with running_dsh(
        tmp_path,
        responses=(TextResponse("ordinary assistant text must not become a business result"),),
    ) as (host, _server):
        with pytest.raises(AgentTaskError) as exc_info:
            await host.execute(_task(), _callbacks([]))
        assert exc_info.value.code == "incomplete_result"
        assert "ordinary assistant text" not in str(exc_info.value)
        assert host.readiness.state is ReadinessState.READY


async def test_transient_failure_uses_three_actual_attempts(tmp_path: Path) -> None:
    responses = (
        HttpErrorResponse(429, "rate limit exceeded", "rate_limit_error", "rate_limit_exceeded"),
        HttpErrorResponse(429, "rate limit exceeded", "rate_limit_error", "rate_limit_exceeded"),
        ToolCallResponse("fixture_complete", {"value": "after-retry"}),
    )
    async with running_dsh(tmp_path, responses=responses) as (host, server):
        admissions: list[AgentCallbackRequest] = []
        result = await host.execute(_task(), _callbacks(admissions))

        assert result.result == {"ok": True, "value": "after-retry"}
        assert len(server.requests) == 3
        assert [request.data["attempt"] for request in admissions] == [1, 2, 3]


async def test_permanent_failure_is_not_retried(tmp_path: Path) -> None:
    async with running_dsh(
        tmp_path,
        responses=(HttpErrorResponse(400, "invalid request", "invalid_request_error", "invalid_request"),),
    ) as (host, server):
        with pytest.raises(AgentTaskError) as exc_info:
            await host.execute(_task(), _callbacks([]))
        assert exc_info.value.code == "agent_error"
        assert len(server.requests) == 1


async def test_empty_response_is_not_retried(tmp_path: Path) -> None:
    async with running_dsh(tmp_path, responses=(EmptyResponse(),)) as (host, server):
        with pytest.raises(AgentTaskError) as exc_info:
            await host.execute(_task(), _callbacks([]))
        assert exc_info.value.code == "agent_error"
        assert len(server.requests) == 1


async def test_admission_revoked_before_second_attempt_prevents_provider_io(tmp_path: Path) -> None:
    responses = (
        HttpErrorResponse(429, "rate limit exceeded", "rate_limit_error", "rate_limit_exceeded"),
        ToolCallResponse("fixture_complete", {"value": "must-not-run"}),
    )
    seen: list[AgentCallbackRequest] = []

    async def admission(request: AgentCallbackRequest) -> JsonValue:
        seen.append(request)
        if request.data["attempt"] == 1:
            return {"allow": True}
        return {"allow": False}

    async with running_dsh(tmp_path, responses=responses) as (host, server):
        with pytest.raises(AgentTaskError):
            await host.execute(_task(), _callbacks([], admission=admission))
        assert len(server.requests) == 1
        assert [request.data["attempt"] for request in seen] == [1, 2]


async def test_cancel_during_model_stream_releases_the_host(tmp_path: Path) -> None:
    hang = HangResponse()
    async with running_dsh(tmp_path, responses=(hang,)) as (host, server):
        task = asyncio.create_task(host.execute(_task(), _callbacks([])))
        await asyncio.to_thread(server.wait_for_request_count, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hang.release.set()

        assert host.readiness.state is ReadinessState.READY
        server.enqueue(ToolCallResponse("fixture_complete", {"value": "after-cancel"}))
        result = await host.execute(_task("task-2"), _callbacks([]))
        assert result.result == {"ok": True, "value": "after-cancel"}


async def test_cancel_during_python_callback_cancels_in_flight_handler(tmp_path: Path) -> None:
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def hanging_fixture(request: AgentCallbackRequest) -> JsonValue:
        _ = request
        callback_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            callback_cancelled.set()
            raise
        return {"ok": True}

    async with running_dsh(
        tmp_path,
        responses=(ToolCallResponse("fixture_complete", {"value": "wait"}),),
    ) as (host, server):
        task = asyncio.create_task(host.execute(_task(), _callbacks([], fixture=hanging_fixture)))
        await asyncio.to_thread(server.wait_for_request_count, 1)
        await asyncio.wait_for(callback_started.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert callback_cancelled.is_set()
        assert host.readiness.state is ReadinessState.READY


async def test_process_exit_fails_pending_tasks_and_latches_unavailable(tmp_path: Path) -> None:
    hang = HangResponse()
    async with running_dsh(tmp_path, responses=(hang,)) as (host, server):
        task = asyncio.create_task(host.execute(_task(), _callbacks([])))
        await asyncio.to_thread(server.wait_for_request_count, 1)
        pid = host.process_id
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        with pytest.raises(AgentHostUnavailableError):
            await task
        assert host.readiness.state is ReadinessState.UNAVAILABLE
        with pytest.raises(AgentHostUnavailableError):
            await host.execute(_task("task-2"), _callbacks([]))
        hang.release.set()


async def test_unconfigured_ready_blocks_generation_without_model_call(tmp_path: Path) -> None:
    async with running_dsh(tmp_path, responses=(), configured=False) as (host, server):
        assert host.readiness.state is ReadinessState.UNCONFIGURED_READY
        assert host.readiness.model_configured is False
        with pytest.raises(AgentTaskError) as exc_info:
            await host.execute(_task(), _callbacks([]))
        assert exc_info.value.code == "binding_unconfigured"
        assert server.requests == []


async def test_boot_failure_reports_unavailable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    profile = data_dir / "dsh"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text("{not-json", encoding="utf-8")
    (profile / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    host = host_for(data_dir)
    with pytest.raises(AgentHostUnavailableError):
        await host.start()
    assert host.readiness.state is ReadinessState.UNAVAILABLE
    await host.close()


async def test_retry_policy_mismatch_is_rejected_without_provider_io(tmp_path: Path) -> None:
    async with running_dsh(
        tmp_path,
        responses=(ToolCallResponse("fixture_complete", {"value": "must-not-run"}),),
        retry_max_retries=1,
    ) as (host, server):
        with pytest.raises(AgentTaskError) as exc_info:
            await host.execute(_task(), _callbacks([]))
        assert exc_info.value.code == "retry_policy_invalid"
        assert server.requests == []


async def test_production_composition_exposes_only_chat_preset_tools(tmp_path: Path) -> None:
    budget = AgentTaskBudget(
        max_rounds=1,
        max_tool_calls_per_round=2,
        max_total_tool_calls=2,
    )
    async with running_dsh(
        tmp_path,
        responses=(TextResponse("plain text"),),
        entry_script=MAIN_HOST_SCRIPT,
    ) as (host, server):
        with pytest.raises(AgentTaskError) as exc_info:
            await host.execute(_task(budget=budget), _callbacks([]))
        assert exc_info.value.code == "budget_exhausted"
        assert len(server.requests) == 1
        assert set(server.requests[0].tool_names) == {
            "fetch_page",
            "final_response",
            "recall_memory",
            "record_favorability_delta",
            "search_knowledge",
            "search_web",
        }
        assert len(server.requests[0].user_texts) == 1
        assert "<komari_chat_task" in server.requests[0].user_texts[0]


async def test_close_fails_running_task_and_closes_process(tmp_path: Path) -> None:
    hang = HangResponse()
    async with running_dsh(tmp_path, responses=(hang,)) as (host, server):
        task = asyncio.create_task(host.execute(_task(), _callbacks([])))
        await asyncio.to_thread(server.wait_for_request_count, 1)
        hang.release.set()
        await host.close()
        with pytest.raises(AgentHostUnavailableError):
            await task
        assert host.readiness.state is ReadinessState.UNAVAILABLE
        assert host.process_id is None
        with pytest.raises(AgentHostUnavailableError):
            await host.execute(_task("task-2"), _callbacks([]))


async def _compaction_test_responses() -> tuple[ToolCallResponse, TextResponse, ToolCallResponse]:
    return (
        ToolCallResponse(
            "fixture_followup",
            {"text": "second turn", "conclude": True},
            usage=Usage(prompt_tokens=200, completion_tokens=1),
        ),
        TextResponse("summary text"),
        ToolCallResponse("fixture_complete", {"value": "after-compaction"}),
    )


async def test_native_compaction_uses_task_admission(tmp_path: Path) -> None:
    responses = await _compaction_test_responses()
    async with running_dsh(
        tmp_path,
        responses=responses,
        compaction=True,
        context_window=100,
        max_tokens=10,
    ) as (host, server):
        admissions: list[AgentCallbackRequest] = []
        result = await host.execute(_task(), _callbacks(admissions))

        assert result.result == {"ok": True, "value": "after-compaction"}
        assert [request.data["purpose"] for request in admissions] == [
            "conversation",
            "compaction",
            "conversation",
        ]
        assert [request.data["attempt"] for request in admissions] == [1, 1, 2]
        assert len(server.requests) == 3
        compaction_request = server.requests[1]
        assert any(
            "compaction engine" in text
            for text in compaction_request.user_texts
        ), "the native compaction summarizer request did not reach the controlled endpoint"


async def test_native_compaction_admission_rejection_prevents_auxiliary_provider_io(tmp_path: Path) -> None:
    responses = (
        ToolCallResponse(
            "fixture_followup",
            {"text": "second turn", "conclude": True},
            usage=Usage(prompt_tokens=200, completion_tokens=1),
        ),
        ToolCallResponse("fixture_complete", {"value": "continued"}),
    )

    async def admission(request: AgentCallbackRequest) -> JsonValue:
        if request.data["purpose"] == "compaction":
            return {"allow": False}
        return {"allow": True}

    seen: list[AgentCallbackRequest] = []

    async def record_admission(request: AgentCallbackRequest) -> JsonValue:
        seen.append(request)
        return await admission(request)

    async with running_dsh(
        tmp_path,
        responses=responses,
        compaction=True,
        context_window=100,
        max_tokens=10,
    ) as (host, server):
        result = await host.execute(_task(), _callbacks([], admission=record_admission))
        assert result.result == {"ok": True, "value": "continued"}
        assert [request.data["purpose"] for request in seen] == [
            "conversation",
            "compaction",
            "conversation",
        ]
        assert len(server.requests) == 2, "a denied compaction admission must not reach provider I/O"
        assert not any(
            "compaction engine" in text
            for request in server.requests
            for text in request.user_texts
        )


async def test_native_compaction_request_cancellation(tmp_path: Path) -> None:
    first = ToolCallResponse(
        "fixture_followup",
        {"text": "second turn", "conclude": True},
        usage=Usage(prompt_tokens=200, completion_tokens=1),
    )
    hang = HangResponse()
    async with running_dsh(
        tmp_path,
        responses=(first, hang),
        compaction=True,
        context_window=100,
        max_tokens=10,
    ) as (host, server):
        seen: list[AgentCallbackRequest] = []
        task = asyncio.create_task(host.execute(_task(), _callbacks(seen)))
        await asyncio.to_thread(server.wait_for_request_count, 2)
        assert [request.data["purpose"] for request in seen][-1] == "compaction"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hang.release.set()
        assert host.readiness.state is ReadinessState.READY

        server.enqueue(ToolCallResponse("fixture_complete", {"value": "after-compaction-cancel"}))
        result = await host.execute(_task("task-2"), _callbacks([]))
        assert result.result == {"ok": True, "value": "after-compaction-cancel"}


async def test_cancel_during_retry_attempt_releases_and_latches_no_restart(tmp_path: Path) -> None:
    hang = HangResponse()
    responses = (
        HttpErrorResponse(429, "rate limit exceeded", "rate_limit_error", "rate_limit_exceeded"),
        hang,
    )
    async with running_dsh(tmp_path, responses=responses) as (host, server):
        seen: list[AgentCallbackRequest] = []
        task = asyncio.create_task(host.execute(_task(), _callbacks(seen)))
        await asyncio.to_thread(server.wait_for_request_count, 2)
        assert [request.data["attempt"] for request in seen] == [1, 2]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hang.release.set()
        assert host.readiness.state is ReadinessState.READY


async def test_permanent_failure_releases_for_a_later_task(tmp_path: Path) -> None:
    responses = (
        HttpErrorResponse(400, "invalid request", "invalid_request_error", "invalid_request"),
        ToolCallResponse("fixture_complete", {"value": "after-failure"}),
    )
    async with running_dsh(tmp_path, responses=responses) as (host, server):
        with pytest.raises(AgentTaskError):
            await host.execute(_task(), _callbacks([]))
        result = await host.execute(_task("task-2"), _callbacks([]))
        assert result.result == {"ok": True, "value": "after-failure"}
        assert len(server.requests) == 2


async def test_two_concurrent_tasks_share_one_process_without_history_leak(tmp_path: Path) -> None:
    responses = (
        ToolCallResponse("fixture_complete", {"value": "first"}),
        ToolCallResponse("fixture_complete", {"value": "second"}),
    )
    async with running_dsh(tmp_path, responses=responses) as (host, server):
        first, second = await asyncio.gather(
            host.execute(_task("concurrent-a", "message-a"), _callbacks([])),
            host.execute(_task("concurrent-b", "message-b"), _callbacks([])),
        )
        assert first.result == {"ok": True, "value": "first"}
        assert second.result == {"ok": True, "value": "second"}
        assert len(server.requests) == 2
        body = json.dumps([request.messages for request in server.requests])
        first_index = body.find("message-a")
        second_index = body.find("message-b")
        assert first_index >= 0 and second_index >= 0
        # Each task's single user message appears in exactly one request body.
        assert sum("message-a" in json.dumps(request.messages) for request in server.requests) == 1
        assert sum("message-b" in json.dumps(request.messages) for request in server.requests) == 1


async def test_start_timeout_is_unavailable_and_does_not_restart(tmp_path: Path) -> None:
    from komari_bot.agent import AgentHost

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    slow_entry = tmp_path / "slow.mjs"
    slow_entry.write_text("setTimeout(() => {}, 60000)\n", encoding="utf-8")
    host = AgentHost(
        runtime_root=tmp_path,
        data_dir=data_dir,
        entry_script=slow_entry,
        startup_timeout=0.5,
        close_timeout=1.0,
    )
    with pytest.raises(AgentHostUnavailableError) as exc_info:
        await host.start()
    assert exc_info.value.code == "startup_timeout"
    assert host.readiness.state is ReadinessState.UNAVAILABLE
    with pytest.raises(AgentHostUnavailableError):
        await host.start()
    await host.close()


async def test_success_failure_and_cancel_leave_no_session_or_log_artifacts(tmp_path: Path) -> None:
    hang = HangResponse()
    responses = (
        ToolCallResponse("fixture_complete", {"value": "ok"}),
        HttpErrorResponse(400, "invalid request", "invalid_request_error", "invalid_request"),
        hang,
    )
    async with running_dsh(tmp_path, responses=responses) as (host, server):
        success = await host.execute(_task("release-success"), _callbacks([]))
        assert success.result == {"ok": True, "value": "ok"}
        with pytest.raises(AgentTaskError):
            await host.execute(_task("release-failure"), _callbacks([]))
        task = asyncio.create_task(host.execute(_task("release-cancel"), _callbacks([])))
        await asyncio.to_thread(server.wait_for_request_count, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hang.release.set()

    assert allowed_profile_files(tmp_path / "data") == {
        "dsh/package.json",
        "dsh/cordis.patch.yml",
    }
