"""TSK-194/ADR-0010 图片理解模式（native/delegated）验收测试。

覆盖：
- ``ImageUnderstandingPolicy`` 冻结值对象：模式解析、非法/缺失模式明确
  失败、下载预算委托 ``ImageDownloadPolicy`` 且保留跨字段约束、不可变；
  ``ImageDownloadPolicy.from_config`` 对 8 项预算逐项严格读取，任一字段
  缺失或非法都明确失败，绝不回退 Python 默认值 / 旧 memory 别名
  （TSK-194/ADR-0010 协调式破坏升级）；
- ``_generate_reply_core`` 按模式切换：native 时图片作为多模态输入嵌入
  (user) 消息且不暴露 ``read_image`` 工具（不把图片交给工具循环），
  delegated 时暴露 ``read_image`` 且任务起点零预下载，只向工具循环
  传入任务级 ``image_session``（稳定索引计数，无 URL/base64，TSK-195）；
- 禁止自动降级的可观察失败：native 时 chat provider 对带图请求报错则本
  任务明确失败（不声明/调用 read_image、不切 delegated、不调视觉服务）；
- 真实任务冻结：同一 ``_generate_reply_core`` 任务中途修改 chat 配置的
  ``image_understanding_mode`` 与一项预算，当前任务仍用起点快照（delegated
  会话按起点预算截断），下一次任务才用新值；且每个任务只读取一次 chat
  config、agent budget 与 image policy 来自同一快照对象；
- 两种模式共用同一份下载预算与同一 chat 槽位主循环（主循环模型选择见
  ``test_request_mode_passthrough.py`` 的 chat 槽位断言）。

本文件沿用 ``test_agent_budget.py`` 的测试 seam：公开无副作用生成核心
（``_generate_reply_core``）配合可控 provider 与业务工具替身；只断言
可观察行为（build_prompt 输入 / 工具集合 / 图片会话 / 下载批次），不断言
私有 helper。
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import types
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

import komari_bot.plugins as plugins_package

image_understanding_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_understanding"
)
image_downloader_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_downloader"
)
image_reading_session_module = import_module(
    "komari_bot.plugins.komari_chat.services.image_reading_session"
)
llm_service_module = import_module(
    "komari_bot.plugins.komari_chat.services.llm_service"
)
message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)
agent_budget_module = import_module(
    "komari_bot.plugins.komari_chat.services.agent_budget"
)

if TYPE_CHECKING:
    from komari_bot.plugins.komari_chat.services.image_reading_session import (
        ImageReadingSession,
    )

ImageUnderstandingPolicy = image_understanding_module.ImageUnderstandingPolicy#: 与配置 Schema / 迁移 0015 默认值一致的图片下载预算 8 项字段。
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
    """TSK-194 视觉槽位全部参数（含推理参数）的替身。"""
    return SimpleNamespace(
        vision_model="vision-model",
        vision_temperature=0.3,
        vision_max_tokens=1024,
        vision_request_api="chat_completions",
        vision_stream_enabled=False,
        vision_thinking_mode=False,
        vision_reasoning_effort="",
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


def test_policy_from_config_native_mode_requires_all_budget_fields() -> None:
    """native 模式同样要求完整预算字段；缺预算字段也明确失败。"""
    config = SimpleNamespace(image_understanding_mode="native", **_CHAT_BUDGET_FIELDS)
    policy = ImageUnderstandingPolicy.from_config(config)
    assert policy.mode == "native"
    assert policy.is_delegated is False
    assert policy.download.max_images == 4
    assert policy.download.max_total_bytes == 20 * 1024 * 1024
    assert (
        policy.download.total_timeout_seconds == 45.0
    )


def test_policy_from_config_any_missing_budget_field_fails_fast() -> None:
    """删除 8 项预算中的任意一项，策略冻结都必须明确失败，不回退默认值。

    TSK-194/ADR-0010 协调式破坏升级：0015 之后 chat typed 配置必然携带
    全部 8 项预算字段；缺任一字段绝不静默采用 Python 默认值或读取
    ``komari_memory`` 别名/历史快照。
    """
    for missing_field in _CHAT_BUDGET_FIELDS:
        config = SimpleNamespace(image_understanding_mode="delegated")
        for name, value in _CHAT_BUDGET_FIELDS.items():
            if name != missing_field:
                setattr(config, name, value)
        with pytest.raises(RuntimeError, match=missing_field):
            ImageUnderstandingPolicy.from_config(config)
        # 即使模式为 native，预算字段缺失同样失败（不因 native 放宽）
        native_config = SimpleNamespace(image_understanding_mode="native")
        for name, value in _CHAT_BUDGET_FIELDS.items():
            if name != missing_field:
                setattr(native_config, name, value)
        with pytest.raises(RuntimeError, match=missing_field):
            ImageUnderstandingPolicy.from_config(native_config)


def test_download_policy_from_config_rejects_invalid_budget_types() -> None:
    """预算字段类型非法（含 bool）同样明确失败（TypeError）。"""
    config = SimpleNamespace(image_understanding_mode="delegated", **_CHAT_BUDGET_FIELDS)
    config.vision_image_download_max_count = True  # type: ignore[assignment]
    with pytest.raises(TypeError, match="vision_image_download_max_count"):
        ImageUnderstandingPolicy.from_config(config)

    float_config = SimpleNamespace(
        image_understanding_mode="delegated", **_CHAT_BUDGET_FIELDS
    )
    float_config.vision_image_download_total_timeout_seconds = "慢"  # type: ignore[assignment]
    with pytest.raises(
        TypeError, match="vision_image_download_total_timeout_seconds"
    ):
        ImageUnderstandingPolicy.from_config(float_config)


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
    config = SimpleNamespace(image_understanding_mode="delegated", **_CHAT_BUDGET_FIELDS)
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


def test_delegated_mode_zero_predownload_and_exposes_read_image_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-195：delegated 任务起点零预下载，仍暴露 read_image 并传入会话。

    断言代理主消息/prompt 不带任何 URL/base64（只有稳定索引计数），且
    ``generate_reply_with_tools`` 收到任务级 ``image_session`` 而非原始
    URL/base64 列表；download 层零触达。
    """
    config = _chat_config_stub(image_understanding_mode="delegated")
    handler, generate_kwargs, download_batches, build_prompt_kwargs = _wire_generate_core(
        monkeypatch,
        config,
        image_urls=["https://example.com/a.png", "https://example.com/b.png"],
    )
    del handler

    assert download_batches == [], "delegated 任务起点零预下载，不得触达下载器"
    assert build_prompt_kwargs.get("delegated_image_mode") is True
    assert build_prompt_kwargs.get("image_urls") is None
    assert build_prompt_kwargs.get("reply_image_urls") is None
    assert build_prompt_kwargs.get("delegated_quoted_image_count") == 0
    assert build_prompt_kwargs.get("delegated_current_image_count") == 2
    session = generate_kwargs.get("image_session")
    assert session is not None, "delegated 必须把任务级图片会话交给工具循环"
    session = cast("ImageReadingSession", session)
    assert session.total_count == 2
    assert session.current_count == 2
    assert generate_kwargs.get("base64_images") is None
    tools = generate_kwargs.get("tools")
    tool_names: set[str] = set()
    if isinstance(tools, list):
        tool_names = {str(tool["function"]["name"]) for tool in tools}
    assert "read_image" in tool_names, "delegated 模式必须暴露 read_image 工具"


