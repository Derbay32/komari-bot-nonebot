"""TSK-196 复审 follow-up：图片失败最终异常 traceback frame locals 安全。

验收（公开 seam + 递归投影，不只查 ``str(exc)``/cause/context）：

- native 主 provider 多模态失败（真实 seam）：canary 同时置于 source URL
  path/query 与 data URI；最终异常 traceback 的 komari_chat 项目帧
  （``_generate_reply_core`` 及其下游仍保留的项目帧）递归/稳健渲染
  ``f_locals``，断言 canary/URL/base64/视觉描述均不存在；
- native 全部下载不可用：至少 URL path/query 不在 core frame locals；
- delegated 全部不可用：真实 ``ImageReadingSession`` 私有 sources 带
  canary；最终异常 traceback 项目帧不得经 repr/递归对象投影暴露；
- ``generate_debug_reply`` 图片失败重抛：``generate_debug_reply`` frame
  locals 已清空且最终异常仍是 ``ImageUnderstandingFailureError``、安全
  summary 保留。

测试函数自身持 canary：只筛选 komari_chat 目标帧，不把测试调用者 frame
算入。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import types
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

import komari_bot.plugins as plugins_package
from komari_bot.plugins.agent_run_logger.diagnostic import LLMDiagnosticCollector
from komari_bot.plugins.komari_chat.services.reply_context import ReplyContext

image_reading_session_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_reading_session"
)
llm_service_module = import_module(
    "komari_bot.plugins.komari_chat.services.llm_service"
)
message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)
base_client_module = import_module("komari_bot.plugins.llm_provider.base_client")

ImageUnderstandingFailureError = image_reading_session_module.ImageUnderstandingFailureError

#: 可区分 canary：URL path/query、data URI、base64 payload、视觉描述。
_CANARY_PATH_QUERY = "/tsk196/canary/photo.png?token=supersecret#frag"
_CANARY_URL = f"https://evil.example.net{_CANARY_PATH_QUERY}"
_CANARY_URL_B = "https://evil2.example.net/tsk196/second.png?token=anothersecret"
_CANARY_DATA_URI = "data:image/png;base64,QUJDREVGR0hJ"
_CANARY_BASE64 = "QUJDREVGR0hJ"
_CANARY_VISION_TEXT = "一只会飞的独角兽在彩虹桥上跳舞"


def _iter_komari_chat_frames(exc: BaseException) -> list[types.FrameType]:
    """遍历最终异常 traceback 中属于 komari_chat 生产包的帧（不含测试帧）。"""
    frames: list[types.FrameType] = []
    tb = exc.__traceback__
    while tb is not None:
        filename = (tb.tb_frame.f_code.co_filename or "").replace(os.sep, "/")
        if "komari_bot/plugins/komari_chat" in filename:
            frames.append(tb.tb_frame)
        tb = tb.tb_next
    return frames


def _render_frame_locals(frame: types.FrameType) -> str:
    """递归/稳健渲染帧局部变量为字符串（供 canary 扫描）。

    只遍历数据容器与对象实例属性（dict/list/tuple/set/dataclass 字段/
    ``__dict__``/``__slots__``）；函数/类/模块/帧/协程等可执行对象只取
    ``repr()``；深度有限并防环，避免经对象图触达模块全局或测试调用者。
    """
    visited: set[int] = set()
    parts: list[str] = []

    def _render(value: object, depth: int) -> None:
        if depth > 6:
            parts.append("<depth>")
            return
        if value is None or isinstance(value, (bool, int, float, str, bytes)):
            parts.append(repr(value))
            return
        if isinstance(
            value,
            (
                types.FrameType,
                types.FunctionType,
                types.BuiltinFunctionType,
                types.MethodType,
                types.ModuleType,
                types.CodeType,
                types.CoroutineType,
                types.GeneratorType,
                type,
            ),
        ):
            parts.append(repr(value))
            return
        value_id = id(value)
        if value_id in visited:
            parts.append("<cycle>")
            return
        visited.add(value_id)
        try:
            if isinstance(value, dict):
                parts.append("{")
                for key, item in value.items():
                    parts.append(repr(key) + ":")
                    _render(item, depth + 1)
                parts.append("}")
            elif isinstance(value, (list, tuple, set, frozenset)):
                parts.append("[")
                for item in value:
                    _render(item, depth + 1)
                parts.append("]")
            elif dataclasses.is_dataclass(value) and not isinstance(value, type):
                parts.append(type(value).__name__ + "(")
                for field in dataclasses.fields(value):
                    try:
                        _render(getattr(value, field.name), depth + 1)
                    except Exception:
                        parts.append("<unreadable>")
                parts.append(")")
            elif hasattr(value, "__dict__"):
                parts.append(type(value).__name__ + "{")
                for key, item in vars(value).items():
                    parts.append(repr(key) + ":")
                    _render(item, depth + 1)
                parts.append("}")
            elif hasattr(value, "__slots__"):
                parts.append(type(value).__name__ + "{")
                for slot in getattr(value, "__slots__", []):
                    if isinstance(slot, str):
                        try:
                            _render(getattr(value, slot), depth + 1)
                        except Exception:
                            parts.append("<unreadable>")
                parts.append("}")
            else:
                parts.append(repr(value))
        finally:
            visited.discard(value_id)

    _render(frame.f_locals, 0)
    return "".join(parts)


def _assert_frames_safe(exc: BaseException) -> None:
    """断言最终异常 traceback 的 komari_chat 帧递归投影不含任何 canary。"""
    frames = _iter_komari_chat_frames(exc)
    assert frames, "最终异常 traceback 必须包含 komari_chat 帧"
    for frame in frames:
        rendered = _render_frame_locals(frame)
        assert _CANARY_URL not in rendered, (
            f"帧 {frame.f_code.co_name} locals 泄漏原始 URL: {rendered[:200]}"
        )
        assert _CANARY_URL_B not in rendered
        assert _CANARY_PATH_QUERY not in rendered
        assert _CANARY_DATA_URI not in rendered
        assert _CANARY_BASE64 not in rendered
        assert _CANARY_VISION_TEXT not in rendered


# ── 通用替身（与 test_tsk196_native_image_failure.py 同风格）─────────────


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


def _chat_config_stub(**overrides: object) -> SimpleNamespace:
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


def _patch_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )


def _base_wire(monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace) -> Any:
    """布设 handler 与模块级替身（两种模式共用的公共部分）。"""
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
    _patch_embedding(monkeypatch)
    return handler


def _wire_native_core(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
    *,
    image_urls: list[str],
    download_results: list[str | None],
    multimodal_prompt_url: str | None = None,
    real_generate: bool = False,
) -> tuple[Any, dict[str, object]]:
    """布设 native 依赖并驱动真实 ``_generate_reply_core`` 一次。"""
    handler = _base_wire(monkeypatch, config)
    generate_kwargs: dict[str, object] = {}

    async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
        if multimodal_prompt_url is not None:
            return [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看看这张图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": multimodal_prompt_url},
                        },
                    ],
                }
            ]
        return [{"role": "user", "content": "test"}]

    async def _fake_generate(**kwargs: object) -> Any:
        generate_kwargs.update(kwargs)
        return llm_service_module.ReplyResult(
            content="回复内容",
            interaction_history={"event": "看图", "result": "描述", "emotion": "好奇"},
            favorability_delta=0,
            favorability_reason="无变化",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    if not real_generate:
        monkeypatch.setattr(
            message_handler_module, "generate_reply_with_tools", _fake_generate
        )

    async def _download_images(
        urls: list[str], _policy: object
    ) -> list[str | None]:
        del urls
        return list(download_results)

    monkeypatch.setattr(
        message_handler_module, "download_images_as_base64_aligned", _download_images
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
            request_trace_id="chat-tsk196-locals",
        )

    del _run
    return handler, generate_kwargs


class _RecordingProvider:
    def __init__(self) -> None:
        self.completions: list[Any] = []

    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        del kwargs
        return self.completions.pop(0)


def _tool_call(
    name: str,
    arguments: str,
    parsed: dict[str, Any],
    *,
    call_id: str,
) -> Any:
    return base_client_module.LLMToolCallSchema(
        id=call_id,
        type="function",
        function=base_client_module.LLMToolCallFunctionSchema(
            name=name,
            arguments=arguments,
        ),
        raw_arguments=arguments,
        parsed_arguments=parsed,
    )


class _FailingDownloader:
    """下载器恒返回 None（解码失败）：私有 sources 记录 canary URL。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.close_calls = 0

    async def download(self, url: str) -> str | None:
        self.calls.append(url)
        return None

    async def close(self) -> None:
        self.close_calls += 1


