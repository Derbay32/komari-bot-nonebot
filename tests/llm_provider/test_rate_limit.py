"""LLM Provider RPM 分桶限流测试。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from importlib import import_module
from typing import Any

import pytest
from pydantic import ValidationError

from komari_bot.plugins.llm_provider.config_schema import DynamicConfigSchema

llm_provider_module = import_module("komari_bot.plugins.llm_provider.__init__")


def test_dynamic_config_schema_includes_rpm_limits() -> None:
    config = DynamicConfigSchema()

    assert config.summary_task_rpm_limit == 20
    assert config.chat_rpm_limit == 60


@pytest.mark.parametrize(
    "field_name",
    ["summary_task_rpm_limit", "chat_rpm_limit"],
)
def test_dynamic_config_schema_rejects_invalid_rpm_limits(field_name: str) -> None:
    with pytest.raises(ValidationError):
        match field_name:
            case "summary_task_rpm_limit":
                DynamicConfigSchema(summary_task_rpm_limit=0)
            case "chat_rpm_limit":
                DynamicConfigSchema(chat_rpm_limit=0)
            case _:
                raise AssertionError


@pytest.mark.parametrize(
    "phase",
    [
        "summary_json_mode",
        "summary_tool_calling",
        "summary_direct_output",
        "profile_agent",
        "forgetting_fuzzify",
        "forgetting_interaction_fuzzify",
        "interaction_event_summary",
        "chat_memory_summary",
        "group_history_summary",
    ],
)
def test_resolve_rate_limit_bucket_for_summary_phases(phase: str) -> None:
    assert llm_provider_module._resolve_rate_limit_bucket(phase) == "summary"


@pytest.mark.parametrize(
    "phase",
    [
        "normal_reply_round_1",
        "vision_tool_round_1",
        "vision_search_tool_round_1",
        "search_tool_round_1",
        "profile_tool_round_1",
        "tool_round_1",
        "query_rewrite",
        "memory_reply",
        "chat_reply",
        "",
        "future_unknown_phase",
    ],
)
def test_resolve_rate_limit_bucket_for_chat_and_unknown_phases(phase: str) -> None:
    assert llm_provider_module._resolve_rate_limit_bucket(phase) == "chat"


@pytest.mark.asyncio
async def test_sliding_window_waits_for_same_bucket_limit(monkeypatch: Any) -> None:
    monkeypatch.setattr(llm_provider_module, "_RATE_LIMIT_WINDOW_SECONDS", 0.02)
    limiter = llm_provider_module._AsyncSlidingWindowRateLimiter(lambda: 2)

    await limiter.wait()
    await limiter.wait()
    start_time = llm_provider_module.time.monotonic()
    await limiter.wait()

    assert llm_provider_module.time.monotonic() - start_time >= 0.015


@pytest.mark.asyncio
async def test_rate_limit_buckets_do_not_block_each_other(monkeypatch: Any) -> None:
    monkeypatch.setattr(llm_provider_module, "_RATE_LIMIT_WINDOW_SECONDS", 1.0)
    summary_limiter = llm_provider_module._AsyncSlidingWindowRateLimiter(lambda: 1)
    chat_limiter = llm_provider_module._AsyncSlidingWindowRateLimiter(lambda: 1)

    await summary_limiter.wait()
    blocked_summary_task = asyncio.create_task(summary_limiter.wait())
    await asyncio.sleep(0)

    assert not blocked_summary_task.done()
    await asyncio.wait_for(chat_limiter.wait(), timeout=0.02)

    blocked_summary_task.cancel()
    with suppress(asyncio.CancelledError):
        await blocked_summary_task


@pytest.mark.asyncio
async def test_rate_limiter_uses_updated_config_for_waiters(monkeypatch: Any) -> None:
    monkeypatch.setattr(llm_provider_module, "_RATE_LIMIT_WINDOW_SECONDS", 1.0)
    current_limit = 1
    limiter = llm_provider_module._AsyncSlidingWindowRateLimiter(lambda: current_limit)

    await limiter.wait()
    waiting_task = asyncio.create_task(limiter.wait())
    await asyncio.sleep(0)
    assert not waiting_task.done()

    current_limit = 2
    async with limiter._condition:
        limiter._condition.notify_all()

    await asyncio.wait_for(waiting_task, timeout=0.02)