def test_delegated_mode_quoted_images_first_stable_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-195：delegated 引用消息图片在前、当前消息图片在后，索引稳定。"""
    config = _chat_config_stub(image_understanding_mode="delegated")
    reply_context = message_handler_module.ReplyContext(
        source_side="user",
        message_id="quoted-msg-1",
        user_id="quoted-user",
        user_nickname="被回复者",
        text="被回复文本",
        image_sources=(
            "https://example.com/quoted-0.png",
            "https://example.com/quoted-1.png",
        ),
        image_count=2,
        has_visible_image=True,
    )
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
        return [{"role": "user", "content": "test"}]

    generate_kwargs: dict[str, object] = {}

    async def _fake_generate_with_tools(**kwargs: object) -> Any:
        generate_kwargs.update(kwargs)
        return llm_service_module.ReplyResult(
            content="回复内容",
            interaction_history={"event": "看图", "result": "结果", "emotion": "好奇"},
            favorability_delta=0,
            favorability_reason="无变化",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(
        message_handler_module, "generate_reply_with_tools", _fake_generate_with_tools
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
            image_urls=["https://example.com/current-0.png"],
            reply_context=reply_context,
            reply_context_requested=True,
            reply_context_refetched=False,
            request_trace_id="delegated-quoted-1",
        )

    asyncio.run(_run())

    assert build_prompt_kwargs.get("delegated_quoted_image_count") == 2
    assert build_prompt_kwargs.get("delegated_current_image_count") == 1
    session = generate_kwargs.get("image_session")
    assert session is not None
    session = cast("ImageReadingSession", session)
    assert session.total_count == 3
    assert session.quoted_count == 2
    assert session.current_count == 1
    assert [(ref.origin, ref.original_index) for ref in session.references] == [
        ("quoted", 0),
        ("quoted", 1),
        ("current", 0),
    ]


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
        assert generate_kwargs.get("image_session") is None, (
            f"{mode} 无图片不应创建图片会话"
        )


# ── 禁止自动降级的可观察失败（TSK-194） ────────────────────────────────


def test_native_mode_chat_provider_image_failure_fails_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native：chat provider 对带图请求报错/拒图时，本任务明确失败。

    驱动公开生成 seam（真实 ``_generate_reply_core``）：图片作为多模态输入
    交给聊天槽位后，模拟 chat provider 拒绝该请求并抛错；断言错误向上传
    播（不静默降级），且不声明/调用 read_image、不切 delegated、不触摸
    视觉服务（read_images 保持未调用）。
    """
    config = _chat_config_stub(image_understanding_mode="native")
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
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )

    build_prompt_kwargs: dict[str, object] = {}

    async def _fake_build_prompt_multimodal(
        **kwargs: object,
    ) -> list[dict[str, object]]:
        build_prompt_kwargs.update(kwargs)
        # native 模式：图片作为多模态输入直接嵌入 (user) 消息
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

    generate_kwargs: dict[str, object] = {}

    async def _refusing_generate_with_tools(**kwargs: object) -> Any:
        generate_kwargs.update(kwargs)
        msg = "chat provider 拒绝多模态图片请求"
        raise RuntimeError(msg)

    read_images_called: list[object] = []

    async def _recording_read_images(*args: Any, **_kwargs: object) -> list[str]:
        read_images_called.append(args)
        return []

    async def _download_images(
        urls: list[str],
        _policy: object,
    ) -> list[str | None]:
        return [f"base64:{url}" for url in urls]

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt_multimodal)
    monkeypatch.setattr(
        message_handler_module,
        "generate_reply_with_tools",
        _refusing_generate_with_tools,
    )
    monkeypatch.setattr(
        message_handler_module,
        "download_images_as_base64_aligned",
        _download_images,
    )
    monkeypatch.setattr(
        image_reading_session_module, "read_images", _recording_read_images
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
            image_urls=["https://example.com/a.png"],
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            request_trace_id="chat-native-fail-1",
        )

    with pytest.raises(RuntimeError, match="拒绝多模态图片请求"):
        asyncio.run(_run())

    tools = generate_kwargs.get("tools")
    tool_names: set[str] = set()
    if isinstance(tools, list):
        tool_names = {str(tool["function"]["name"]) for tool in tools}
    assert "read_image" not in tool_names, (
        "native 模式不得声明 read_image（chat provider 失败也不得切 delegated）"
    )
    assert generate_kwargs.get("image_session") is None, (
        "native 模式不得为工具循环创建图片会话（图片只走多模态输入）"
    )
    assert build_prompt_kwargs.get("delegated_image_mode") is False
    assert read_images_called == [], "native 模式禁止调用视觉服务（read_image 子调用）"


