"""TSK-194/ADR-0010 图片理解模式（native/delegated）验收测试。

覆盖：
- ``ImageUnderstandingPolicy`` 冻结值对象：模式解析、非法/缺失模式明确
  失败、下载预算委托 ``ImageDownloadPolicy`` 且保留跨字段约束、不可变；
- ``_generate_reply_core`` 按模式切换：native 时图片作为多模态输入嵌入
  (user) 消息且不暴露 ``read_image`` 工具（不把 base64 交给工具循环），
  delegated 时暴露 ``read_image`` 并把下载后的 base64 交给视觉子调用；
- 两种模式共用同一份下载预算与同一 chat 槽位主循环（主循环模型选择见
  ``test_request_mode_passthrough.py`` 的 chat 槽位断言）。

本文件沿用 ``test_agent_budget.py`` 的测试 seam：公开无副作用生成核心
（``_generate_reply_core``）配合可控 provider 与业务工具替身；只断言
可观察行为（build_prompt 输入 / 工具集合 / base64 传递 / 下载批次），
不断言私有 helper。
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import types
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

import komari_bot.plugins as plugins_package

image_understanding_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_understanding"
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

ImageUnderstandingPolicy = image_understanding_module.ImageUnderstandingPolicy


# ── 基础替身 ────────────────────────────────────────────────────────────


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
        # TSK-194：图片理解模式（生产默认 delegated）
        "image_understanding_mode": "delegated",
        "vision_image_download_max_count": 4,
        "vision_image_download_max_bytes": 8 * 1024 * 1024,
        "vision_image_download_total_max_bytes": 20 * 1024 * 1024,
        "vision_image_download_max_pixels": 40_000_000,
        "vision_image_download_concurrency": 2,
        "vision_image_download_connect_timeout_seconds": 5.0,
        "vision_image_download_read_timeout_seconds": 30.0,
        "vision_image_download_total_timeout_seconds": 45.0,
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


def _wire_generate_core(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
    *,
    image_urls: list[str] | None,
) -> tuple[
    Any,
    dict[str, object],
    list[list[str]],
    dict[str, object],
]:
    """布设依赖并驱动真实 ``_generate_reply_core`` 一次。

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
        SimpleNamespace(
            get=lambda: SimpleNamespace(
                vision_model="vision-model",
                vision_temperature=0.3,
                vision_max_tokens=1024,
                vision_request_api="chat_completions",
                vision_stream_enabled=False,
            )
        ),
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
        return [{"role": "user", "content": "test"}]

    generate_kwargs: dict[str, object] = {}

    async def _fake_generate_with_tools(**kwargs: object) -> Any:
        generate_kwargs.update(kwargs)
        return llm_service_module.ReplyResult(
            content="回复内容",
            interaction_history={"event": "看图", "result": "描述", "emotion": "好奇"},
            favorability_delta=0,
            favorability_reason="无变化",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(
        message_handler_module, "generate_reply_with_tools", _fake_generate_with_tools
    )

    download_batches: list[list[str]] = []

    async def _download_images(
        urls: list[str],
        _policy: object,
    ) -> list[str | None]:
        download_batches.append(urls)
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
            request_trace_id="chat-img-msg-1",
        )

    result = asyncio.run(_run())
    assert result.favorability_delta == 0  # 走通成功路径
    return handler, generate_kwargs, download_batches, build_prompt_kwargs


# ── 冻结值对象 ──────────────────────────────────────────────────────────


def test_policy_from_config_parses_delegated_mode_and_budgets() -> None:
    """delegated 模式 + 预算逐项解析，预算委托 ImageDownloadPolicy。"""
    config = SimpleNamespace(
        image_understanding_mode="delegated",
        vision_image_download_max_count=6,
        vision_image_download_max_bytes=9 * 1024 * 1024,
        vision_image_download_total_max_bytes=21 * 1024 * 1024,
        vision_image_download_max_pixels=41_000_000,
        vision_image_download_concurrency=3,
        vision_image_download_connect_timeout_seconds=6.0,
        vision_image_download_read_timeout_seconds=31.0,
        vision_image_download_total_timeout_seconds=46.0,
    )
    policy = ImageUnderstandingPolicy.from_config(config)
    assert policy.mode == "delegated"
    assert policy.is_delegated is True
    assert policy.download.max_images == 6
    assert policy.download.max_image_bytes == 9 * 1024 * 1024
    assert policy.download.max_total_bytes == 21 * 1024 * 1024
    assert policy.download.max_pixels == 41_000_000
    assert policy.download.concurrency == 3
    assert policy.download.connect_timeout_seconds == 6.0
    assert policy.download.read_timeout_seconds == 31.0
    assert policy.download.total_timeout_seconds == 46.0


