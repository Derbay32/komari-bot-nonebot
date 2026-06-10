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
            deepseek_api_token="token",
            deepseek_api_base="https://example.test/v1",
            deepseek_timeout_seconds=30,
        )


class _FakeCompletions:
    active: ClassVar[int] = 0
    max_active: ClassVar[int] = 0
    fail_next: ClassVar[bool] = False

    async def create(self, **kwargs: Any) -> SimpleNamespace:
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
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=f"图片描述：{image_url}"),
                    )
                ],
            )
        finally:
            self.__class__.active -= 1


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeAsyncOpenAI:
    def __init__(self, **_kwargs: Any) -> None:
        self.chat = _FakeChat()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _patch_vision_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeCompletions.active = 0
    _FakeCompletions.max_active = 0
    _FakeCompletions.fail_next = False
    monkeypatch.setattr(vision_service_module, "llm_provider_config_manager", _FakeConfigManager())
    monkeypatch.setattr(vision_service_module, "AsyncOpenAI", _FakeAsyncOpenAI)


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
    assert _FakeCompletions.max_active <= 2


@pytest.mark.asyncio
async def test_read_images_returns_empty_list_for_empty_input() -> None:
    assert await vision_service_module.read_images([], vision_model="vision-model") == []


@pytest.mark.asyncio
async def test_read_images_formats_single_image_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_vision_dependencies(monkeypatch)
    _FakeCompletions.fail_next = True

    result = await vision_service_module.read_images(
        ["image-1"],
        vision_model="vision-model",
    )

    assert result == ["[图片读取失败: 视觉模型故障]"]
