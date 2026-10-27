"""TSK-225 聊天即时效果准入逐效果红基线（AC-2..AC-7）。

每个用例把 ``group_admission`` 顶层 ``adjudicate`` 换为三态脚本，驱动真实
生产 seam（komari_chat 编排模块及其直接子效果），按准入状态断言下游是否执行：

- ``restricted`` / ``failed``：下游必须 fail-if-called（不执行）；
- ``admitted``：下游恰好执行一次；
- 效果前必须已经调用 ``adjudicate``（脚本 ``calls`` 非空）且归属/意图正确，
  且一次结果不得跨效果缓存（未接线前 ``calls`` 为空即接入缺失的红证据）。

当前生产 komari_chat 尚未在效果前裁决，restricted/failed 用例预期红；
admitted 用例锁定「获准后下游恰好执行」的稳定合约（绿）。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.group_admission.chat_admission_support import (
    ScriptedAdjudicate,
    install_scripted_adjudicate,
)

pytestmark = pytest.mark.group_admission_acceptance

message_handler_module = importlib.import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)

GROUP_ID = 100


def _patch_chat_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    face_reaction: bool,
) -> None:
    """patch message_handler 的 get_config / get_context_config 为本地替身。"""
    stub = SimpleNamespace(
        bot_nickname="小鞠",
        bot_aliases=["小鞠", "小鞠知花", "komari"],
        face_reaction_enabled=face_reaction,
        face_reaction_id="76",
        error_notify_enabled=False,
    )
    monkeypatch.setattr(message_handler_module, "get_config", lambda: stub)
    monkeypatch.setattr(message_handler_module, "get_context_config", lambda: stub)


async def _run_dsh() -> tuple[Any, Any]:
    from tests.komari_chat.test_dsh_chat_service import (
        _FakeHost,
        _generate,
        _invoke,
        _service,
    )

    async def finalize(callbacks: dict[str, Any]) -> None:
        await _invoke(callbacks, "chat.submit_final_response", {"content": "回复内容"})
        await _invoke(callbacks, "chat.submit_favorability", {"delta": 0, "reason": "无变化"})

    host = _FakeHost({"content": "回复内容", "favorability_delta": 0,
                      "favorability_reason": "无变化"}, finalize)
    service, _memory, session = _service(host)
    result = await _generate(service, session)
    return result, host


async def test_llm_round_admitted_executes_provider_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """获准后由 DSH 执行一次任务，不再调用自有模型网关。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, scripted)
    result, host = await _run_dsh()
    assert scripted.calls
    assert result.content == "回复内容"
    assert len(host.requests) == 1


async def test_llm_round_restricted_blocks_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    result, host = await _run_dsh()
    assert scripted.calls
    assert result.content == ""
    assert host.requests == []


async def test_llm_round_failed_discards_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = ScriptedAdjudicate("failed")
    install_scripted_adjudicate(monkeypatch, scripted)
    result, host = await _run_dsh()
    assert scripted.calls
    assert result.content == ""
    assert host.requests == []


@pytest.mark.parametrize("name", ["chat.recall_memory", "chat.search_knowledge", "chat.search_web", "chat.fetch_page"])
async def test_tool_dispatch_restricted_blocks_tool_body(
    monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    from komari_bot.plugins.komari_chat.services.admission_gate import (
        effect_business_admitted,
    )
    from komari_bot.plugins.komari_chat.services.chat_query_tools import (
        ChatQueryAdmissionError,
    )
    from tests.komari_chat.test_chat_query_tools import _callbacks, _request, _tools

    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    tools, memory, calls, knowledge_calls = _tools(
        admitted=lambda: effect_business_admitted(group_id=GROUP_ID),
    )
    data = {"urls": ["https://example.com"]} if name == "chat.fetch_page" else {"query": "test"}
    with pytest.raises(ChatQueryAdmissionError):
        await _callbacks(tools)[name](_request(name, data))
    assert scripted.calls
    assert calls == []
    assert memory.recall_calls == []
    assert knowledge_calls == []


async def test_tool_search_restricted_blocks_search_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await test_tool_dispatch_restricted_blocks_tool_body(monkeypatch, "chat.search_web")


# ---------------------------------------------------------------------------
# 平台读取（chat.group_read）
# ---------------------------------------------------------------------------


async def test_group_read_restricted_blocks_get_msg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.group_read：get_msg 群读取前按该群裁决，受限时不得发生。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)

    get_msg_called: list[int] = []

    class _FakeBot:
        self_id = "12345"

        async def get_msg(self, *, message_id: int) -> dict[str, object]:
            get_msg_called.append(message_id)
            raise RuntimeError("模拟：受限接入下不应发生 get_msg")  # noqa: TRY003

    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    reply = SimpleNamespace(message_id="12")

    await handler._refetch_reply(bot=cast("Any", _FakeBot()), reply=reply)

    assert scripted.calls != [], "chat.group_read 读取前未调用 adjudicate"
    assert get_msg_called == [], "chat.group_read 在受限下仍发生了 get_msg 平台读取"


