"""TSK-195 原始 URL 安全边界（delegated 任务级图片会话）验收测试。

断言（可观察边界，不断言私有 helper）：

- 原始 URL（含 path/query）与 base64 只存在于任务级图片会话 → 安全下载器
  边界：不进主模型 messages / 工具结果 / 普通日志 / 诊断收集器；
- 视觉子调用（``read_images``）只收到下载后的 data URI；
- 视觉描述经既有 ``render_untrusted_context`` 包裹回流，``source_type``
  恒为 ``"vision"``；
- 会话内部日志只记录安全来源标签（scheme://host），不记录 URL path/query。

本文件经公开 seam 驱动真实 ``generate_reply_with_tools`` + 真实
``ImageReadingSession``（仅打桩下载器与视觉读取），与
``test_request_mode_passthrough.py`` 共用同一 seam 风格。
"""

from __future__ import annotations

import asyncio
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

llm_service_module = import_module("komari_bot.plugins.komari_chat.services.llm_service")
image_reading_session_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_reading_session"
)
image_downloader_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_downloader"
)
base_client_module = import_module("komari_bot.plugins.llm_provider.base_client")

_RAW_URL = "https://example.com/secret/path/photo.png?token=abc123"
_DATA_URI = "data:image/png;base64,SUMSECRETUX=="


class _RecordingProvider:
    def __init__(self) -> None:
        self.completion_calls: list[dict[str, Any]] = []
        self.completions: list[Any] = []

    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        self.completion_calls.append(kwargs)
        return self.completions.pop(0)


