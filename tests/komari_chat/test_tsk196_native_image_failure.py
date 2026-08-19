"""TSK-196 native 模式图片失败统一通知的验收测试。

覆盖（公开 seam，不断言私有 helper）：

- native 全部图片下载失败 → 在主 LLM 之前以 ``ImageUnderstandingFailureError``
  终止（不调用 generate、不切 delegated、不触达视觉服务）；
- native 带图请求 provider 报错 → 安全包装为 mode=native 摘要（``from None``，
  不保留原异常 cause），绝不切 delegated / 不声明 read_image / 不创建会话；
- native 部分下载失败且任务成功 → 聚合摘要附加到 ``ReplyResult``；
- native 无图片的普通 LLM 错误按原样传播，不误报为图片失败；
- Agent Run：native 多模态请求失败时 LLM trace 与 final_error 不含
  URL/base64（异常正文脱敏），失败调用仍进入 Agent Run。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

import komari_bot.plugins as plugins_package

image_understanding_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_understanding"
)
image_reading_session_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_reading_session"
)
image_downloader_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_downloader"
)
llm_service_module = import_module(
    "komari_bot.plugins.komari_chat.services.llm_service"
)
message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)

if TYPE_CHECKING:
    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

ImageUnderstandingFailureError = image_reading_session_module.ImageUnderstandingFailureError

_RAW_URL = "https://example.com/secret/path/photo.png?token=abc123"
_DATA_URI = "data:image/png;base64,SUMSECRETUX=="

_CHAT_BUDGET_FIELDS: dict[str, object] = {
    "vision_image_download_max_count": 4,
    "vision_image_download_max_bytes": 8 * 1024 * 1024,
    "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
    "vision_image_download_max_pixels": 40_000_000,
    "vision_image_download_concurrency": 2,
    "vision_image_download_connect_timeout_seconds": 5.0,
    "vision_image_download_read_timeout_seconds": 30.0,
    "vision_image_download_total_timeout_seconds": 45.0,
}


class _FakeRedis:
    async def get_buffer(self, _group_id: str, limit: int = 100) -> list[object]:
        del limit
        return []

    async def push_message(self, _group_id: str, _message: object) -> None:
        return None

    async def get_global_interaction_buffer(
        self, _user_id: str, limit: int = 10
    ) -> list[dict[str, object]]:
        del limit
        return []


class _FakeMemory:
    async def search_conversations(self, **_kwargs: object) -> list[dict[str, object]]:
        return []

    async def search_interaction_events(
        self, **_kwargs: object
    ) -> list[dict[str, object]]:
        return []

    async def get_user_profile(
        self, *, user_id: str, group_id: str
    ) -> dict[str, object]:
        del user_id, group_id
        return {"display_name": "测试用户", "traits": {}}


class _FakeQueryRewrite:
    async def rewrite_query(self, current_query: str, **_kwargs: object) -> str:
        return current_query


class _FakeEmbeddingProvider:
    async def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2]


class _FakeUserData:
    def get_config(self) -> SimpleNamespace:
        return SimpleNamespace(max_favorability_delta_per_reply=5)

    async def get_user_favorability(self, _user_id: str) -> SimpleNamespace:
        return SimpleNamespace()


def _chat_config_stub(**overrides: object) -> SimpleNamespace:
    """合并 get_config / get_memory_config 字段的配置替身。"""
    values: dict[str, object] = {
        "proactive_enabled": False,
        "context_messages_limit": 10,
        "context_max_utf8_bytes": 24_000,
        "context_max_estimated_tokens": 6_000,
        "memory_search_limit": 3,
        "bot_nickname": "小鞠",
        "error_notify_enabled": False,
        "agent_max_rounds": 10,
        "agent_max_tool_calls_per_round": 4,
        "agent_max_total_tool_calls": 20,
        "agent_tool_call_mode": "required",
        "image_understanding_mode": "native",
        "llm_model_chat": "chat-model",
        "llm_temperature_chat": 0.7,
        "llm_max_tokens_chat": 1024,
        "llm_thinking_mode_chat": False,
        "llm_reasoning_effort_chat": "",
        "llm_request_api_chat": "chat_completions",
        "llm_stream_enabled_chat": False,
        **_CHAT_BUDGET_FIELDS,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _make_message(message_id: str = "img-msg-1") -> Any:
    from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

    return MessageSchema(
        user_id="user-1",
        user_nickname="测试用户",
        group_id="group-1",
        content="看看这张图",
        timestamp=1.0,
        message_id=message_id,
    )


def _vision_stub() -> SimpleNamespace:
    return SimpleNamespace(
        vision_model="vision-model",
        vision_temperature=0.3,
        vision_max_tokens=1024,
        vision_request_api="chat_completions",
        vision_stream_enabled=False,
        vision_thinking_mode=False,
        vision_reasoning_effort="",
    )


def _wire_native_core(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
    *,
    image_urls: list[str] | None,
    download_results: list[str | None] | None = None,
    build_prompt_multimodal: bool = False,
    generate_error: BaseException | None = None,
    generate_result: Any = None,
    real_generate: bool = False,
) -> tuple[
    Any,
    dict[str, object],
    list[list[str]],
    dict[str, object],
]:
    """布设 native 依赖并驱动真实 ``_generate_reply_core`` 一次。

    Returns:
        (handler, generate_kwargs, download_batches, build_prompt_kwargs)
    """
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = _FakeRedis()
    handler.memory = _FakeMemory()
    handler.query_rewrite = _FakeQueryRewrite()

    monkeypatch.setattr(message_handler_module, "get_config", lambda: config)
    monkeypatch.setattr(message_handler_module, "get_memory_config", lambda: config)
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserData())
    monkeypatch.setattr(
        message_handler_module,
        "llm_provider_config_manager",
        SimpleNamespace(get=lambda: _vision_stub()),
    )
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )

    build_prompt_kwargs: dict[str, object] = {}

    async def _fake_build_prompt(**kwargs: object) -> list[dict[str, object]]:
        build_prompt_kwargs.update(kwargs)
        if build_prompt_multimodal:
            return [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看看这张图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "base64:https://example.com/a.png"},
                        },
                    ],
                }
            ]
        return [{"role": "user", "content": "test"}]

    generate_kwargs: dict[str, object] = {}

    async def _fake_generate_with_tools(**kwargs: object) -> Any:
        generate_kwargs.update(kwargs)
        if generate_error is not None:
            raise generate_error
        return (
            generate_result
            if generate_result is not None
            else llm_service_module.ReplyResult(
                content="回复内容",
                interaction_history={
                    "event": "看图",
                    "result": "描述",
                    "emotion": "好奇",
                },
                favorability_delta=0,
                favorability_reason="无变化",
            )
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    if not real_generate:
        monkeypatch.setattr(
            message_handler_module,
            "generate_reply_with_tools",
            _fake_generate_with_tools,
        )

    download_batches: list[list[str]] = []

    async def _download_images(
        urls: list[str],
        _policy: object,
    ) -> list[str | None]:
        download_batches.append(urls)
        if download_results is not None:
            return list(download_results)
        return [f"base64:{url}" for url in urls]

    monkeypatch.setattr(
        message_handler_module,
        "download_images_as_base64_aligned",
        _download_images,
    )

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )

    async def _run() -> Any:
        return await handler._generate_reply_core(
            message=_make_message(),
            recent_messages=[],
            interaction_records=[],
            image_urls=image_urls,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            request_trace_id="chat-native-tsk196",
        )

    return handler, generate_kwargs, download_batches, build_prompt_kwargs


# ── native 全部下载失败：主 LLM 之前终止 ───────────────────────────────


def test_native_all_downloads_unavailable_terminates_before_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 全部图片下载失败 → 主 LLM 之前终止，不切 delegated。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler, generate_kwargs, download_batches, _build_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=[
            "https://example.com/a.png",
            "https://example.com/b.png",
        ],
        download_results=[None, None],
    )

    with pytest.raises(ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=[
                    "https://example.com/a.png",
                    "https://example.com/b.png",
                ],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-native-tsk196",
            )
        )

    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.all_images_unavailable is True
    assert summary.total_images == 2
    assert summary.attempted_images == 2
    assert summary.failed_images == 2
    assert summary.error_types == ("image_unavailable",)
    assert summary.stages == ("download",)
    # 异常正文安全：无 URL/base64
    assert "https://" not in str(excinfo.value)
    assert "base64" not in str(excinfo.value)
    # 未进入主 LLM：generate 未被调用
    assert generate_kwargs == {}
    assert download_batches == [
        ["https://example.com/a.png", "https://example.com/b.png"]
    ]


# ── native 多模态 provider 失败：安全包装，不切 delegated ──────────────


def test_native_multimodal_provider_failure_wraps_without_mode_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 带图请求 provider 报错 → 安全包装 mode=native 摘要（from None）。

    真实 seam（``_execute_tool_loop`` 对 ``_call_llm_completion`` 的 except）把
    主 provider 多模态调用失败收敛为窄 marker ``NativeMultimodalRequestError``
    （不携带原异常 cause/正文）；此处 fake generate 直接抛该 marker 以驱动
    message_handler 的包装边界。
    """
    config = _chat_config_stub(image_understanding_mode="native")
    handler, generate_kwargs, download_batches, build_prompt_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=["https://example.com/a.png"],
        download_results=["base64:https://example.com/a.png"],
        build_prompt_multimodal=True,
        generate_error=llm_service_module.NativeMultimodalRequestError(),
    )

    with pytest.raises(ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=["https://example.com/a.png"],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-native-fail-1",
            )
        )

    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.all_images_unavailable is False
    assert summary.total_images == 1
    assert summary.attempted_images == 1
    assert summary.failed_images == 1
    assert summary.error_types == ("vision_failed",)
    assert summary.stages == ("vision",)
    # from None：不保留原异常 cause / 正文
    assert excinfo.value.__cause__ is None
    assert "chat provider 拒绝" not in str(excinfo.value)
    assert _RAW_URL not in str(excinfo.value)
    assert _DATA_URI not in str(excinfo.value)
    # 不切 delegated：不声明 read_image、不创建图片会话、不触达视觉服务
    tools = generate_kwargs.get("tools")
    tool_names: set[str] = set()
    if isinstance(tools, list):
        tool_names = {str(tool["function"]["name"]) for tool in tools}
    assert "read_image" not in tool_names
    assert generate_kwargs.get("image_session") is None
    assert build_prompt_kwargs.get("delegated_image_mode") is False
    assert download_batches == [["https://example.com/a.png"]]