# ---------------------------------------------------------------------------
# 表情反应（chat.reaction）
# ---------------------------------------------------------------------------


async def test_reaction_restricted_blocks_fire_and_forget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.reaction：受限时表情 fire-and-forget 不派发下游（红）。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    _patch_chat_config(monkeypatch, face_reaction=True)

    fired: list[bool] = []

    async def callback() -> None:
        fired.append(True)

    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler._reaction_tasks = set()

    scheduled = handler._schedule_reply_reaction(callback)
    await asyncio.sleep(0)  # 允许任何已派发的 fire-and-forget 任务运行一次

    assert scripted.calls != [], "chat.reaction dispatch 前未调用 adjudicate"
    assert scheduled is False, "chat.reaction 在受限准入下仍派发“生成中”表情"
    assert fired == [], "chat.reaction 在受限准入下回调仍启动"


# ---------------------------------------------------------------------------
# 固定失败文本 / 失败通知（chat.fixed_failure_text）
# ---------------------------------------------------------------------------


async def test_fixed_failure_text_restricted_not_notified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.fixed_failure_text：受限时不得发送群内固定错误文本/通知。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    _patch_chat_config(monkeypatch, face_reaction=False)

    notify_calls: list[dict[str, Any]] = []

    class _FakeNotifier:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def notify(self, **kwargs: Any) -> None:
            notify_calls.append(kwargs)

    monkeypatch.setattr(
        message_handler_module, "GroupTaskFailureNotifier", _FakeNotifier
    )
    monkeypatch.setattr(
        message_handler_module,
        "RedisFailureNotificationCooldown",
        lambda _redis: SimpleNamespace(),
    )

    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = SimpleNamespace(redis=SimpleNamespace())

    event = SimpleNamespace(group_id=GROUP_ID, message_id=7)
    failure = SimpleNamespace(
        stage="generate",
        error_type="SomeError",
        summary="x",
        request_trace_id="chat-1",
        reaction_sent=True,
        image_failure_summary=None,
    )

    await handler.report_reply_failure(
        bot=cast("Any", SimpleNamespace()),
        event=cast("Any", event),
        failure=cast("Any", failure),
        reason="at",
    )

    assert scripted.calls != [], "chat.fixed_failure_text 拒绝前未调用 adjudicate"
    assert notify_calls == [], (
        "chat.fixed_failure_text 在受限准入下仍触发群内固定错误文本/通知"
    )


# ---------------------------------------------------------------------------
# 记忆候选写入（chat.memory_candidate）
# ---------------------------------------------------------------------------


class _GuardOnlyRetainSession:
    """只暴露 chat 编排传入的同步 BUSINESS guard，不触达 SDK。"""

    target_fingerprint = "a" * 64

    def __init__(self) -> None:
        self.guard_results: list[bool] = []
        self.source_ids: list[str] = []

    async def retain(self, request: Any, *, submission_guard: Any) -> None:
        self.source_ids.append(request.source_id)
        self.guard_results.append(submission_guard())


async def test_memory_candidate_restricted_blocks_retain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.memory_candidate：受限时 retain 前只得到 False BUSINESS guard。"""
    from tests.komari_decision.jev_test_support import make_request

    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    session = _GuardOnlyRetainSession()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )

    await handler._retain_candidate(make_request(), cast("Any", session))

    assert scripted.calls != [], "chat.memory_candidate 前未调用 adjudicate"
    assert session.guard_results == [False], (
        "chat.memory_candidate 在受限准入下仍提交了 retain"
    )
    assert session.source_ids and session.source_ids[0].startswith("candidate:")