class _NeverVision:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, images: list[str], **kwargs: Any) -> list[str]:
        self.calls.append({"images": list(images), **kwargs})
        raise AssertionError("下载失败后不得触达视觉服务")


def _wire_delegated_core(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
    *,
    image_urls: list[str],
) -> tuple[Any, _FailingDownloader, _NeverVision, Any]:
    """布设 delegated：真实 ``generate_reply_with_tools`` + 真实会话（打桩下载）。

    会话私有 ``_sources`` 持有 canary URL；read_image 触发下载失败 →
    全部不可用 → 工具循环以 ``ImageUnderstandingFailureError`` 终止。
    """
    handler = _base_wire(monkeypatch, config)

    async def _fake_build_prompt(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return [{"role": "user", "content": "看图"}]

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)

    downloader = _FailingDownloader()
    vision = _NeverVision()
    monkeypatch.setattr(
        image_reading_session_module,
        "ImageDownloadSession",
        lambda _policy: downloader,
    )
    monkeypatch.setattr(image_reading_session_module, "read_images", vision)

    provider = _RecordingProvider()
    provider.completions = [
        base_client_module.LLMCompletionResultSchema(
            content="",
            tool_calls=[
                _tool_call(
                    "read_image",
                    '{"image_index": 0}',
                    {"image_index": 0},
                    call_id="call-tsk196-img",
                )
            ],
            finish_reason="tool_calls",
        )
    ]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    async def _run() -> Any:
        return await handler._generate_reply_core(
            message=_make_message(),
            recent_messages=[],
            interaction_records=[],
            image_urls=image_urls,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            request_trace_id="chat-tsk196-delegated",
        )

    del _run
    return handler, downloader, vision, provider


