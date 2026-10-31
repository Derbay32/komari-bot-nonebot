"""TSK-225 真实业务 seam 补齐红基线（第 2 部分）。

补齐 vision_completion / tool_fetch_page / image_download / reply_send
行为级红断（AC-2）+ AC3（阶段屏障）+ AC4（send 粒度）+ AC5（sibling 隔离）
+ AC6（恢复不复活）。驱动真实生产 seam；未接入准入故预期红。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

pytestmark = pytest.mark.group_admission_acceptance
GROUP_ID = 100

mh = importlib.import_module("komari_bot.plugins.komari_chat.handlers.message_handler")
idm = importlib.import_module(
    "komari_bot.plugins.komari_chat.services.image_downloader"
)
init = importlib.import_module("komari_bot.plugins.komari_chat")


def _pol(**o: object) -> Any:
    v = {
        "max_images": 4,
        "max_image_bytes": 8 << 20,
        "max_total_bytes": 20 << 20,
        "max_pixels": 40_000_000,
        "concurrency": 2,
        "connect_timeout_seconds": 5.0,
        "read_timeout_seconds": 30.0,
        "total_timeout_seconds": 45.0,
    }
    v.update(o)
    return idm.ImageDownloadPolicy(**v)  # type: ignore[arg-type]


class _DL:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download(self, url: str) -> str | None:
        self.calls.append(url)
        return "data:image/png;base64,QQ=="

    async def close(self) -> None:
        return None


async def test_fetch_page_restricted_blocks_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.group_admission.test_chat_effect_admission import (
        test_tool_dispatch_restricted_blocks_tool_body,
    )

    await test_tool_dispatch_restricted_blocks_tool_body(monkeypatch, "chat.fetch_page")


async def test_image_download_restricted_blocks_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_chat.services import native_image
    from komari_bot.plugins.komari_chat.services.admission_gate import (
        effect_business_admitted,
    )

    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    dl = _DL()
    monkeypatch.setattr(native_image, "ImageDownloadSession", lambda _policy: dl)
    images = native_image.NativeImageInput(
        quoted_sources=["https://example.com/a.png"], current_sources=[],
        policy=_pol(), admission_guard=lambda: effect_business_admitted(group_id=GROUP_ID),
        group_id=str(GROUP_ID), request_trace_id="test-image",
    )
    try:
        with pytest.raises(native_image.ImageChunkAdmissionError):
            await images.prepare()
    finally:
        await images.aclose()
    assert s.calls
    assert dl.calls == []


async def _perm_ok(_a: object, _b: object, _c: object) -> tuple[bool, str]:
    del _a, _b, _c
    return (True, "")


async def _not_banned(_a: object, _b: object, _c: object) -> bool:
    del _a, _b, _c
    return False


def _pending() -> SimpleNamespace:
    m = SimpleNamespace(group_id=str(GROUP_ID))
    return SimpleNamespace(
        message=m,
        reply="hi",
        reply_to_message_id="9",
        decision_id="d9",
        decision_payload={},
        reason="platform_summon",
    )


def _carrier() -> SimpleNamespace:
    """本次分发共享载体替身：本用例只验证下游发送准入栅栏。"""
    return SimpleNamespace(request=SimpleNamespace(platform_summoned=False))


async def test_reply_send_restricted_blocks_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")  # provider admitted, send restricted
    install_scripted_adjudicate(monkeypatch, s)
    mod = _fresh_chat_module(monkeypatch)
    monkeypatch.setattr(
        mod,
        "user_ban_plugin",
        SimpleNamespace(is_event_banned=_not_banned),
        raising=False,
    )
    monkeypatch.setattr(
        mod,
        "get_context_config",
        lambda: SimpleNamespace(
            face_reaction_enabled=False,
            face_reaction_id=None,
            error_notify_enabled=False,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        mod,
        "get_config",
        lambda: SimpleNamespace(
            chat_enabled=True,
            face_reaction_enabled=False,
            face_reaction_id=None,
        ),
        raising=False,
    )
    sent: list[str] = []
    reached = SimpleNamespace(process=False, fulfill=0, send_callback=0)
    carrier = _carrier()

    class _Sender:
        def __init__(self, bot: object) -> None:
            self.bot = bot

        async def __call__(self, _req: object) -> None:
            sent.append("sent")

    class _Handler:
        async def process_message(
            self, _bot: object, _event: object, **kw: object
        ) -> Any:
            del _bot, _event
            # 入口编排确实把本次分发的载体交给了消息处理器。
            assert kw.get("decision_dispatch") is carrier
            reached.process = True
            return _pending()

        async def report_reply_failure(self, **kw: object) -> None:
            del kw

        def _log_decision(self, _obj: object = None) -> None:
            return None

    class _Wf:
        async def fulfill(self, reply: object, **kw: object) -> bool:
            reached.fulfill += 1
            sr = cast("Any", kw.get("send_reply"))
            if sr is not None:
                reached.send_callback += 1
                await sr(reply)
            return True

    monkeypatch.setattr(mod, "_get_or_build_handler", lambda: _Handler(), raising=False)
    monkeypatch.setattr(
        mod, "_get_or_build_reply_fulfillment", lambda: _Wf(), raising=False
    )
    monkeypatch.setattr(mod, "OneBotReplySender", _Sender, raising=False)
    monkeypatch.setattr(
        mod, "get_decision_dispatch", lambda *_a, **_kw: carrier, raising=False
    )
    bot = SimpleNamespace(self_id="12345", type="OneBot V11")
    event = SimpleNamespace(group_id=GROUP_ID, message_id=9, user_id="7")
    await mod.handle_group_message(bot, event, {})
    # 不空转证明：入口处理与发送回调确实都走到了，才断言发送被受限拦截。
    assert reached.process is True, "入口未到达消息处理（用例空转）"
    assert reached.fulfill == 1, "履约未接过一次回复（用例空转）"
    assert reached.send_callback == 1, "发送回调未被执行（用例空转）"
    assert s.calls != [], "reply_send 前未裁决"
    assert sent == [], "受限下仍执行群回复 send/write"


async def test_phase_barrier_admitted_then_restricted_blocks_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.group_admission.test_chat_effect_admission import _run_dsh

    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")
    install_scripted_adjudicate(monkeypatch, s)
    result, host = await _run_dsh()
    assert len(s.calls) >= 2
    assert result.content == ""
    assert host.requests == []


async def test_reauth_does_not_resurrect_skipped_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_chat.services.admission_gate import (
        effect_business_admitted,
    )
    from komari_bot.plugins.komari_chat.services.chat_query_tools import (
        ChatQueryAdmissionError,
    )
    from tests.komari_chat.test_chat_query_tools import _callbacks, _request, _tools

    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted", "admitted")
    install_scripted_adjudicate(monkeypatch, s)
    tools, _memory, calls, _knowledge = _tools(admitted=lambda: effect_business_admitted(group_id=GROUP_ID))
    callback = _callbacks(tools)["chat.search_web"]
    await callback(_request("chat.search_web", {"query": "first"}))
    with pytest.raises(ChatQueryAdmissionError):
        await callback(_request("chat.search_web", {"query": "skipped"}))
    await callback(_request("chat.search_web", {"query": "last"}))
    assert calls == ["search:first:trace-1", "search:last:trace-1"]
    assert len(s.calls) == 3


async def test_concurrent_sibling_admitted_completes_restricted_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from komari_bot.plugins.komari_chat.services.admission_gate import (
        effect_business_admitted,
    )
    from komari_bot.plugins.komari_chat.services.chat_query_tools import (
        ChatQueryAdmissionError,
    )
    from tests.komari_chat.test_chat_query_tools import _callbacks, _request, _tools

    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")
    install_scripted_adjudicate(monkeypatch, s)
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def search(query: str, **_kwargs: object) -> str:
        calls.append(query)
        started.set()
        await release.wait()
        return "已完成"

    tools, _memory, _calls, _knowledge = _tools(
        admitted=lambda: effect_business_admitted(group_id=GROUP_ID), search=search,
    )
    callback = _callbacks(tools)["chat.search_web"]
    first = asyncio.create_task(callback(_request("chat.search_web", {"query": "first"})))
    try:
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(ChatQueryAdmissionError):
            await callback(_request("chat.search_web", {"query": "denied"}))
        release.set()
        assert (await first)["ok"] is True
    finally:
        release.set()
        await first
    assert calls == ["first"]
    assert len(s.calls) == 2


# ---------------------------------------------------------------------------
# 强制补充项：recall（chat.recall）真实行为断言
# ---------------------------------------------------------------------------


def _recall_handler(memory: Any) -> Any:
    handler = mh.MessageHandler.__new__(mh.MessageHandler)
    handler.hindsight_memory = memory
    handler.redis = SimpleNamespace()
    handler.query_rewrite = SimpleNamespace(
        rewrite_query=lambda current_query, **_kw: _return_value(current_query)
    )
    return handler


async def _return_value(value: Any) -> Any:
    return value


async def _run_recall_core(handler: Any) -> Any:
    message = mh.MessageSchema(
        user_id="u1",
        user_nickname="昵称",
        group_id=str(GROUP_ID),
        content="你好",
        timestamp=1.0,
        message_id="m1",
    )
    return await handler._generate_reply_core(
        message=message,
        recent_messages=[],
        image_urls=None,
        reply_context=None,
        reply_context_requested=False,
        reply_context_refetched=False,
        request_trace_id="t",
    )


class _BlockingContextRedis:
    """Recent-context double whose existing-buffer read pauses the turn."""

    def __init__(self) -> None:
        self.read_started = asyncio.Event()
        self.read_release = asyncio.Event()
        self.pushed: list[Any] = []
        self.buffer: list[Any] = []

    async def get_buffer(self, _group_id: str, *, limit: int = 10) -> list[Any]:
        del limit
        self.read_started.set()
        await self.read_release.wait()
        return list(self.buffer)

    async def push_message(self, _group_id: str, message: Any) -> None:
        self.pushed.append(message)
        self.buffer.append(message)


@pytest.mark.parametrize("state", ["restricted", "failed"])
async def test_context_push_rechecks_live_admission_after_buffer_await(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Denied current-context write returns not-stored and performs no push."""
    from komari_bot.plugins.group_admission import AdmissionIntent

    scripted = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, scripted)
    redis = _BlockingContextRedis()
    handler = mh.MessageHandler.__new__(mh.MessageHandler)
    handler.redis = redis
    monkeypatch.setattr(
        mh,
        "get_config",
        lambda: SimpleNamespace(
            context_messages_limit=10,
            context_max_utf8_bytes=24_000,
            context_max_estimated_tokens=6_000,
        ),
    )
    previous = mh.MessageSchema(
        user_id="u-old",
        user_nickname="旧消息",
        group_id=str(GROUP_ID),
        content="旧上下文",
        timestamp=1.0,
        message_id="old-1",
    )
    current = mh.MessageSchema(
        user_id="u-current",
        user_nickname="当前用户",
        group_id=str(GROUP_ID),
        content="当前消息",
        timestamp=2.0,
        message_id="current-1",
    )
    redis.buffer.append(previous)

    task = asyncio.create_task(
        handler._read_buffers(
            group_id=str(GROUP_ID),
            message=current,
            store_current=True,
        )
    )
    await asyncio.wait_for(redis.read_started.wait(), timeout=2)
    scripted.set_state(state)
    redis.read_release.set()
    recent, stored = await asyncio.wait_for(task, timeout=2)

    assert stored is False
    assert [message.message_id for message in recent] == ["old-1"]
    assert redis.pushed == []
    assert len(scripted.calls) == 1
    groups, intent = scripted.calls[0]
    assert groups == (GROUP_ID,)
    assert intent is AdmissionIntent.BUSINESS


