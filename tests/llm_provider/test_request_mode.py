"""网关公共 API 的请求协议参数解析与 continuation 占位测试。"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from komari_bot.plugins.llm_provider.base_client import (
    LLMCompletionResultSchema,
    LLMProviderContinuationSchema,
)

llm_provider_module = import_module("komari_bot.plugins.llm_provider.__init__")


class TestContinuationSchema:
    """LLMProviderContinuationSchema 与完成结果占位字段。"""

    def test_completion_result_continuation_defaults_to_none(self) -> None:
        result = LLMCompletionResultSchema(content="ok")
        assert result.continuation is None

    def test_continuation_schema_shape(self) -> None:
        continuation = LLMProviderContinuationSchema(
            api="responses",
            output_items=[{"type": "message", "id": "msg_1"}],
        )
        assert continuation.api == "responses"
        assert continuation.output_items == [{"type": "message", "id": "msg_1"}]

    def test_continuation_schema_rejects_non_responses_api(self) -> None:
        with pytest.raises(ValidationError):
            LLMProviderContinuationSchema(
                api="chat_completions",  # type: ignore[arg-type]
                output_items=[],
            )


class _RecordingClient:
    """记录最后一次调用参数的假客户端。"""

    def __init__(self) -> None:
        self.last_call: dict[str, Any] | None = None

    async def generate_text(self, **kwargs: Any) -> LLMCompletionResultSchema:
        self.last_call = {"method": "generate_text", **kwargs}
        return LLMCompletionResultSchema(content="ok")

    async def generate_text_with_messages(
        self, **kwargs: Any
    ) -> LLMCompletionResultSchema:
        self.last_call = {"method": "generate_text_with_messages", **kwargs}
        return LLMCompletionResultSchema(content="ok")

    async def close(self) -> None:
        return None


class _RecordingPool:
    """提供固定假客户端的连接池替身。"""

    def __init__(self, client: _RecordingClient) -> None:
        self._client = client

    def acquire(self) -> Any:
        pool = self

        class _Lease:
            async def __aenter__(self) -> _RecordingClient:
                return pool._client

            async def __aexit__(self, *_args: object) -> None:
                return None

        return _Lease()


def _install_fakes(monkeypatch: Any, **config_overrides: Any) -> _RecordingClient:
    """替换模块级 config_manager 与 _client_pool 为测试替身。"""
    base: dict[str, Any] = {
        "request_api": "chat_completions",
        "stream_enabled": False,
        "summary_task_rpm_limit": 100,
        "chat_rpm_limit": 100,
    }
    base.update(config_overrides)
    monkeypatch.setattr(
        llm_provider_module,
        "config_manager",
        SimpleNamespace(get=lambda: SimpleNamespace(**base)),
    )
    client = _RecordingClient()
    monkeypatch.setattr(
        llm_provider_module, "_client_pool", _RecordingPool(client)
    )
    return client


class TestPublicApiRequestModeResolution:
    """四个公开入口的 request_api / stream_enabled 解析与透传。"""

    def test_generate_text_defaults_from_config_snapshot(
        self, monkeypatch: Any
    ) -> None:
        client = _install_fakes(monkeypatch)

        result = asyncio.run(
            llm_provider_module.generate_text(prompt="你好", model="m1")
        )

        assert result == "ok"
        assert client.last_call is not None
        assert client.last_call["request_api"] == "chat_completions"
        assert client.last_call["stream_enabled"] is False

    def test_generate_completion_defaults_from_config_snapshot(
        self, monkeypatch: Any
    ) -> None:
        client = _install_fakes(
            monkeypatch, request_api="responses", stream_enabled=True
        )

        result = asyncio.run(
            llm_provider_module.generate_completion(prompt="你好", model="m1")
        )

        assert result.content == "ok"
        assert client.last_call is not None
        assert client.last_call["request_api"] == "responses"
        assert client.last_call["stream_enabled"] is True

    def test_generate_text_with_messages_defaults_from_config(
        self, monkeypatch: Any
    ) -> None:
        client = _install_fakes(monkeypatch, stream_enabled=True)

        result = asyncio.run(
            llm_provider_module.generate_text_with_messages(
                messages=[{"role": "user", "content": "你好"}], model="m1"
            )
        )

        assert result == "ok"
        assert client.last_call is not None
        assert client.last_call["request_api"] == "chat_completions"
        assert client.last_call["stream_enabled"] is True

    def test_generate_messages_completion_defaults_from_config(
        self, monkeypatch: Any
    ) -> None:
        client = _install_fakes(monkeypatch, request_api="responses")

        result = asyncio.run(
            llm_provider_module.generate_messages_completion(
                messages=[{"role": "user", "content": "你好"}], model="m1"
            )
        )

        assert result.content == "ok"
        assert client.last_call is not None
        assert client.last_call["request_api"] == "responses"
        assert client.last_call["stream_enabled"] is False

    def test_explicit_params_override_config(self, monkeypatch: Any) -> None:
        client = _install_fakes(monkeypatch)

        asyncio.run(
            llm_provider_module.generate_messages_completion(
                messages=[{"role": "user", "content": "你好"}],
                model="m1",
                request_api="responses",
                stream_enabled=True,
            )
        )

        assert client.last_call is not None
        assert client.last_call["request_api"] == "responses"
        assert client.last_call["stream_enabled"] is True

    def test_partial_explicit_params_fall_back_individually(
        self, monkeypatch: Any
    ) -> None:
        client = _install_fakes(
            monkeypatch, request_api="responses", stream_enabled=True
        )

        asyncio.run(
            llm_provider_module.generate_text(
                prompt="你好", model="m1", stream_enabled=False
            )
        )

        assert client.last_call is not None
        # stream 显式 False，request_api 回退配置快照
        assert client.last_call["request_api"] == "responses"
        assert client.last_call["stream_enabled"] is False