# ── native 主 provider 失败：真实 seam，core frame locals 清空 ──────────


def test_native_provider_failure_core_frame_locals_are_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 seam：canary 置于 source URL path/query 与 data URI；最终异常
    traceback 的 komari_chat 帧递归渲染 f_locals 不含 canary/URL/base64/
    视觉描述；最终异常 cause/context 都为 None。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler, _generate_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=[_CANARY_URL],
        download_results=[_CANARY_DATA_URI],
        multimodal_prompt_url=_CANARY_DATA_URI,
        real_generate=True,
    )

    async def _fail_completion(**kwargs: object) -> None:
        del kwargs
        msg = f"native 多模态请求失败 {_CANARY_URL} {_CANARY_DATA_URI}"
        raise RuntimeError(msg)

    monkeypatch.setattr(llm_service_module, "_call_llm_completion", _fail_completion)

    with pytest.raises(ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=[_CANARY_URL],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-tsk196-native-provider",
            )
        )

    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.error_types == ("vision_failed",)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None

    frames = _iter_komari_chat_frames(excinfo.value)
    assert any(f.f_code.co_name == "_generate_reply_core" for f in frames)
    core_frame = next(f for f in frames if f.f_code.co_name == "_generate_reply_core")
    # 关键帧显式清空
    core_locals = core_frame.f_locals
    assert core_locals.get("image_urls") is None
    assert core_locals.get("reply_context") is None
    assert core_locals.get("base64_image_urls") is None
    assert core_locals.get("reply_image_urls") is None
    assert core_locals.get("combined_sources") == []
    assert core_locals.get("prompt_messages") == []
    assert core_locals.get("collector") is None
    # 全部 komari_chat 帧递归投影安全
    _assert_frames_safe(excinfo.value)


def test_native_all_download_unavailable_core_frame_locals_are_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native 全部下载不可用：至少 URL path/query 不在 core frame locals。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler, _generate_kwargs = _wire_native_core(
        monkeypatch,
        config,
        image_urls=[_CANARY_URL, _CANARY_URL_B],
        download_results=[None, None],
    )

    with pytest.raises(ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=[_CANARY_URL, _CANARY_URL_B],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-tsk196-native-download",
            )
        )

    assert excinfo.value.summary.mode == "native"
    assert excinfo.value.summary.all_images_unavailable is True
    frames = _iter_komari_chat_frames(excinfo.value)
    core_frame = next(f for f in frames if f.f_code.co_name == "_generate_reply_core")
    rendered = _render_frame_locals(core_frame)
    assert _CANARY_PATH_QUERY not in rendered
    assert _CANARY_URL not in rendered
    assert _CANARY_URL_B not in rendered
    assert _CANARY_DATA_URI not in rendered
    assert _CANARY_BASE64 not in rendered
    _assert_frames_safe(excinfo.value)