class _FakeChatHost:
    """Trusted DSH host double: validates through the real Python candidate."""

    def __init__(self, requests: list[Any]) -> None:
        self.requests = requests

    async def execute(
        self,
        request: Any,
        callbacks: Mapping[str, Callable[[Any], Awaitable[Any]]],
    ) -> Any:
        self.requests.append(request)
        final = callbacks["chat.submit_final_response"]
        favor = callbacks["chat.submit_favorability"]
        receipt = await final(SimpleNamespace(data={"content": "ok"}))
        assert isinstance(receipt, dict)
        receipt = await favor(
            SimpleNamespace(data={"delta": 0, "reason": "无变化"})
        )
        assert isinstance(receipt, dict)
        return SimpleNamespace(
            result={
                "content": "ok",
                "favorability_delta": 0,
                "favorability_reason": "无变化",
            }
        )


def _patch_recall_core(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prompt_calls: list[dict[str, Any]] | None = None,
    generate_calls: list[Any] | None = None,
    knowledge_search: Any | None = None,
) -> None:
    del prompt_calls
    service_module = importlib.import_module(
        "komari_bot.plugins.komari_chat.services.dsh_chat_service"
    )
    conf = SimpleNamespace(
        memory_search_limit=3,
        knowledge_enabled=False,
        knowledge_limit=3,
        image_understanding_mode="native",
        agent_max_rounds=10,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=20,
        agent_tool_call_mode="required",
        bot_nickname="小鞠",
        face_reaction_enabled=False,
        face_reaction_id="76",
        error_notify_enabled=False,
        vision_image_download_max_count=4,
        vision_image_download_max_bytes=8 * 1024 * 1024,
        vision_image_download_total_max_bytes=20 * 1024 * 1024,
        vision_image_download_max_pixels=40_000_000,
        vision_image_download_concurrency=2,
        vision_image_download_connect_timeout_seconds=5.0,
        vision_image_download_read_timeout_seconds=30.0,
        vision_image_download_total_timeout_seconds=45.0,
    )
    host = _FakeChatHost(generate_calls if generate_calls is not None else [])

    async def _none_search(**_kwargs: object) -> list[object]:
        return []

    async def _none_uid(_uid: str) -> list[object]:
        return []

    async def _read_favorability(_user_id: str) -> object:
        return SimpleNamespace(favorability=0, stage_index=1, stage_name="熟悉", stage_prompt="关系逐渐熟悉。")

    service = service_module.DshChatService(
        host_getter=lambda: host,
        knowledge_search=knowledge_search or _none_search,
        uid_search=_none_uid,
        favorability_getter=_read_favorability,
        group_bindings_getter=lambda _group: {},
    )
    monkeypatch.setattr(mh, "get_config", lambda: conf)
    monkeypatch.setattr(mh, "get_context_config", lambda: conf)
    monkeypatch.setattr(mh, "DshChatService", lambda: service)
    monkeypatch.setattr(
        service_module,
        "_user_data_config",
        lambda: SimpleNamespace(max_favorability_delta_per_reply=5),
    )




