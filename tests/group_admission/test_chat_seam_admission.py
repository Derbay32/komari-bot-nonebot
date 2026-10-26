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
from typing import Any, cast

import pytest

from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

pytestmark = pytest.mark.group_admission_acceptance
GROUP_ID = 100

mh = importlib.import_module("komari_bot.plugins.komari_chat.handlers.message_handler")
llm = importlib.import_module("komari_bot.plugins.komari_chat.services.llm_service")
vs = importlib.import_module("komari_bot.plugins.komari_chat.services.vision_service")
irs = importlib.import_module(
    "komari_bot.plugins.komari_chat.services.image_reading_session"
)
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


def _sess(
    monkeypatch: pytest.MonkeyPatch,
    dl: Any,
    quoted: list[str] | None = None,
    current: list[str] | None = None,
) -> Any:
    monkeypatch.setattr(irs, "ImageDownloadSession", lambda _p: dl)
    vis = SimpleNamespace(__call__=lambda _images, **_kw: ["d"])
    monkeypatch.setattr(irs, "read_images", vis)
    return irs.ImageReadingSession.build(
        quoted_sources=quoted or [],
        current_sources=current or [],
        policy=_pol(),
        vision_model="v",
        vision_temperature=0.3,
        vision_max_tokens=1024,
        request_trace_id="t",
    )


async def test_vision_completion_restricted_blocks_vision_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    monkeypatch.setattr(
        vs,
        "llm_provider_config_manager",
        SimpleNamespace(get=lambda: SimpleNamespace(api_token="x")),
    )
    calls: list[dict[str, Any]] = []

    class _P:
        async def generate_messages_completion(self, **kw: Any) -> Any:
            calls.append(kw)
            return SimpleNamespace(content="视觉")

    monkeypatch.setattr(vs, "llm_provider", _P())
    await vs._read_single_image(
        image_data_uri="data:image/png;base64,QQ==",
        image_index=0,
        vision_model="v",
        vision_description_prompt="p",
        temperature=0.3,
        max_tokens=1024,
    )
    assert s.calls != [], "vision 前未裁决"
    assert calls == [], "受限下仍调用视觉 LLM"


async def test_fetch_page_restricted_blocks_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    fc: list[dict[str, Any]] = []

    async def f(urls: list[str], **kw: object) -> str:
        fc.append({"urls": urls, **kw})
        return "[正文]"

    monkeypatch.setattr(llm, "komari_search", SimpleNamespace(fetch_page=f))
    tc = SimpleNamespace(
        id="c",
        type="function",
        function=SimpleNamespace(name="fetch_page", arguments="{}"),
        raw_arguments="{}",
        parsed_arguments={"urls": ["https://x/a"]},
    )
    await llm._execute_business_tool(
        tool_call=tc,
        image_session=None,
        phase_prefix="p",
        round_num=1,
        recall_session=None,
        group_id=str(GROUP_ID),
        caller_group_id=None,
        caller_is_superuser=False,
        request_trace_id="t",
    )
    assert s.calls != [], "fetch_page 前未裁决"
    assert fc == [], "受限下仍发起 fetch_page"


async def test_image_download_restricted_blocks_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, s)
    dl = _DL()
    session = _sess(monkeypatch, dl, quoted=["https://example.com/a.png"])
    await session.read(0)
    assert s.calls != [], "下载前未裁决"
    assert dl.calls == [], "受限下仍触发图片下载"


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
            plugin_enable=True,
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
            plugin_enable=True,
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
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")  # 入口获准，效果启动前翻转受限
    install_scripted_adjudicate(monkeypatch, s)
    monkeypatch.setattr(
        vs,
        "llm_provider_config_manager",
        SimpleNamespace(get=lambda: SimpleNamespace(api_token="x")),
    )
    started = asyncio.Event()

    async def gated(**kw: object) -> Any:
        del kw
        # 效果真正启动的屏障信号：受限（已翻转）时不得置位。
        started.set()
        return SimpleNamespace(content="d")

    monkeypatch.setattr(
        vs, "llm_provider", SimpleNamespace(generate_messages_completion=gated)
    )
    await vs._read_single_image(
        image_data_uri="data:image/png;base64,QQ==",
        image_index=0,
        vision_model="v",
        vision_description_prompt="p",
        temperature=0.3,
        max_tokens=1024,
    )
    assert len(s.calls) >= 2, "入口与效果两个时点都应裁决"
    assert started.is_set() is False, "翻转受限后效果不应被启动"