# ── native 复审：generic 错误不包装、provider 失败 failed_count=total ──


def test_native_generic_errors_with_images_are_not_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 带图请求但 generic 错误（MaxRounds/工具预算/协议校验/内部）
    不包装为图片失败，原样传播。

    TSK-196 复审：只包装“主 provider 多模态调用失败”的窄 marker；其他异常
    即便请求带图也绝不误报 vision_failed。
    """
    config = _chat_config_stub(image_understanding_mode="native")
    handler, _generate_kwargs, _download_batches, _build_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=["https://example.com/a.png"],
        download_results=["base64:https://example.com/a.png"],
        build_prompt_multimodal=True,
        generate_error=RuntimeError(
            "vision_tool 达到最大轮数或工具预算上限，模型仍未完成 final_response"
        ),
    )

    with pytest.raises(RuntimeError, match="达到最大轮数") as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=["https://example.com/a.png"],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-native-generic-1",
            )
        )

    # 是普通 RuntimeError（MaxRounds 等），不是图片失败专用异常
    assert not isinstance(
        excinfo.value,
        image_reading_session_module.ImageUnderstandingFailureError,
    )


def test_native_provider_failure_counts_all_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 3 张图全部下载成功但 provider 整体失败：failed_count 必须为
    total_images（所有进入有效范围的图片都未被成功理解），不是 0+1。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler, _generate_kwargs, _download_batches, _build_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=[
            "https://example.com/a.png",
            "https://example.com/b.png",
            "https://example.com/c.png",
        ],
        download_results=[
            "base64:https://example.com/a.png",
            "base64:https://example.com/b.png",
            "base64:https://example.com/c.png",
        ],
        build_prompt_multimodal=True,
        generate_error=llm_service_module.NativeMultimodalRequestError(),
    )

    with pytest.raises(
        image_reading_session_module.ImageUnderstandingFailureError
    ) as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=[
                    "https://example.com/a.png",
                    "https://example.com/b.png",
                    "https://example.com/c.png",
                ],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-native-provider-total",
            )
        )

    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.total_images == 3
    assert summary.attempted_images == 3
    assert summary.failed_images == 3, "provider 整体失败时失败数量必须等于 total_images"
    assert summary.error_types == ("vision_failed",)
    assert summary.stages == ("vision",)


def test_native_partial_download_failure_plus_provider_failure_counts_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 部分下载失败后 provider 也失败：failed_count 仍为 total_images，
    error_types/stages 同时包含 download+vision。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler, _generate_kwargs, _download_batches, _build_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=[
            "https://example.com/bad.png",
            "https://example.com/ok.png",
            "https://example.com/ok2.png",
        ],
        download_results=[
            None,
            "base64:https://example.com/ok.png",
            "base64:https://example.com/ok2.png",
        ],
        build_prompt_multimodal=True,
        generate_error=llm_service_module.NativeMultimodalRequestError(),
    )

    with pytest.raises(
        image_reading_session_module.ImageUnderstandingFailureError
    ) as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=[
                    "https://example.com/bad.png",
                    "https://example.com/ok.png",
                    "https://example.com/ok2.png",
                ],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-native-partial-provider",
            )
        )

    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.total_images == 3
    assert summary.failed_images == 3, "部分下载失败 + provider 失败时失败数量仍等于 total_images"
    assert summary.error_types == ("image_unavailable", "vision_failed")
    assert summary.stages == ("download", "vision")


# ── native 部分下载失败 + 任务成功：聚合摘要附加到结果 ─────────────────


def test_native_partial_download_failure_success_attaches_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 部分下载失败且任务成功 → ReplyResult 携带 mode=native 摘要。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler, generate_kwargs, _download_batches, _build_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=[
            "https://example.com/bad.png",
            "https://example.com/ok.png",
        ],
        download_results=[None, "base64:https://example.com/ok.png"],
    )

    result = asyncio.run(
        handler._generate_reply_core(
            message=_make_message(),
            recent_messages=[],
            interaction_records=[],
            image_urls=[
                "https://example.com/bad.png",
                "https://example.com/ok.png",
            ],
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            request_trace_id="chat-native-partial-1",
        )
    )

    assert result.content == "回复内容"
    assert result.image_failure_summary is not None
    assert result.image_failure_summary.mode == "native"
    assert result.image_failure_summary.failed_images == 1
    assert result.image_failure_summary.error_types == ("image_unavailable",)
    assert result.image_failure_summary.stages == ("download",)
    assert generate_kwargs  # 任务继续进入主 LLM


# ── native 无图片：普通 LLM 错误不包装 ─────────────────────────────────


def test_native_no_images_plain_llm_error_propagates_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 无图片时的普通 LLM 错误按原样传播，不误报为图片失败。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler, _generate_kwargs, download_batches, _build_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=None,
        generate_error=RuntimeError("普通文本生成失败"),
    )

    with pytest.raises(RuntimeError, match="普通文本生成失败"):
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=None,
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-native-noplain-1",
            )
        )

    assert download_batches == []


# ── Agent Run：native 多模态失败脱敏 + 失败调用进入采集 ────────────────


def test_agent_run_native_multimodal_failure_redacts_exception_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 多模态请求失败：失败 LLM trace 进入 Agent Run，且异常正文
    内嵌的 URL/base64 被脱敏；final_error 只有安全模式摘要。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler, _generate_kwargs, _download_batches, _build_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=["https://example.com/a.png"],
        download_results=["base64:https://example.com/a.png"],
        build_prompt_multimodal=True,
        real_generate=True,
    )

    async def _fail_completion(**kwargs: object) -> None:
        del kwargs
        msg = f"native 多模态请求失败 {_RAW_URL} {_DATA_URI}"
        raise RuntimeError(msg)

    # 直接替换底层 completion 调用（跳过瞬时重试延迟），仍走真实
    # ``_execute_tool_loop`` 的 record_failed_call + 安全包装路径。
    monkeypatch.setattr(llm_service_module, "_call_llm_completion", _fail_completion)

    from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector

    collector: LLMDiagnosticCollector = LLMDiagnosticCollector(
        request_id="trace-native-redact-1"
    )
    with pytest.raises(ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=["https://example.com/a.png"],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="trace-native-redact-1",
                collector=collector,
            )
        )

    assert excinfo.value.summary.mode == "native"
    # 失败调用仍进入 Agent Run：至少一条 LLM trace 且状态 error
    assert collector.calls, "失败调用必须进入 Agent Run"
    assert all(call.status == "error" for call in collector.calls)
    collector.mark_finished(status="error", error=excinfo.value)
    record = collector.build_record()
    record_json = json.dumps(record, ensure_ascii=False, default=str)
    assert _RAW_URL not in record_json
    assert _DATA_URI not in record_json
    assert "example.com" not in record_json
    assert "secret/path" not in record_json
    assert "token=abc123" not in record_json
    # LLM trace 错误消息被归一化为安全文本
    failed_call = collector.calls[0]
    assert failed_call.error is not None
    assert "RuntimeError" in failed_call.error["message"]
    assert _RAW_URL not in failed_call.error["message"]
    assert _DATA_URI not in failed_call.error["message"]