def test_policy_from_config_native_mode_uses_download_defaults() -> None:
    """native 模式解析；缺失预算字段回退 ImageDownloadPolicy 默认值。"""
    policy = ImageUnderstandingPolicy.from_config(
        SimpleNamespace(image_understanding_mode="native")
    )
    assert policy.mode == "native"
    assert policy.is_delegated is False
    assert policy.download.max_images == image_downloader_module._DEFAULT_MAX_IMAGE_COUNT
    assert (
        policy.download.max_total_bytes
        == image_downloader_module._DEFAULT_MAX_TOTAL_BYTES
    )


def test_policy_from_config_missing_mode_fails_fast() -> None:
    """缺失模式字段明确失败，不做旧快照/测试替身兼容。"""
    with pytest.raises(RuntimeError, match="image_understanding_mode"):
        ImageUnderstandingPolicy.from_config(SimpleNamespace())


def test_policy_from_config_invalid_mode_fails_fast() -> None:
    """非法模式值明确失败，绝不静默回退或自动降级。"""
    with pytest.raises(RuntimeError, match="native 或 delegated"):
        ImageUnderstandingPolicy.from_config(
            SimpleNamespace(image_understanding_mode="auto")
        )
    with pytest.raises(RuntimeError, match="native 或 delegated"):
        ImageUnderstandingPolicy.from_config(
            SimpleNamespace(image_understanding_mode="")
        )


def test_policy_frozen_and_reproducible() -> None:
    """冻结值对象不可变；同配置两次读取结果相等且互不共享。"""
    config = SimpleNamespace(image_understanding_mode="delegated")
    first = ImageUnderstandingPolicy.from_config(config)
    second = ImageUnderstandingPolicy.from_config(config)
    assert first == second
    assert first is not second
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.mode = "native"  # type: ignore[misc]


# ── _generate_reply_core 模式切换 ──────────────────────────────────────


def test_native_mode_embeds_images_and_hides_read_image_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native：图片下载后交 build_prompt 多模态嵌入，不暴露 read_image。"""
    config = _chat_config_stub(
        image_understanding_mode="native",
        vision_image_download_max_count=4,
    )
    handler, generate_kwargs, download_batches, build_prompt_kwargs = _wire_generate_core(
        monkeypatch,
        config,
        image_urls=["https://example.com/a.png", "https://example.com/b.png"],
    )
    del handler

    assert download_batches == [
        ["https://example.com/a.png", "https://example.com/b.png"]
    ]
    # build_prompt 收到 base64 图片与关闭的委托开关（由 prompt_builder 嵌入）
    assert build_prompt_kwargs.get("delegated_image_mode") is False
    assert build_prompt_kwargs.get("image_urls") == [
        "base64:https://example.com/a.png",
        "base64:https://example.com/b.png",
    ]
    assert generate_kwargs.get("base64_images") is None, (
        "native 模式不得把 base64 交给工具循环（无 read_image 消费方）"
    )
    tools = generate_kwargs.get("tools")
    tool_names: set[str] = set()
    if isinstance(tools, list):
        tool_names = {str(tool["function"]["name"]) for tool in tools}
    assert "read_image" not in tool_names, "native 模式不得暴露 read_image 工具"


def test_delegated_mode_exposes_read_image_tool_with_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delegated：暴露 read_image 并把下载后的 base64 交给视觉子调用。"""
    config = _chat_config_stub(image_understanding_mode="delegated")
    handler, generate_kwargs, download_batches, build_prompt_kwargs = _wire_generate_core(
        monkeypatch,
        config,
        image_urls=["https://example.com/a.png", "https://example.com/b.png"],
    )
    del handler

    assert download_batches == [
        ["https://example.com/a.png", "https://example.com/b.png"]
    ]
    assert build_prompt_kwargs.get("delegated_image_mode") is True
    assert generate_kwargs.get("base64_images") == [
        "base64:https://example.com/a.png",
        "base64:https://example.com/b.png",
    ]
    tools = generate_kwargs.get("tools")
    tool_names: set[str] = set()
    if isinstance(tools, list):
        tool_names = {str(tool["function"]["name"]) for tool in tools}
    assert "read_image" in tool_names, "delegated 模式必须暴露 read_image 工具"


def test_no_images_no_vision_tool_in_either_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无图片时两种模式都不暴露 read_image，且不触达下载器。"""
    for mode in ("native", "delegated"):
        config = _chat_config_stub(image_understanding_mode=mode)
        handler, generate_kwargs, download_batches, _build_kwargs = _wire_generate_core(
            monkeypatch,
            config,
            image_urls=None,
        )
        del handler
        assert download_batches == []
        tools = generate_kwargs.get("tools")
        tool_names: set[str] = set()
        if isinstance(tools, list):
            tool_names = {str(tool["function"]["name"]) for tool in tools}
        assert "read_image" not in tool_names, f"{mode} 无图片不应暴露 read_image"
        assert generate_kwargs.get("base64_images") is None