async def _favorability() -> object:
    return SimpleNamespace(favorability=0)


async def test_recall_restricted_blocks_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    """chat.recall：会话打开前与自动召回外呼前各做一次当前群裁决。"""
    from tests.komari_chat.hindsight_recall_support import FakeHindsightMemory

    # Phase 1: restricted before session opening.
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    memory = FakeHindsightMemory()
    _patch_recall_core(monkeypatch)

    result = await _run_recall_core(_recall_handler(memory))

    assert scripted.calls != [], "chat.recall 会话打开前未调用 adjudicate"
    assert memory.open_calls == 0
    assert memory.recall_calls == []
    assert result.content == ""

    # Phase 2: admitted for the session, restricted immediately before recall.
    scripted = ScriptedAdjudicate("admitted")
    scripted.set_sequence("admitted", "restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    memory = FakeHindsightMemory()
    generate_calls: list[Any] = []
    _patch_recall_core(monkeypatch, generate_calls=generate_calls)

    result = await _run_recall_core(_recall_handler(memory))

    assert len(scripted.calls) >= 1, "自动召回外呼前未再次裁决"
    assert memory.open_calls == 1
    assert memory.recall_calls == []
    assert memory.sessions[0].closed is True
    assert generate_calls == []
    assert result.content == ""


@pytest.mark.parametrize("source_user_id", ["u1", "another-user"])
async def test_recalled_memory_from_restricted_source_usable_in_admitted_group(
    monkeypatch: pytest.MonkeyPatch,
    source_user_id: str,
) -> None:
    """已接收记忆不按来源群过滤；当前群获准即可用于另一群。"""
    from hindsight_client import RecallResponse

    from tests.komari_chat.hindsight_recall_support import FakeHindsightMemory

    scripted = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, scripted)
    response = RecallResponse.model_validate(
        {
            "results": [
                {
                    "id": "memory-from-group-a",
                    "text": "A 群的旅行经历",
                    "metadata": {
                        "source_group_id": "group-a",
                        "source_user_id": source_user_id,
                    },
                }
            ]
        }
    )
    memory = FakeHindsightMemory(response=response)
    generate_calls: list[Any] = []
    _patch_recall_core(monkeypatch, generate_calls=generate_calls)

    result = await _run_recall_core(_recall_handler(memory))

    assert result.content == "ok"
    assert len(memory.recall_calls) == 1
    assert "你好" in memory.recall_calls[0]
    assert len(generate_calls) == 1
    task_data = generate_calls[0].task_data
    assert task_data["memory"][0]["content"] == "A 群的旅行经历"
    assert all(call[0] == (GROUP_ID,) for call in scripted.calls)


