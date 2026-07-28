"""Komari Chat 视觉读图服务测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

vision_service_module = import_module("komari_bot.plugins.komari_chat.services.vision_service")


class _FakeConfigManager:
    @staticmethod
    def get() -> SimpleNamespace:
        return SimpleNamespace(
            api_token="token",
            api_base="https://example.test/v1",
            timeout_seconds=30,
        )


class _FakeLLMProvider:
    """伪造 llm_provider，模拟 generate_messages_completion 视觉调用。"""

    active: ClassVar[int] = 0
    max_active: ClassVar[int] = 0
    fail_next: ClassVar[bool] = False

    @classmethod
    def reset(cls) -> None:
        cls.active = 0
        cls.max_active = 0
        cls.fail_next = False

    async def generate_messages_completion(self, **kwargs: Any) -> SimpleNamespace:
        self.__class__.active += 1
        self.__class__.max_active = max(
            self.__class__.max_active,
            self.__class__.active,
        )
        try:
            await asyncio.sleep(0.01)
            if self.__class__.fail_next:
                self.__class__.fail_next = False
                msg = "视觉模型故障"
                raise RuntimeError(msg)
            image_url = kwargs["messages"][0]["content"][1]["image_url"]["url"]
            return SimpleNamespace(
                content=f"图片描述：{image_url}",
                finish_reason="stop",
                duration_ms=50.0,
                usage=None,
            )
        finally:
            self.__class__.active -= 1


def _patch_vision_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLLMProvider.reset()
    monkeypatch.setattr(vision_service_module, "llm_provider_config_manager", _FakeConfigManager())
    monkeypatch.setattr(vision_service_module, "llm_provider", _FakeLLMProvider())


@pytest.mark.asyncio
async def test_read_images_limits_model_call_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_vision_dependencies(monkeypatch)
    monkeypatch.setattr(vision_service_module, "_VISION_READ_SEMAPHORE", asyncio.Semaphore(2))

    result = await vision_service_module.read_images(
        ["image-1", "image-2", "image-3", "image-4"],
        vision_model="vision-model",
    )

    assert result == [
        "图片描述：image-1",
        "图片描述：image-2",
        "图片描述：image-3",
        "图片描述：image-4",
    ]
    assert _FakeLLMProvider.max_active <= 2


@pytest.mark.asyncio
async def test_read_images_returns_empty_list_for_empty_input() -> None:
    assert await vision_service_module.read_images([], vision_model="vision-model") == []


@pytest.mark.asyncio
async def test_read_images_formats_single_image_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_vision_dependencies(monkeypatch)
    _FakeLLMProvider.fail_next = True

    result = await vision_service_module.read_images(
        ["image-1"],
        vision_model="vision-model",
    )

    assert result == ["[图片读取失败: 视觉模型故障]"]


@pytest.mark.asyncio
async def test_read_images_passes_trace_to_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视觉调用在传入 collector 时记录 LLMCallTrace。"""
    _patch_vision_dependencies(monkeypatch)
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

    collector = LLMDiagnosticCollector(request_id="test-vision-trace")
    result = await vision_service_module.read_images(
        ["image-trace"],
        vision_model="vision-model",
        request_trace_id="trace-1",
        parent_call_id="parent-1",
        collector=collector,
    )

    assert result == ["图片描述：image-trace"]
    assert len(collector.calls) == 1
    assert collector.calls[0].phase == "vision_read_image"
    assert collector.calls[0].parent_call_id == "parent-1"


@pytest.mark.asyncio
async def test_read_images_records_error_in_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """视觉调用失败时记录错误到 collector。"""
    _patch_vision_dependencies(monkeypatch)
    _FakeLLMProvider.fail_next = True
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

    collector = LLMDiagnosticCollector(request_id="test-vision-error")
    result = await vision_service_module.read_images(
        ["image-fail"],
        vision_model="vision-model",
        collector=collector,
    )

    assert "图片读取失败" in result[0]
    assert len(collector.errors) >= 1
    assert collector.errors[0]["type"] == "RuntimeError"
