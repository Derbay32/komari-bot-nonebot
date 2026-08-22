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
llm_service_module = importlib.import_module(
    "komari_bot.plugins.komari_chat.services.llm_service"
)

GROUP_ID = 100


def _patch_chat_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    face_reaction: bool,
) -> None:
    """patch message_handler 的 get_config / get_memory_config 为本地替身。"""
    stub = SimpleNamespace(
        bot_nickname="小鞠",
        bot_aliases=["小鞠", "小鞠知花", "komari"],
        face_reaction_enabled=face_reaction,
        face_reaction_id="76",
        error_notify_enabled=False,
    )
    monkeypatch.setattr(message_handler_module, "get_config", lambda: stub)
    monkeypatch.setattr(message_handler_module, "get_memory_config", lambda: stub)


def _llm_config() -> SimpleNamespace:
    """任务起点冻结的 chat 配置替身（覆盖预算与槽位字段）。"""
    return SimpleNamespace(
        llm_model_chat="chat-model",
        llm_temperature_chat=0.7,
        llm_max_tokens_chat=1024,
        llm_thinking_mode_chat=False,
        llm_reasoning_effort_chat="",
        bot_nickname="小鞠",
        agent_max_rounds=10,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=20,
        agent_tool_call_mode="required",
        llm_request_api_chat="chat_completions",
        llm_stream_enabled_chat=False,
    )


def _final_response_completion() -> Any:
    """与 LLMCompletionResultSchema 兼容的假 completion（final_response 终结）。"""
    return SimpleNamespace(
        content="",
        tool_calls=[
            SimpleNamespace(
                id="call-final",
                type="function",
                function=SimpleNamespace(
                    name="final_response", arguments="{}"
                ),
                raw_arguments="{}",
                parsed_arguments={
                    "content": "回复内容",
                    "interaction_history": {
                        "event": "打招呼",
                        "result": "陪话",
                        "emotion": "开心",
                    },
                },
            )
        ],
        finish_reason="stop",
        duration_ms=10.0,
        usage=None,
    )


class _RecorderProvider:
    """记录 provider 调用；completions 队列由调用方灌入。"""

    def __init__(self) -> None:
        self.completion_calls: list[dict[str, Any]] = []
        self.completions: list[Any] = []

    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        self.completion_calls.append(kwargs)
        if not self.completions:
            raise AssertionError("provider 无脚本化 completion 队列")  # noqa: TRY003
        return self.completions.pop(0)


# ---------------------------------------------------------------------------
# 每轮聊天 LLM / provider（chat.llm_round）
# ---------------------------------------------------------------------------


async def test_llm_round_admitted_executes_provider_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.llm_round：获准后每轮 provider 恰好执行一次（绿）。"""
    scripted = ScriptedAdjudicate("admitted")
    install_scripted_adjudicate(monkeypatch, scripted)
    provider = _RecorderProvider()
    provider.completions = [_final_response_completion()]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    result = await llm_service_module.generate_reply(
        config=_llm_config(),
        messages=[{"role": "user", "content": "你好"}],
    )

    assert scripted.calls != [], "chat.llm_round 效果前未调用 adjudicate"
    assert result.content == "回复内容"
    assert len(provider.completion_calls) == 1


async def test_llm_round_restricted_blocks_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.llm_round：受限时 provider 不得被调用（红：当前未接裁决）。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)
    provider = _RecorderProvider()
    provider.completions = [_final_response_completion()]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    await llm_service_module.generate_reply(
        config=_llm_config(),
        messages=[{"role": "user", "content": "你好"}],
    )

    assert scripted.calls != [], "chat.llm_round 效果前未调用 adjudicate"
    assert provider.completion_calls == [], (
        "chat.llm_round 在受限准入下仍调用了 provider"
    )


async def test_llm_round_failed_discards_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.llm_round：failed（无有效策略）同样阻止 provider 调用（红）。"""
    scripted = ScriptedAdjudicate("failed")
    install_scripted_adjudicate(monkeypatch, scripted)
    provider = _RecorderProvider()
    provider.completions = [_final_response_completion()]
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)

    await llm_service_module.generate_reply(
        config=_llm_config(),
        messages=[{"role": "user", "content": "hi"}],
    )

    assert scripted.calls != [], "chat.llm_round failed 时未调用 adjudicate"
    assert provider.completion_calls == [], (
        "chat.llm_round 在 failed 准入下仍调用了 provider"
    )


# ---------------------------------------------------------------------------
# 工具 dispatch（chat.tool_dispatch / chat.tool_search）
# ---------------------------------------------------------------------------


def _read_image_tool_call() -> SimpleNamespace:
    return SimpleNamespace(
        id="call-read",
        type="function",
        function=SimpleNamespace(name="read_image", arguments="{}"),
        raw_arguments="{}",
        parsed_arguments={"image_index": 0},
    )


async def test_tool_dispatch_restricted_blocks_tool_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.tool_dispatch：工具 dispatch 前必须裁决，受限时工具体不执行。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)

    read_calls: list[int] = []

    async def fake_read(index: int, **kwargs: Any) -> Any:
        del kwargs
        read_calls.append(index)
        return SimpleNamespace(
            status="success", description="图片描述", failure_message=None
        )

    fake_image_session = SimpleNamespace(
        total_count=1,
        read=fake_read,
        all_images_unavailable=lambda: False,
    )

    await llm_service_module._execute_business_tool(
        tool_call=_read_image_tool_call(),
        image_session=fake_image_session,
        phase_prefix="test",
        round_num=1,
        memory_service=None,
        group_id=str(GROUP_ID),
        allowed_profile_user_ids=frozenset(),
        caller_user_id=None,
        caller_group_id=None,
        caller_is_superuser=False,
        request_trace_id="chat-1",
        parent_call_id=None,
    )

    assert scripted.calls != [], "chat.tool_dispatch 工具前未调用 adjudicate"
    assert read_calls == [], (
        "chat.tool_dispatch 在受限准入下仍执行了工具体（read_image）"
    )


async def test_tool_search_restricted_blocks_search_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat.tool_search：search 工具进入前裁决；受限时不得发起外呼。"""
    scripted = ScriptedAdjudicate("restricted")
    install_scripted_adjudicate(monkeypatch, scripted)

    search_calls: list[dict[str, Any]] = []

    async def fake_search(query: str, **kwargs: Any) -> str:
        search_calls.append({"query": query, **kwargs})
        return "[搜索结果]"

    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=fake_search),
    )

    tool_call = SimpleNamespace(
        id="call-search",
        type="function",
        function=SimpleNamespace(name="search_web", arguments="{}"),
        raw_arguments="{}",
        parsed_arguments={"query": "test"},
    )

    await llm_service_module._execute_business_tool(
        tool_call=tool_call,
        image_session=None,
        phase_prefix="reader",
        round_num=1,
        memory_service=None,
        group_id=str(GROUP_ID),
        allowed_profile_user_ids=frozenset(),
        caller_user_id=None,
        caller_group_id=None,
        caller_is_superuser=False,
        request_trace_id="chat-2",
        parent_call_id=None,
    )

    assert scripted.calls != [], "chat.tool_search 搜索前未调用 adjudicate"
    assert search_calls == [], (
        "chat.tool_search 在受限准入下仍发起了 search_web"
    )


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
    assert get_msg_called == [], (
        "chat.group_read 在受限下仍发生了 get_msg 平台读取"
    )


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
    assert scheduled is False, (
        "chat.reaction 在受限准入下仍派发“生成中”表情"
    )
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