# ---------------------------------------------------------------------------
# 强制补充项：debug_public（chat.debug_public）真实行为断言
# ---------------------------------------------------------------------------


async def test_debug_public_restricted_blocks_group_public_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.debug_public：debug 干跑的群公开输出前按群裁决，受限时丢弃。

    驱动「效果所在的最近生产函数」``generate_debug_reply``（komari_chat
    入口）：受限时静默返回空回复，不进入 LLM / 不产生可送达群内的公开输出。
    """
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    mod = _fresh_chat_module(monkeypatch)
    result = await mod.generate_debug_reply(
        group_id=str(GROUP_ID),
        user_id="5",
        user_nickname="n",
        content="x",
        request_trace_id="debug-1",
    )
    assert s.calls != [], "chat.debug_public 群公开输出前未调用 adjudicate"
    assert result.reply == "", "受限准入下 debug 群公开输出应被安静丢弃"
    assert result.request_id == "debug-1"


def _fresh_chat_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """按 test_plugin_entry 模式独立加载 chat 插件入口，避开 NoneBot 插件装载。"""
    import importlib.util
    import pathlib

    import nonebot.plugin as np

    original_require = np.require

    def _require(name: str) -> object:
        if name in {"komari_memory", "komari_decision", "user_ban", "user_data"}:
            return SimpleNamespace(get_plugin_manager=lambda: None)
        return original_require(name)

    monkeypatch.setattr(np, "require", _require)
    module_name = "komari_bot.plugins.komari_chat._entry_test"
    module_path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "komari_bot"
        / "plugins"
        / "komari_chat"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        msg = "无法加载聊天插件入口"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