async def test_reauth_does_not_resurrect_skipped_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted", "admitted")
    install_scripted_adjudicate(monkeypatch, s)
    dl = _DL()
    session = _sess(
        monkeypatch,
        dl,
        quoted=["https://x/0.png", "https://x/1.png"],
        current=["https://x/2.png"],
    )
    await session.read(0)
    await session.read(1)
    await session.read(2)
    assert dl.calls == ["https://x/0.png", "https://x/2.png"], (
        "受限时该 sibling 效果被跳过，恢复准入后不得补执行/复活"
    )


async def test_concurrent_sibling_admitted_completes_restricted_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = ScriptedAdjudicate("admitted")
    s.set_sequence("admitted", "restricted")
    install_scripted_adjudicate(monkeypatch, s)
    dl = _DL()
    session = _sess(
        monkeypatch,
        dl,
        quoted=["https://x/0.png", "https://x/1.png"],
        current=["https://x/2.png"],
    )
    await asyncio.gather(session.read(0), session.read(1))
    assert len(s.calls) >= 2, "两个 sibling 各自在 dispatch 前裁决"
    assert len(dl.calls) == 1, (
        "并发 sibling：获准者完成下载，受限者不启动；已 dispatch 结果不外溢"
    )


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


def _patch_recall_core(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prompt_calls: list[dict[str, Any]] | None = None,
    generate_calls: list[dict[str, Any]] | None = None,
) -> None:
    conf = SimpleNamespace(
        memory_search_limit=3,
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
    monkeypatch.setattr(mh, "get_config", lambda: conf)
    monkeypatch.setattr(mh, "get_context_config", lambda: conf)
    monkeypatch.setattr(
        mh,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kw: False,
            is_fetch_available=lambda **_kw: False,
        ),
    )
    monkeypatch.setattr(
        mh,
        "user_data_plugin",
        SimpleNamespace(
            get_user_favorability=lambda _user_id: _favorability(),
            get_config=lambda: SimpleNamespace(max_favorability_delta_per_reply=5),
        ),
    )

    async def _prompt(**kwargs: object) -> list[object]:
        if prompt_calls is not None:
            prompt_calls.append(kwargs)
        return [{"role": "user", "content": "test"}]

    async def _generate(**kwargs: object) -> object:
        if generate_calls is not None:
            generate_calls.append(kwargs)
        return llm.ReplyResult(
            content="ok",
            favorability_delta=0,
            favorability_reason="无变化",
        )

    monkeypatch.setattr(mh, "build_prompt", _prompt)
    monkeypatch.setattr(mh, "generate_reply_with_tools", _generate)


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
    prompt_calls: list[dict[str, Any]] = []
    generate_calls: list[dict[str, Any]] = []
    _patch_recall_core(
        monkeypatch,
        prompt_calls=prompt_calls,
        generate_calls=generate_calls,
    )

    result = await _run_recall_core(_recall_handler(memory))

    assert len(scripted.calls) >= 2, "自动召回外呼前未再次裁决"
    assert memory.open_calls == 1
    assert memory.recall_calls == []
    assert memory.sessions[0].closed is True
    assert prompt_calls == []
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
    prompt_calls: list[dict[str, Any]] = []
    generate_calls: list[dict[str, Any]] = []
    _patch_recall_core(
        monkeypatch,
        prompt_calls=prompt_calls,
        generate_calls=generate_calls,
    )

    result = await _run_recall_core(_recall_handler(memory))

    assert result.content == "ok"
    assert memory.recall_calls == ["你好"]
    assert prompt_calls[0]["recall_response"] is response
    assert generate_calls
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