# ── 真实任务冻结（TSK-194） ────────────────────────────────────────────


def test_generate_core_freezes_image_policy_snapshot_across_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat 配置在任务起点冻结，中途修改只影响下一个任务（真实任务流）。

    同一 ``_generate_reply_core`` 任务执行期间修改 ``image_understanding_mode``
    与一项预算（``vision_image_download_max_count``），当前任务仍使用起点
    快照（delegated + max_count=2），下一次任务才读取新值（native +
    max_count=7）。同时断言每个任务只获取一次 chat config，且 agent budget
    与 image policy 来自同一个快照对象（不只是 frozen dataclass 自证）。
    """
    config = _chat_config_stub(
        image_understanding_mode="delegated",
        vision_image_download_max_count=2,
    )
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = _FakeRedis()
    handler.memory = _FakeMemory()
    handler.query_rewrite = _FakeQueryRewrite()

    get_config_calls: list[object] = []
    budget_args: list[object] = []
    policy_args: list[object] = []
    download_batches: list[list[str]] = []
    download_policies: list[object] = []
    generate_calls: list[dict[str, object]] = []

    def _get_config_spy() -> object:
        get_config_calls.append(config)
        return config

    original_budget_from_config = (
        agent_budget_module.AgentExecutionBudget.from_config
    )
    original_policy_from_config = (
        image_downloader_module.ImageDownloadPolicy.from_config
    )

    def _budget_spy(_cls: type, cfg: object) -> object:
        budget_args.append(cfg)
        return original_budget_from_config(cfg)  # type: ignore[no-any-return]

    def _policy_spy(_cls: type, cfg: object) -> object:
        policy_args.append(cfg)
        return original_policy_from_config(cfg)  # type: ignore[no-any-return]

    monkeypatch.setattr(message_handler_module, "get_config", _get_config_spy)
    monkeypatch.setattr(message_handler_module, "get_memory_config", lambda: config)
    monkeypatch.setattr(
        agent_budget_module.AgentExecutionBudget,
        "from_config",
        classmethod(_budget_spy),
    )
    monkeypatch.setattr(
        image_downloader_module.ImageDownloadPolicy,
        "from_config",
        classmethod(_policy_spy),
    )
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserData())
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: False,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )
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
                vision_thinking_mode=False,
                vision_reasoning_effort="",
            )
        ),
    )

    build_prompt_calls = {"count": 0}

    async def _download_images(
        urls: list[str],
        policy: object,
    ) -> list[str | None]:
        max_images = policy.max_images  # type: ignore[attr-defined]
        download_batches.append(list(urls[:max_images]))
        download_policies.append(policy)
        selected = [f"base64:{url}" for url in urls[:max_images]]
        return [*selected, *([None] * (len(urls) - max_images))]

    async def _fake_build_prompt(**kwargs: object) -> list[dict[str, object]]:
        del kwargs
        build_prompt_calls["count"] += 1
        if build_prompt_calls["count"] == 1:
            # 任务中途修改 chat 配置：当前任务已冻结（TSK-195 会话容量按
            # 起点预算 max_count=2 截断），只影响下一个任务
            config.image_understanding_mode = "native"
            config.vision_image_download_max_count = 7
        return [{"role": "user", "content": "test"}]

    async def _fake_generate_with_tools(**kwargs: object) -> Any:
        generate_calls.append(kwargs)
        return llm_service_module.ReplyResult(
            content="回复内容",
            interaction_history={
                "event": "看图",
                "result": "描述",
                "emotion": "好奇",
            },
            favorability_delta=0,
            favorability_reason="无变化",
        )

    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(
        message_handler_module,
        "generate_reply_with_tools",
        _fake_generate_with_tools,
    )
    monkeypatch.setattr(
        message_handler_module,
        "download_images_as_base64_aligned",
        _download_images,
    )

    # 4 张图：起点预算 max_count=2 只选前 2 张；新预算 7 全部下载
    urls = [f"https://example.com/{index}.png" for index in range(4)]

    async def _run_first() -> Any:
        return await handler._generate_reply_core(
            message=_make_message("frozen-1"),
            recent_messages=[],
            interaction_records=[],
            image_urls=urls,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            request_trace_id="frozen-task-1",
        )

    asyncio.run(_run_first())

    # 当前任务使用起点快照（delegated + max_count=2 ⇒ 会话上限 2 张）
    assert get_config_calls == [config], "每个任务最多读取一次 chat config"
    assert budget_args == [config] and policy_args == [config]
    assert budget_args[0] is policy_args[0], (
        "agent budget 与 image policy 来自同一快照对象"
    )
    assert download_batches == [], "delegated 任务起点零预下载，不触达下载器"
    first_tools = generate_calls[0]["tools"]
    assert isinstance(first_tools, list)
    first_names = {str(tool["function"]["name"]) for tool in first_tools}
    assert "read_image" in first_names, "当前任务使用起点 delegated 模式"
    first_session = generate_calls[0].get("image_session")
    assert first_session is not None
    first_session = cast("ImageReadingSession", first_session)
    assert first_session.total_count == 2, "起点预算 max_count=2 截断会话容量"
    assert first_session.current_count == 2
    assert generate_calls[0].get("base64_images") is None

    async def _run_second() -> Any:
        return await handler._generate_reply_core(
            message=_make_message("frozen-2"),
            recent_messages=[],
            interaction_records=[],
            image_urls=urls,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            request_trace_id="frozen-task-2",
        )

    asyncio.run(_run_second())

    # 下一次任务使用新值（native + max_count=7）
    assert len(get_config_calls) == 2, "每个任务只读取一次 chat config"
    assert download_batches[0] == urls, "下一任务使用新的 max_count=7"
    assert download_policies[0].max_images == 7  # type: ignore[attr-defined]
    second_tools = generate_calls[1]["tools"]
    assert isinstance(second_tools, list)
    second_names = {str(tool["function"]["name"]) for tool in second_tools}
    assert "read_image" not in second_names, "下一任务使用 native 模式"
    assert generate_calls[1]["image_session"] is None