# ── delegated 全部不可用：真实会话私有 sources 带 canary ───────────────


def test_delegated_all_unavailable_core_frame_locals_are_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 ``ImageReadingSession`` 私有 ``_sources`` 带 canary；最终异常
    traceback 项目帧不得经 repr/递归对象投影暴露；cause/context 都为 None。"""
    config = _chat_config_stub(image_understanding_mode="delegated")
    handler, downloader, vision, provider = _wire_delegated_core(
        monkeypatch,
        config,
        image_urls=[_CANARY_URL],
    )

    with pytest.raises(ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            handler._generate_reply_core(
                message=_make_message(),
                recent_messages=[],
                interaction_records=[],
                image_urls=[_CANARY_URL],
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                request_trace_id="chat-tsk196-delegated",
            )
        )

    summary = excinfo.value.summary
    assert summary.mode == "delegated"
    assert summary.all_images_unavailable is True
    assert summary.failed_images == 1
    assert summary.error_types == ("image_unavailable",)
    assert summary.stages == ("download",)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None

    # 会话确实经下载器触达（私有 sources 确实带 canary，验证 seam 有效）
    assert downloader.calls == [_CANARY_URL]
    assert vision.calls == []
    assert provider.completions == []

    frames = _iter_komari_chat_frames(excinfo.value)
    assert any(f.f_code.co_name == "_generate_reply_core" for f in frames)
    core_frame = next(f for f in frames if f.f_code.co_name == "_generate_reply_core")
    core_locals = core_frame.f_locals
    assert core_locals.get("image_session") is None
    assert core_locals.get("image_urls") is None
    assert core_locals.get("reply_context") is None
    assert core_locals.get("prompt_messages") == []
    assert core_locals.get("collector") is None
    _assert_frames_safe(excinfo.value)


# ── generate_debug_reply 图片失败重抛：frame locals 清空 ────────────────


def test_debug_reply_image_failure_reraise_clears_frame_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """debug 图片失败重抛：``generate_debug_reply`` frame 的 image_urls/
    reply_context/refetched_context/collector 已清空，最终异常仍是
    ``ImageUnderstandingFailureError`` 且安全 summary 保留；traceback 的
    komari_chat 帧递归投影不含 canary。"""
    config = _chat_config_stub(image_understanding_mode="native")
    handler = _base_wire(monkeypatch, config)

    async def _fake_build_prompt(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        return [{"role": "user", "content": "test"}]

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)

    async def _download_images(
        urls: list[str], _policy: object
    ) -> list[str | None]:
        del urls
        return [None, None]

    monkeypatch.setattr(
        message_handler_module, "download_images_as_base64_aligned", _download_images
    )

    reply_context = ReplyContext(
        source_side="user",
        message_id="ref-tsk196",
        user_id="user-1",
        user_nickname="引用用户",
        text="",
        image_sources=(_CANARY_URL_B,),
        image_count=1,
        has_visible_image=True,
    )
    collector: LLMDiagnosticCollector = LLMDiagnosticCollector(
        request_id="debug-tsk196-locals"
    )

    with pytest.raises(ImageUnderstandingFailureError) as excinfo:
        asyncio.run(
            handler.generate_debug_reply(
                group_id="debug-group-tsk196",
                user_id="user-debug-tsk196",
                user_nickname="调试用户",
                content="看图",
                image_urls=[_CANARY_URL],
                reply_context=reply_context,
                _bot=None,
                collector=collector,
            )
        )

    summary = excinfo.value.summary
    assert summary.mode == "native"
    assert summary.all_images_unavailable is True
    assert summary.failed_images == 2
    assert isinstance(excinfo.value, ImageUnderstandingFailureError)
    # collector 已安全 finalize（不因重抛丢失）
    assert collector.finalized
    assert collector.status == "error"

    frames = _iter_komari_chat_frames(excinfo.value)
    assert any(f.f_code.co_name == "generate_debug_reply" for f in frames)
    debug_frame = next(
        f for f in frames if f.f_code.co_name == "generate_debug_reply"
    )
    debug_locals = debug_frame.f_locals
    assert debug_locals.get("image_urls") is None
    assert debug_locals.get("reply_context") is None
    assert debug_locals.get("refetched_context") is None
    assert debug_locals.get("collector") is None
    _assert_frames_safe(excinfo.value)