def _build_config(**overrides: Any) -> SimpleNamespace:
    """与 ``AgentExecutionBudget.from_config`` / chat 槽位解析兼容的配置替身。"""
    values: dict[str, Any] = {
        "llm_model_chat": "chat-model",
        "llm_temperature_chat": 0.7,
        "llm_max_tokens_chat": 1024,
        "llm_thinking_mode_chat": False,
        "llm_reasoning_effort_chat": "",
        "llm_request_api_chat": "chat_completions",
        "llm_stream_enabled_chat": False,
        "agent_max_rounds": 10,
        "agent_max_tool_calls_per_round": 4,
        "agent_max_total_tool_calls": 20,
        "agent_tool_call_mode": "required",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _tool_call(
    name: str,
    arguments: str,
    parsed_arguments: dict[str, Any],
    *,
    call_id: str = "call-1",
) -> Any:
    return base_client_module.LLMToolCallSchema(
        id=call_id,
        type="function",
        function=base_client_module.LLMToolCallFunctionSchema(
            name=name,
            arguments=arguments,
        ),
        raw_arguments=arguments,
        parsed_arguments=parsed_arguments,
    )


def _final_response_completion() -> Any:
    return base_client_module.LLMCompletionResultSchema(
        content="",
        tool_calls=[
            _tool_call(
                "final_response",
                "{}",
                {
                    "content": "这只猫在窗台上。",
                    "interaction_history": {
                        "event": "看图",
                        "result": "描述",
                        "emotion": "平静",
                    },
                },
                call_id="call-final",
            )
        ],
        finish_reason="tool_calls",
    )


def _build_real_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    Any,
    list[dict[str, Any]],
    list[tuple[str, str]],
]:
    """真实任务级图片会话 + 打桩 下载/视觉 seam；返回会话与调用记录。"""
    read_images_calls: list[dict[str, Any]] = []
    log_records: list[tuple[str, str]] = []

    async def _fake_read_images(images: list[str], **kwargs: Any) -> list[str]:
        read_images_calls.append({"images": list(images), **kwargs})
        return ["一只猫在窗台上"]

    class _Downloader:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def download(self, url: str) -> str:
            self.calls.append(url)
            return _DATA_URI

        async def close(self) -> None:
            return None

    downloader = _Downloader()

    class _LogRecorder:
        def info(self, *args: object, **_kwargs: object) -> None:
            log_records.append(("info", str(args)))

        def warning(self, *args: object, **_kwargs: object) -> None:
            log_records.append(("warning", str(args)))

        def debug(self, *args: object, **_kwargs: object) -> None:
            log_records.append(("debug", str(args)))

        def error(self, *args: object, **_kwargs: object) -> None:
            log_records.append(("error", str(args)))

    monkeypatch.setattr(image_reading_session_module, "read_images", _fake_read_images)
    monkeypatch.setattr(
        image_reading_session_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    monkeypatch.setattr(image_reading_session_module, "logger", _LogRecorder())

    session = image_reading_session_module.ImageReadingSession.build(
        quoted_sources=[],
        current_sources=[_RAW_URL],
        policy=image_downloader_module.ImageDownloadPolicy(),
        vision_model="vision-model",
    )
    return session, read_images_calls, log_records


def test_delegated_raw_url_never_reaches_model_messages_or_tool_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原始 URL/base64 不进入主模型 messages 与工具结果；vision 描述带来源标签。

    使用真实会话 + 真实工具循环：read_image 工具结果只含文字描述（经
    ``render_untrusted_context`` 以 ``source_type="vision"`` 包裹），主循环
    两轮 messages 全程不含 URL path/query 与 base64。
    """
    provider = _RecordingProvider()
    provider.completions = [
        base_client_module.LLMCompletionResultSchema(
            content="",
            tool_calls=[
                _tool_call(
                    "read_image",
                    '{"image_index": 0}',
                    {"image_index": 0},
                    call_id="call-image-1",
                )
            ],
            finish_reason="tool_calls",
        ),
        _final_response_completion(),
    ]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    session, read_images_calls, _logs = _build_real_session(monkeypatch)

    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

    collector = LLMDiagnosticCollector(request_id="security-boundary-1")
    result = asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=_build_config(),
            messages=[{"role": "user", "content": "看图"}],
            tools=[llm_service_module.READ_IMAGE_TOOL],
            request_trace_id="security-boundary-1",
            image_session=session,
            collector=collector,
        )
    )

    assert result.content == "这只猫在窗台上。"
    # 视觉子调用只收到 data URI，收到不到原始 URL
    assert len(read_images_calls) == 1
    assert read_images_calls[0]["images"] == [_DATA_URI]

    # 主模型两轮 messages（含工具结果）不得出现 URL path/query 或 base64
    for call in provider.completion_calls:
        rendered = str(call["messages"])
        assert "example.com" not in rendered
        assert "/secret/" not in rendered and "photo.png" not in rendered
        assert "token=abc123" not in rendered
        assert _DATA_URI not in rendered

    # 工具结果（第二轮 user→tool 消息）以 untrusted_context source_type=vision 兜底
    rendered_all = str(provider.completion_calls)
    assert 'source_type="vision"' in rendered_all
    # 原始 URL 绝不进入诊断收集器的工具投影
    image_trace = next(
        trace for trace in collector.tools if trace.tool_name == "read_image"
    )
    assert image_trace.parsed_arguments == {"image_index": 0}
    assert _RAW_URL not in str(image_trace)
    assert _DATA_URI not in str(image_trace)


def test_vision_description_is_wrapped_with_untrusted_vision_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vision 描述经 render_untrusted_context 包裹（source_type=vision）。"""
    _session, _read_images_calls, _logs = _build_real_session(monkeypatch)

    from komari_bot.llm.untrusted_context import (
        UntrustedContext,
        render_untrusted_context,
    )

    rendered = render_untrusted_context(
        UntrustedContext(
            source_type="vision",
            source_id="vision:call-image-1",
            content="一只猫在窗台上",
        ),
        max_chars=2048,
    )
    assert 'source_type="vision"' in rendered
    assert "一只猫" in rendered


def test_session_logs_keep_only_safe_source_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会话内部日志只记录安全来源标签，不记录 URL path/query/base64。"""
    session, _read_images_calls, log_records = _build_real_session(monkeypatch)

    asyncio.run(session.read(0))

    assert log_records, "读取图片应产生会话日志"
    for level, record in log_records:
        assert _RAW_URL not in record, f"[{level}] 日志泄漏原始 URL: {record}"
        assert "/secret/" not in record and "photo.png" not in record
        assert "token=abc123" not in record
        assert _DATA_URI not in record, f"[{level}] 日志泄漏 data URI: {record}"
        assert "example.com" in record, (
            f"[{level}] 日志缺少安全来源标签（scheme://host）: {record}"
        )
