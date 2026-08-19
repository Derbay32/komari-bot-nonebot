"""TSK-192 回复 Agent 执行预算验收测试。

覆盖验收清单的运行时部分：普通 / debug / 简单入口使用同一份任务冻结预算
（AC3/AC10）、入口专属隐藏封顶删除（AC4）、全部工具调用计入总量且超量
整批拒绝（AC5/AC7）、无工具轮只耗轮次（AC6）、简单回复整任务重试删除与
provider 瞬时重试仍属同一逻辑轮次（AC8）、Agent Run 结构化预算元数据
（AC9）。

测试 seam 遵循 TSK-188 Testing Decisions：公开 chat LLM 服务
（``generate_reply`` / ``generate_reply_with_tools``）与公开无副作用 debug
生成入口（``MessageHandler.generate_debug_reply``），配合可控 provider 与
业务工具替身；只断言可观察行为（provider 请求次数/轮次阶段/工具执行次数/
结果/collector 记录），不断言私有计数 helper 或内部常量。

所有预算 fixture 必须满足真实 Schema 约束（``per_round <= total <=
rounds*per_round``，另含字段范围 2..20 / 1..8 / 2..64），由
``_assert_agent_budget_consistent`` 在两个 builder 返回前守卫；禁止用
SimpleNamespace 绕过 Pydantic 构造线上不可能状态。
"""

from __future__ import annotations

import asyncio
import sys
import types
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

import komari_bot.plugins as plugins_package
from komari_bot.plugins.agent_run_logger.diagnostic import AgentRunCollector
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema
from komari_bot.plugins.llm_provider.base_client import (
    LLMCompletionResultSchema,
    LLMProviderContinuationSchema,
    LLMToolCallFunctionSchema,
    LLMToolCallSchema,
)

retry_module = import_module("komari_bot.plugins.komari_memory.core.retry")
agent_budget_module = import_module(
    "komari_bot.plugins.komari_chat.services.agent_budget"
)
llm_service_module = import_module(
    "komari_bot.plugins.komari_chat.services.llm_service"
)
message_handler_module = import_module(
    "komari_bot.plugins.komari_chat.handlers.message_handler"
)
prompt_builder_module = import_module(
    "komari_bot.plugins.komari_chat.services.prompt_builder"
)

from tests.config.prompt_field_contract import prompt_marker_values

# ── 基础替身 ────────────────────────────────────────────────────────────


async def _no_sleep(_seconds: float) -> Any:
    return None


async def _fake_build_prompt(**_kwargs: object) -> list[dict[str, object]]:
    return [{"role": "user", "content": "test"}]


def _tool_call(
    name: str,
    arguments: str,
    parsed_arguments: dict[str, object],
    *,
    call_id: str = "call-1",
) -> LLMToolCallSchema:
    """按真实业务协议构造工具调用（不绕私有 helper）。"""
    return LLMToolCallSchema(
        id=call_id,
        type="function",
        function=LLMToolCallFunctionSchema(name=name, arguments=arguments),
        raw_arguments=arguments,
        parsed_arguments=parsed_arguments,
    )


def _completion(*tool_calls: LLMToolCallSchema) -> LLMCompletionResultSchema:
    return LLMCompletionResultSchema(
        content="",
        tool_calls=list(tool_calls),
        finish_reason="tool_calls" if tool_calls else "stop",
    )


def _final_response_completion(
    content: str = "预算测试回复",
) -> LLMCompletionResultSchema:
    return _completion(
        _tool_call(
            "final_response",
            "{}",
            {
                "content": content,
                "interaction_history": {
                    "event": "打招呼",
                    "result": "回应",
                    "emotion": "开心",
                },
            },
        )
    )


def _search_completion(*queries: str) -> LLMCompletionResultSchema:
    return _completion(
        *[
            _tool_call(
                "search_web",
                f'{{"query":"{query}"}}',
                {"query": query},
                call_id=f"call-search-{index}",
            )
            for index, query in enumerate(queries)
        ]
    )


class _ScriptedProvider:
    """按队列返回 completion；异常实例按原样抛出（驱动瞬时故障重试）。"""

    def __init__(self, steps: list[Any] | None = None) -> None:
        self.completion_calls: list[dict[str, Any]] = []
        self.steps = list(steps or [])

    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        self.completion_calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.fail = False

    async def search_web(self, query: str, **_kwargs: object) -> str:
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("搜索服务网络中断")
        return f"搜索结果：{query}"

    async def fetch_page(self, *_args: object, **_kwargs: object) -> str:
        return "[抓取结果]"


class _FakeRedis:
    async def get_buffer(self, group_id: str, limit: int = 100) -> list[MessageSchema]:
        del group_id, limit
        return []

    async def push_message(self, group_id: str, message: MessageSchema) -> None:
        del group_id, message

    async def get_global_interaction_buffer(
        self,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        del user_id, limit
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
    async def rewrite_query(
        self,
        current_query: str,
        **_kwargs: object,
    ) -> str:
        return current_query


class _FakeEmbeddingProvider:
    async def embed(self, text: str) -> list[float]:
        del text
        return [0.1, 0.2]


class _FakeUserData:
    def get_config(self) -> SimpleNamespace:
        return SimpleNamespace(max_favorability_delta_per_reply=5)

    async def get_user_favorability(self, user_id: str) -> SimpleNamespace:
        del user_id
        return SimpleNamespace(favorability=0)


def _assert_agent_budget_consistent(rounds: int, per_round: int, total: int) -> None:
    """测试侧守卫：预算三元组必须是真实 Schema 可接受状态。

    线上 Pydantic 约束为字段范围 2..20 / 1..8 / 2..64 且
    ``per_round <= total <= rounds*per_round``。故意不 import 生产
    validator 自证；任何 runtime budget fixture 都必须在返回前通过。
    """

    assert 2 <= rounds <= 20, f"agent_max_rounds 超出 2..20: {rounds}"
    assert 1 <= per_round <= 8, f"agent_max_tool_calls_per_round 超出 1..8: {per_round}"
    assert 2 <= total <= 64, f"agent_max_total_tool_calls 超出 2..64: {total}"
    assert per_round <= total <= rounds * per_round, (
        f"非法预算组合 rounds={rounds}, per_round={per_round}, total={total}："
        "必须满足 per_round <= total <= rounds*per_round"
    )


def _build_config(**overrides: Any) -> SimpleNamespace:
    """内存 + 预算字段合并的配置替身（TSK-192 默认 10/4/20）。"""
    values: dict[str, Any] = {
        "llm_model_chat": "chat-model",
        "llm_temperature_chat": 0.7,
        "llm_max_tokens_chat": 1024,
        "llm_thinking_mode_chat": False,
        "llm_reasoning_effort_chat": "",
        "llm_request_api_chat": "chat_completions",
        "llm_stream_enabled_chat": False,
        "llm_model_summary": "summary-model",
        "llm_temperature_summary": 0.3,
        "llm_max_tokens_summary": 2048,
        "llm_thinking_mode_summary": False,
        "llm_reasoning_effort_summary": "",
        "bot_nickname": "小鞠",
        "agent_max_rounds": 10,
        "agent_max_tool_calls_per_round": 4,
        "agent_max_total_tool_calls": 20,
        # TSK-193：工具调用约束模式（与配置 Schema 默认值一致）
        "agent_tool_call_mode": "required",
    }
    values.update(overrides)
    _assert_agent_budget_consistent(
        values["agent_max_rounds"],
        values["agent_max_tool_calls_per_round"],
        values["agent_max_total_tool_calls"],
    )
    return SimpleNamespace(**values)


def _build_chat_config_stub(**overrides: Any) -> SimpleNamespace:
    """handler 级测试使用的合并配置替身（get_config 与 get_memory_config 共用）。"""
    values: dict[str, Any] = {
        "proactive_enabled": False,
        "context_messages_limit": 10,
        "context_max_utf8_bytes": 24_000,
        "context_max_estimated_tokens": 6_000,
        "summary_max_buffer_size": 500,
        "memory_search_limit": 3,
        "bot_nickname": "小鞠",
        "memory_agent_lock_timeout_seconds": 5,
        "global_interaction_enabled": True,
        "global_interaction_trigger_size": 20,
        "face_reaction_enabled": False,
        "face_reaction_id": "76",
        # TSK-194：图片理解模式（合并替身承载 get_config/get_memory_config）
        "image_understanding_mode": "delegated",
        "error_notify_enabled": False,
        "knowledge_enabled": False,
    }
    values.update(_build_config().__dict__)
    values.update(overrides)
    _assert_agent_budget_consistent(
        values["agent_max_rounds"],
        values["agent_max_tool_calls_per_round"],
        values["agent_max_total_tool_calls"],
    )
    return SimpleNamespace(**values)


def _wire_handler(
    monkeypatch: pytest.MonkeyPatch,
    config: SimpleNamespace,
    provider: _ScriptedProvider,
    *,
    search: _FakeSearch | None = None,
) -> tuple[Any, _FakeSearch]:
    """布设 handler 依赖，保留真实 _generate_reply_core 与真实工具循环。"""
    search = search or _FakeSearch()
    handler = message_handler_module.MessageHandler.__new__(
        message_handler_module.MessageHandler
    )
    handler.redis = _FakeRedis()
    handler.memory = _FakeMemory()
    handler.query_rewrite = _FakeQueryRewrite()
    handler._reaction_tasks = set()

    monkeypatch.setattr(message_handler_module, "get_config", lambda: config)
    monkeypatch.setattr(message_handler_module, "get_memory_config", lambda: config)
    monkeypatch.setattr(message_handler_module, "build_prompt", _fake_build_prompt)
    monkeypatch.setattr(
        message_handler_module,
        "komari_search_plugin",
        SimpleNamespace(
            is_search_available=lambda **_kwargs: True,
            is_fetch_available=lambda **_kwargs: False,
        ),
    )
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _FakeUserData())
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )

    embedding_package_name = "komari_bot.plugins.embedding_provider"
    embedding_fake = types.ModuleType(embedding_package_name)
    embedding_fake.embed = _FakeEmbeddingProvider().embed  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, embedding_package_name, embedding_fake)
    monkeypatch.setattr(
        plugins_package, "embedding_provider", embedding_fake, raising=False
    )
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)
    return handler, search


def _make_message(message_id: str = "msg-1") -> MessageSchema:
    return MessageSchema(
        user_id="user-1",
        user_nickname="测试用户",
        group_id="group-1",
        content="测试预算",
        timestamp=1.0,
        message_id=message_id,
    )


# ── 入口一致性：普通 / debug / 简单使用同一份配置预算（AC3 / AC10） ──────


def test_simple_reply_entry_uses_configured_budget_without_whole_task_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """简单回复入口按配置轮次失败，且删除整任务重试（AC8）。

    旧实现：整任务重试 3 次 x 2 轮 = 6 次 provider 调用；
    新契约：配置轮次 2 → 恰好 2 次 provider 调用，整任务只跑一次。
    """
    provider = _ScriptedProvider([_completion() for _ in range(6)])
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="最大轮数"):
        asyncio.run(
            llm_service_module.generate_reply(
                config=_build_config(agent_max_rounds=2, agent_max_total_tool_calls=8),
                messages=[{"role": "user", "content": "你好"}],
            )
        )

    assert len(provider.completion_calls) == 2


def test_normal_entry_uses_configured_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """普通回复入口（_attempt_reply）使用配置轮次，不再有调用点封顶 5。"""
    config = _build_chat_config_stub(agent_max_rounds=2, agent_max_total_tool_calls=8)
    provider = _ScriptedProvider([_completion() for _ in range(5)])
    handler, _search = _wire_handler(monkeypatch, config, provider)

    _pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=_make_message(),
            reply_to_message_id="msg-1",
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="at",
            reply_score=1.0,
            store_current=True,
        )
    )

    assert failure is not None
    assert failure.error_type == "RuntimeError"
    assert len(provider.completion_calls) == 2


def test_debug_entry_uses_configured_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """debug 无副作用入口（generate_debug_reply）使用配置轮次，不再有调用点封顶 5。"""
    config = _build_chat_config_stub(agent_max_rounds=2, agent_max_total_tool_calls=8)
    provider = _ScriptedProvider([_completion() for _ in range(5)])
    handler, _search = _wire_handler(monkeypatch, config, provider)

    with pytest.raises(RuntimeError, match="最大轮数"):
        asyncio.run(
            handler.generate_debug_reply(
                group_id="group-1",
                user_id="user-1",
                user_nickname="测试用户",
                content="测试预算",
            )
        )

    assert len(provider.completion_calls) == 2


def test_next_task_receives_updated_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """任务之间配置变更只影响下一任务：同一入口第二次运行使用新轮次。"""
    config = _build_config(agent_max_rounds=2, agent_max_total_tool_calls=8)
    provider = _ScriptedProvider([_completion() for _ in range(6)])
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="最大轮数"):
        asyncio.run(
            llm_service_module.generate_reply(
                config=config,
                messages=[{"role": "user", "content": "第一次"}],
            )
        )
    assert len(provider.completion_calls) == 2

    config.agent_max_rounds = 3
    provider2 = _ScriptedProvider([_completion() for _ in range(3)])
    monkeypatch.setattr(llm_service_module, "llm_provider", provider2)

    with pytest.raises(RuntimeError, match="最大轮数"):
        asyncio.run(
            llm_service_module.generate_reply(
                config=config,
                messages=[{"role": "user", "content": "第二次"}],
            )
        )
    assert len(provider2.completion_calls) == 3


def test_debug_entry_freezes_budget_snapshot_across_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务内冻结：debug 入口中途 mutate 配置，当前任务仍按任务开始快照执行。

    使用旧实现无法达到的值：per_round=6（旧常量 4 会拒绝 6 连搜）；
    任务执行中把三项预算改为合法最小值（2/1/2），冻结实现仍完成后续轮次。
    """
    config = _build_chat_config_stub(
        agent_max_rounds=8,
        agent_max_tool_calls_per_round=6,
        agent_max_total_tool_calls=30,
    )
    provider = _ScriptedProvider(
        [
            _search_completion("a", "b", "c", "d", "e", "f"),
            _completion(
                _tool_call(
                    "record_favorability_delta",
                    '{"delta":0,"reason":"冻结测试"}',
                    {"delta": 0, "reason": "冻结测试"},
                    call_id="call-favor-freeze",
                )
            ),
            _search_completion("g", "h", "i", "j"),
            _final_response_completion(),
        ]
    )
    handler, search = _wire_handler(monkeypatch, config, provider)
    original_search_web = search.search_web

    async def _mutating_search_web(query: str, **kwargs: object) -> str:
        # 模拟任务执行期间运维把预算改成合法最小值（经公开 search_web 工具 seam 触发）
        config.agent_max_rounds = 2
        config.agent_max_tool_calls_per_round = 1
        config.agent_max_total_tool_calls = 2
        return await original_search_web(query, **kwargs)

    monkeypatch.setattr(
        llm_service_module.komari_search,
        "search_web",
        _mutating_search_web,
    )

    result = asyncio.run(
        handler.generate_debug_reply(
            group_id="group-1",
            user_id="user-1",
            user_nickname="测试用户",
            content="冻结预算测试",
        )
    )

    assert result.reply == "预算测试回复"
    # 6 + 4 连搜全部执行：per_round=6/总预算 30 在任务开始时冻结；
    # 若中途重读配置，后续批次会被新预算整批拒绝
    assert len(search.queries) == 10


def test_normal_entry_freezes_budget_snapshot_across_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通回复入口（_attempt_reply）任务内冻结预算：中途变更不影响当前任务。"""
    config = _build_chat_config_stub(
        agent_max_rounds=3,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=4,
    )
    provider = _ScriptedProvider(
        [
            _search_completion("a"),
            _completion(
                _tool_call(
                    "record_favorability_delta",
                    '{"delta":0,"reason":"冻结测试"}',
                    {"delta": 0, "reason": "冻结测试"},
                    call_id="call-favor-normal-freeze",
                )
            ),
            _final_response_completion(),
        ]
    )
    handler, search = _wire_handler(monkeypatch, config, provider)
    original_search_web = search.search_web

    async def _mutating_search_web(query: str, **kwargs: object) -> str:
        # 第一轮执行期间把预算改为合法最小值（2/1/2 仍在 Schema 边界内）
        config.agent_max_rounds = 2
        config.agent_max_tool_calls_per_round = 1
        config.agent_max_total_tool_calls = 2
        return await original_search_web(query, **kwargs)

    monkeypatch.setattr(
        llm_service_module.komari_search,
        "search_web",
        _mutating_search_web,
    )

    pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=_make_message(),
            reply_to_message_id="msg-1",
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="at",
            reply_score=1.0,
            store_current=True,
        )
    )

    assert failure is None
    assert pending is not None
    assert pending.reply == "预算测试回复"
    assert search.queries == ["a"]
    # 变更后的 2/1/2 若在任务内被重读，后续轮次会被整批拒绝或提前终止
    assert len(provider.completion_calls) == 3


# ── 预算计数：全部工具调用计入总量，超量整批拒绝（AC5 / AC7 / AC10） ──


def test_total_budget_rejects_whole_batch_before_any_tool_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """总预算超限：整批工具调用在任何业务工具执行前拒绝，不执行半批。"""
    config = _build_config(
        agent_max_rounds=2,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=4,
    )
    provider = _ScriptedProvider(
        [
            _search_completion("a", "b", "c"),
            _search_completion("d", "e"),
            _final_response_completion(),
        ]
    )
    search = _FakeSearch()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )

    with pytest.raises(RuntimeError, match="工具预算上限"):
        asyncio.run(
            llm_service_module.generate_reply_with_tools(
                config=config,
                messages=[{"role": "user", "content": "批量搜索"}],
                tools=[llm_service_module.SEARCH_WEB_TOOL],
            )
        )

    # 第一轮 3 次执行；第二轮 2 次未执行任何一次（无半批）
    assert search.queries == ["a", "b", "c"]


def test_per_round_rejected_batch_still_counts_toward_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被单轮上限拒绝的调用也计入总预算：其后批次因总量不足被整体拒绝。"""
    config = _build_config(
        agent_max_rounds=3,
        agent_max_tool_calls_per_round=2,
        agent_max_total_tool_calls=3,
    )
    provider = _ScriptedProvider(
        [
            _search_completion("a"),
            _search_completion("b", "c", "d"),
            _search_completion("e"),
        ]
    )
    search = _FakeSearch()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )

    with pytest.raises(RuntimeError, match="工具预算上限"):
        asyncio.run(
            llm_service_module.generate_reply_with_tools(
                config=config,
                messages=[{"role": "user", "content": "超单轮搜索"}],
                tools=[llm_service_module.SEARCH_WEB_TOOL],
            )
        )

    # 第一轮 1 次执行；第二轮 3 连搜整体拒绝；若被拒批未计总量，
    # 第三轮会再执行一次（旧实现恰好如此）
    assert search.queries == ["a"]


def test_all_tool_call_outcomes_consume_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功、未知、参数错误、好感度、final_response 全部计入总预算（AC5）。"""
    config = _build_config(
        agent_max_rounds=5,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=4,
    )
    provider = _ScriptedProvider(
        [
            _search_completion("a"),
            _completion(
                _tool_call(
                    "unknown_tool",
                    '{"arg":"val"}',
                    {"arg": "val"},
                    call_id="call-unknown",
                )
            ),
            _completion(
                _tool_call(
                    "record_favorability_delta",
                    '{"delta":1,"reason":"互动"}',
                    {"delta": 1, "reason": "互动"},
                    call_id="call-favor",
                )
            ),
            _completion(
                _tool_call(
                    "final_response",
                    "{}",
                    {
                        "interaction_history": {
                            "event": "e",
                            "result": "r",
                            "emotion": "m",
                        }
                    },
                    call_id="call-final-bad",
                )
            ),
            _final_response_completion("未执行的最终回复"),
        ]
    )
    search = _FakeSearch()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )

    with pytest.raises(RuntimeError, match="工具预算上限"):
        asyncio.run(
            llm_service_module.generate_reply_with_tools(
                config=config,
                messages=[{"role": "user", "content": "混合工具"}],
                tools=[
                    llm_service_module.SEARCH_WEB_TOOL,
                    llm_service_module.RECORD_FAVORABILITY_DELTA_TOOL,
                ],
            )
        )

    # 五种结果各占 1 次调用：成功/未知/好感度/参数错误 = 4，恰好占满 total；
    # 若任意一种未计总量，第五轮合法 final_response 会成功而非被拒。
    # 旧实现轮次上限 3 时只消耗 3 轮即失败，无法到达第五轮。
    assert len(provider.completion_calls) == 5
    assert search.queries == ["a"]


def test_execution_failure_consumes_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """业务工具执行失败也计入总预算：两次失败后 final_response 被拒绝。"""
    config = _build_config(
        agent_max_rounds=3,
        agent_max_tool_calls_per_round=2,
        agent_max_total_tool_calls=2,
    )
    provider = _ScriptedProvider(
        [
            _search_completion("a"),
            _search_completion("b"),
            _final_response_completion("不应成功"),
        ]
    )
    search = _FakeSearch()
    search.fail = True
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )

    with pytest.raises(RuntimeError, match="工具预算上限"):
        asyncio.run(
            llm_service_module.generate_reply_with_tools(
                config=config,
                messages=[{"role": "user", "content": "失败搜索"}],
                tools=[llm_service_module.SEARCH_WEB_TOOL],
            )
        )

    # 两次失败的搜索都尝试执行并占满 total=2；若失败不占配额，
    # 第三轮 final_response 会成功
    assert search.queries == ["a", "b"]


def test_no_tool_round_consumes_rounds_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """无工具调用的轮次只消耗轮次预算，不增加工具计数（AC6）。"""
    config = _build_config(
        agent_max_rounds=3,
        agent_max_tool_calls_per_round=2,
        agent_max_total_tool_calls=2,
    )
    provider = _ScriptedProvider(
        [
            _completion(),
            _search_completion("a"),
            _final_response_completion("无工具轮后成功"),
        ]
    )
    search = _FakeSearch()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )

    result = asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=config,
            messages=[{"role": "user", "content": "空轮次"}],
            tools=[llm_service_module.SEARCH_WEB_TOOL],
        )
    )

    # 空轮若计入工具计数，第 3 轮 total 会变 3 > 2 被拒绝
    assert result.content == "无工具轮后成功"
    assert search.queries == ["a"]


# ── 重试边界（AC8 / AC10） ────────────────────────────────────────────


def test_provider_transient_retry_stays_within_same_logical_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider 单 completion 瞬时故障重试仍属同一逻辑轮次（不新增轮次计数）。"""
    provider = _ScriptedProvider(
        [RuntimeError("瞬时网络故障"), _final_response_completion()]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

    result = asyncio.run(
        llm_service_module.generate_reply(
            config=_build_config(),
            messages=[{"role": "user", "content": "你好"}],
        )
    )

    assert result.content == "预算测试回复"
    assert len(provider.completion_calls) == 2
    # 两次尝试都属于同一逻辑轮次，第二轮才进入新轮次
    assert [call["request_phase"] for call in provider.completion_calls] == [
        "normal_reply_round_1",
        "normal_reply_round_1",
    ]


# ── Agent Run 预算元数据（AC9） ────────────────────────────────────────


def test_agent_run_records_frozen_budget_and_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent Run 记录冻结预算与轮次/工具消耗，且保留既有记录结构。

    ``record["budget"]`` 块（任务开始冻结的预算值 + rounds_used /
    tool_calls_used）是本 ticket 选定的外部 JSONL contract，经公开
    collector 构造 seam 观察；消耗计数与 ``record["rounds"]`` 的工具提案
    数一致。只断言预算相关字段与既有顶层键存在，不复制/校验与预算无关的
    完整正文（正文内容由 agent_run_logger 自有测试基线负责）。
    """
    config = _build_config(
        agent_max_rounds=2,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=4,
    )
    provider = _ScriptedProvider(
        [
            _search_completion("a"),
            _final_response_completion("预算记录回复"),
        ]
    )
    search = _FakeSearch()
    original_search_web = search.search_web

    async def _mutating_search_web(query: str, **kwargs: object) -> str:
        # 任务执行期间把配置改为合法最小值：记录中必须是任务开始时的冻结预算
        config.agent_max_rounds = 2
        config.agent_max_tool_calls_per_round = 1
        config.agent_max_total_tool_calls = 2
        return await original_search_web(query, **kwargs)

    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=_mutating_search_web, fetch_page=search.fetch_page),
    )
    collector = AgentRunCollector(request_id="budget-tsk-192")

    result = asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=config,
            messages=[{"role": "user", "content": "预算记录"}],
            tools=[llm_service_module.SEARCH_WEB_TOOL],
            collector=collector,
        )
    )
    collector.mark_finished(status="success", output=result)
    record = collector.build_record()

    budget = record["budget"]
    assert budget["agent_max_rounds"] == 2
    assert budget["agent_max_tool_calls_per_round"] == 4
    assert budget["agent_max_total_tool_calls"] == 4
    assert budget["rounds_used"] == 2
    assert budget["tool_calls_used"] == 2

    # 消耗计数与结构化 trace 一致，不断言/复制完整正文
    assert len(record["rounds"]) == budget["rounds_used"]
    proposed_calls = sum(
        len(trace_entry["response"]["tool_calls"]) for trace_entry in record["rounds"]
    )
    assert proposed_calls == budget["tool_calls_used"]

    # 既有记录结构不因新增预算元数据而改变（脱敏边界不被削弱）；仅检查
    # 顶层键存在，不校验与预算无关的正文内容
    for key in (
        "schema_version",
        "rounds",
        "tool_executions",
        "errors",
        "usage",
        "input",
        "output",
    ):
        assert key in record


# ── 隐藏封顶删除（AC4 / 边界 AC10） ───────────────────────────────────


def test_configured_rounds_at_upper_bound_are_honored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC4：配置轮次上界 20 生效，旧隐藏封顶 6 不再限制。"""
    provider = _ScriptedProvider(
        [_completion() for _ in range(19)] + [_final_response_completion()]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

    result = asyncio.run(
        llm_service_module.generate_reply(
            config=_build_config(agent_max_rounds=20),
            messages=[{"role": "user", "content": "边界轮次"}],
        )
    )

    assert result.content == "预算测试回复"
    assert len(provider.completion_calls) == 20


def test_configured_per_round_upper_bound_batch_is_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC4：单轮 8 个工具调用整批执行，旧每轮上限 4 不再生效。"""
    queries = [chr(ord("a") + index) for index in range(8)]
    provider = _ScriptedProvider(
        [
            _search_completion(*queries),
            _final_response_completion(),
        ]
    )
    search = _FakeSearch()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )

    result = asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=_build_config(
                agent_max_rounds=2,
                agent_max_tool_calls_per_round=8,
                agent_max_total_tool_calls=16,
            ),
            messages=[{"role": "user", "content": "八连搜"}],
            tools=[llm_service_module.SEARCH_WEB_TOOL],
        )
    )

    assert result.content == "预算测试回复"
    assert search.queries == queries


def test_provider_transient_retry_in_tool_loop_stays_same_logical_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具路径下 provider 瞬时重试仍属同一逻辑轮，失败尝试不耗预算（AC8）。"""
    config = _build_config(
        agent_max_rounds=2,
        agent_max_tool_calls_per_round=2,
        agent_max_total_tool_calls=2,
    )
    provider = _ScriptedProvider(
        [
            RuntimeError("瞬时网络故障"),
            _search_completion("a"),
            _final_response_completion("重试后回复"),
        ]
    )
    search = _FakeSearch()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

    result = asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=config,
            messages=[{"role": "user", "content": "瞬断重试"}],
            tools=[llm_service_module.SEARCH_WEB_TOOL],
        )
    )

    assert result.content == "重试后回复"
    assert [call["request_phase"] for call in provider.completion_calls] == [
        "search_tool_round_1",
        "search_tool_round_1",
        "search_tool_round_2",
    ]


def test_public_entries_have_no_max_tool_rounds_parameter() -> None:
    """公开入口不再暴露 max_tool_rounds 参数（不再有入口专属默认值）。"""
    for func_name in ("generate_reply", "generate_reply_with_tools"):
        signature = __import__("inspect").signature(
            getattr(llm_service_module, func_name)
        )
        assert "max_tool_rounds" not in signature.parameters, func_name


# ── TSK-193：工具调用约束模式与裸文本硬协议 ────────────────────────────


def _marker_template(*, instruction: str = "MARKER-工具调用指令-必须调用final_response") -> Any:
    """经公开 Prompt loader seam 注入的完整 marker Prompt 快照。"""
    template = dict(prompt_marker_values("komari_chat"))
    template["tool_call_instruction"] = instruction
    return template


class _RichUserData(_FakeUserData):
    """真实 build_prompt 需要好感度对象携带画像字段。"""

    async def get_user_favorability(self, user_id: str) -> SimpleNamespace:
        del user_id
        return SimpleNamespace(
            user_id="user-1",
            favorability=0,
            stage_index=0,
            stage_name="初识",
            stage_prompt="保持自然",
        )


def test_required_mode_sends_tool_choice_in_plain_and_thinking_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-193 AC2：required 在普通与思考模式都向 provider 提交 tool_choice。"""
    for thinking in (False, True):
        config = _build_config(
            agent_tool_call_mode="required",
            llm_thinking_mode_chat=thinking,
        )
        provider = _ScriptedProvider([_final_response_completion()])
        monkeypatch.setattr(llm_service_module, "llm_provider", provider)
        monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

        asyncio.run(
            llm_service_module.generate_reply(
                config=config,
                messages=[{"role": "user", "content": "你好"}],
            )
        )

        assert provider.completion_calls[0]["tool_choice"] == "required"
        assert provider.completion_calls[0]["thinking_mode"] is thinking


def test_prompt_guided_simple_entry_omits_tool_choice_and_injects_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-193 AC3：prompt_guided 不发送 tool_choice，并注入 DB Prompt marker。

    经公开 Prompt loader seam（prompt_builder.get_template）注入完整
    marker 快照，用真实 build_prompt 构造消息，再经简单入口提交：
    provider 收到的请求不得包含 tool_choice，且 messages 含 marker。
    """
    config = _build_config(
        agent_tool_call_mode="prompt_guided",
        knowledge_enabled=False,
    )
    template = _marker_template()
    marker = template["tool_call_instruction"]
    monkeypatch.setattr(
        prompt_builder_module,
        "get_template",
        _template_loader(template),
    )
    provider = _ScriptedProvider([_final_response_completion()])
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

    messages = asyncio.run(
        prompt_builder_module.build_prompt(
            user_message="你好",
            memories=[],
            config=config,
        )
    )
    asyncio.run(
        llm_service_module.generate_reply(
            config=config,
            messages=messages,
        )
    )

    call = provider.completion_calls[0]
    assert "tool_choice" not in call, "prompt_guided 不得发送 tool_choice 参数"
    rendered = "\n".join(str(message.get("content", "")) for message in call["messages"])
    assert marker in rendered, "请求 messages 必须包含数据库 tool_call_instruction marker"


def _template_loader(template: dict[str, str]) -> Any:
    async def _loader() -> dict[str, str]:
        return dict(template)

    return _loader


@pytest.mark.parametrize("entry", ["normal", "debug"])
def test_prompt_guided_normal_and_debug_entries_match_simple_entry(
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    """TSK-193 AC3：普通 / debug 与简单入口的 prompt_guided 语义一致。"""
    config = _build_chat_config_stub(agent_tool_call_mode="prompt_guided")
    # handler 工具集含 record_favorability_delta：先记录好感度再 final_response
    provider = _ScriptedProvider(
        [
            _completion(
                _tool_call(
                    "record_favorability_delta",
                    '{"delta":0,"reason":"TSK193约束模式"}',
                    {"delta": 0, "reason": "TSK193约束模式"},
                    call_id="call-favor-tsk193",
                )
            ),
            _final_response_completion(),
        ]
    )
    handler, _search = _wire_handler(monkeypatch, config, provider)
    template = _marker_template()
    marker = template["tool_call_instruction"]
    monkeypatch.setattr(
        prompt_builder_module,
        "get_template",
        _template_loader(template),
    )
    monkeypatch.setattr(
        message_handler_module,
        "build_prompt",
        _prompt_builder_wrapper,
    )
    monkeypatch.setattr(message_handler_module, "user_data_plugin", _RichUserData())

    if entry == "debug":
        result = asyncio.run(
            handler.generate_debug_reply(
                group_id="group-1",
                user_id="user-1",
                user_nickname="测试用户",
                content="测试约束模式",
            )
        )
        assert result.reply == "预算测试回复"
    else:
        pending, _stored, failure = asyncio.run(
            handler._attempt_reply(
                bot_self_id="bot-1",
                adapter_name="OneBot V11",
                message=_make_message(),
                reply_to_message_id="msg-1",
                image_urls=None,
                reply_context=None,
                reply_context_requested=False,
                reply_context_refetched=False,
                force_reply=True,
                reason="at",
                reply_score=1.0,
                store_current=True,
            )
        )
        assert failure is None
        assert pending is not None
        assert pending.reply == "预算测试回复"

    call = provider.completion_calls[0]
    assert "tool_choice" not in call, f"{entry} 入口 prompt_guided 不得发送 tool_choice"
    rendered = "\n".join(str(message.get("content", "")) for message in call["messages"])
    assert marker in rendered, f"{entry} 入口请求 messages 必须包含 marker"


async def _prompt_builder_wrapper(**kwargs: Any) -> list[dict[str, object]]:
    return await prompt_builder_module.build_prompt(**kwargs)


def test_tool_call_mode_frozen_at_task_start_and_applies_next_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-193：任务内冻结工具约束模式；下一任务才生效。"""
    config = _build_config(
        agent_tool_call_mode="prompt_guided",
        agent_max_rounds=2,
        agent_max_total_tool_calls=8,
    )
    provider = _ScriptedProvider(
        [_search_completion("a"), _final_response_completion()]
    )
    search = _FakeSearch()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(
        llm_service_module,
        "komari_search",
        SimpleNamespace(search_web=search.search_web, fetch_page=search.fetch_page),
    )
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

    original_search_web = search.search_web

    async def _mutating_search_web(query: str, **kwargs: object) -> str:
        # 任务执行期间运维把模式改为 required：冻结实现当前任务仍保持 prompt_guided
        config.agent_tool_call_mode = "required"
        return await original_search_web(query, **kwargs)

    monkeypatch.setattr(
        llm_service_module.komari_search,
        "search_web",
        _mutating_search_web,
    )

    asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=config,
            messages=[{"role": "user", "content": "约束模式冻结"}],
            tools=[llm_service_module.SEARCH_WEB_TOOL],
        )
    )

    assert len(provider.completion_calls) == 2
    assert all("tool_choice" not in call for call in provider.completion_calls), (
        "任务内配置变更不得影响已冻结的 prompt_guided 模式"
    )
    assert search.queries == ["a"]

    # 下一任务读取新值：required 生效
    provider2 = _ScriptedProvider([_final_response_completion()])
    monkeypatch.setattr(llm_service_module, "llm_provider", provider2)
    asyncio.run(
        llm_service_module.generate_reply(
            config=config,
            messages=[{"role": "user", "content": "下一任务"}],
        )
    )
    assert provider2.completion_calls[0]["tool_choice"] == "required"


class _RejectingProvider(_ScriptedProvider):
    """模拟不兼容 required 的 provider：每轮都抛错，并记录请求。"""

    async def generate_messages_completion(self, **kwargs: Any) -> Any:
        self.completion_calls.append(kwargs)
        raise RuntimeError("required_rejected")


def test_required_thinking_provider_rejection_fails_clearly_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-193 AC5：不兼容 provider 拒绝 required 时明确失败。

    简单入口：异常经单请求瞬时重试后上抛；不得自动关闭思考、换模型、
    删除 tool_choice 或改写模式。
    """
    config = _build_config(
        agent_tool_call_mode="required",
        llm_thinking_mode_chat=True,
        agent_max_rounds=2,
        agent_max_total_tool_calls=8,
    )
    provider = _RejectingProvider()
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)

    with pytest.raises(RuntimeError, match="required_rejected"):
        asyncio.run(
            llm_service_module.generate_reply(
                config=config,
                messages=[{"role": "user", "content": "思考模式强制工具"}],
            )
        )

    assert len(provider.completion_calls) == 3
    for call in provider.completion_calls:
        assert call["tool_choice"] == "required"
        assert call["thinking_mode"] is True
        assert call["model"] == "chat-model"


def test_handler_reports_incompatible_provider_rejection_as_reply_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-193 AC5：消息处理公开失败 seam 走既有 ReplyFailureInfo 边界。

    不重新铺整条群内错误 / SUPERUSER 通知 fixture（已有 test_error_notify
    覆盖该边界），只断言失败诊断信息与请求不可变。
    """
    config = _build_chat_config_stub(
        agent_tool_call_mode="required",
        llm_thinking_mode_chat=True,
    )
    provider = _RejectingProvider()
    handler, _search = _wire_handler(monkeypatch, config, provider)

    pending, _stored, failure = asyncio.run(
        handler._attempt_reply(
            bot_self_id="bot-1",
            adapter_name="OneBot V11",
            message=_make_message(),
            reply_to_message_id="msg-1",
            image_urls=None,
            reply_context=None,
            reply_context_requested=False,
            reply_context_refetched=False,
            force_reply=True,
            reason="at",
            reply_score=1.0,
            store_current=True,
        )
    )

    assert pending is None
    assert failure is not None
    assert failure.stage == "generate"
    assert failure.error_type == "RuntimeError"
    for call in provider.completion_calls:
        assert call["tool_choice"] == "required"
        assert call["thinking_mode"] is True
        assert call["model"] == "chat-model"


def test_agent_run_records_frozen_tool_mode_violation_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-193 AC7/AC9：裸文本违例保留完整诊断，消耗 1 轮但 0 工具调用。

    记录冻结的 tool mode、协议违例与预算 usage；被拒绝的 completion 的
    正文 / reasoning / continuation 完整留在 collector 投影中。
    """
    config = _build_config(
        agent_tool_call_mode="prompt_guided",
        agent_max_rounds=2,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=4,
    )
    continuation = LLMProviderContinuationSchema(
        api="responses",
        output_items=[{"type": "message", "id": "msg_1"}, {"type": "reasoning", "id": "rs_1"}],
    )
    provider = _ScriptedProvider(
        [
            LLMCompletionResultSchema(
                content="裸文本违例正文",
                reasoning_content="裸文本轮推理正文",
                tool_calls=[],
                finish_reason="stop",
                continuation=continuation,
            ),
            _final_response_completion("违例后成功回复"),
        ]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)
    collector = AgentRunCollector(request_id="tsk193-violation")

    result = asyncio.run(
        llm_service_module.generate_reply_with_tools(
            config=config,
            messages=[{"role": "user", "content": "违例诊断"}],
            tools=[llm_service_module.SEARCH_WEB_TOOL],
            collector=collector,
        )
    )
    collector.mark_finished(status="success", output=result)
    record = collector.build_record()

    # 被拒 completion 完整保留（正文 / reasoning / continuation）
    first_response = record["rounds"][0]["response"]
    assert first_response["content"] == "裸文本违例正文"
    assert first_response["reasoning_content"] == "裸文本轮推理正文"
    assert first_response["continuation"]["output_items"] == [
        {"type": "message", "id": "msg_1"},
        {"type": "reasoning", "id": "rs_1"},
    ]

    # 冻结 tool mode 与任务整体消耗：违例轮(0 工具) + 成功轮(1 工具)
    assert record["budget"].get("agent_tool_call_mode") == "prompt_guided"
    assert record["budget"]["rounds_used"] == 2
    assert record["budget"]["tool_calls_used"] == 1
    assert len(record["rounds"]) == 2
    # 违例轮本身：1 个轮次、0 个工具调用
    assert len(record["rounds"][0]["response"]["tool_calls"]) == 0

    # 协议违例进入诊断错误列表（消息文本为既有可观察诊断文案）
    assert record["errors"], "裸文本违例必须记录协议违例诊断"
    assert any(
        "未调用任何工具" in str(error.get("message", "")) for error in record["errors"]
    )

    # 后续合法 final_response 仍成功
    assert result.content == "违例后成功回复"


# ── TSK-193 修订：from_config 严格 no-fallback 契约 ─────────────────────


def test_from_config_missing_tool_call_mode_raises() -> None:
    """运行时配置快照缺少 tool_call_mode 必须 RuntimeError，不得回退隐藏默认。

    0014 之后生产 typed 配置必然携带 ``agent_tool_call_mode``；快照缺字段
    只可能来自未迁移的旧快照或测试替身。TSK-193 验收基线修订后
    ``from_config`` 不再对缺失字段静默按 ``required`` 兼容，测试 fixture
    一律显式提供该字段（见 ``_build_config`` / ``_build_chat_config_stub``），
    本用例把该契约固化为公开行为。
    """
    config = _build_config()
    del config.agent_tool_call_mode
    with pytest.raises(RuntimeError):
        agent_budget_module.AgentExecutionBudget.from_config(config)


@pytest.mark.parametrize("invalid", ["auto", "", "None", "required ", "Required"])
def test_from_config_invalid_tool_call_mode_raises(invalid: str) -> None:
    """非法 mode 值必须 RuntimeError，不允许宽松规范化或静默降级。"""
    with pytest.raises(RuntimeError, match="必须为 required 或 prompt_guided"):
        agent_budget_module.AgentExecutionBudget.from_config(
            _build_config(agent_tool_call_mode=invalid)
        )


@pytest.mark.parametrize("mode", ["required", "prompt_guided"])
def test_from_config_freeze_explicit_valid_mode(mode: str) -> None:
    """合法 mode 显式冻结进预算快照；不依赖 dataclass / 配置隐藏默认。"""
    budget = agent_budget_module.AgentExecutionBudget.from_config(
        _build_config(agent_tool_call_mode=mode)
    )
    assert budget.tool_call_mode == mode


def test_consecutive_bare_text_until_rounds_exhausted_is_diagnosable_protocol_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-193 AC8：连续裸文本直到配置轮次耗尽抛出可诊断协议失败。

    恰好消耗配置轮次（无隐藏纠错轮）；被拒轮次的正文不得回填，
    第二轮请求中不存在 assistant 消息。
    """
    config = _build_config(
        agent_tool_call_mode="required",
        agent_max_rounds=2,
        agent_max_tool_calls_per_round=4,
        agent_max_total_tool_calls=4,
    )
    provider = _ScriptedProvider(
        [
            LLMCompletionResultSchema(
                content="第一轮裸文本", tool_calls=[], finish_reason="stop"
            ),
            LLMCompletionResultSchema(
                content="第二轮裸文本", tool_calls=[], finish_reason="stop"
            ),
        ]
    )
    monkeypatch.setattr(llm_service_module, "llm_provider", provider)
    monkeypatch.setattr(retry_module.asyncio, "sleep", _no_sleep)
    collector = AgentRunCollector(request_id="tsk193-rounds-exhausted")

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            llm_service_module.generate_reply(
                config=config,
                messages=[{"role": "user", "content": "连续裸文本"}],
                collector=collector,
            )
        )
    collector.mark_finished(status="error", error=excinfo.value)
    record = collector.build_record()

    assert len(provider.completion_calls) == 2, "必须恰好消耗配置轮次，无隐藏纠错轮"
    assert "未调用任何工具" in str(excinfo.value)
    second_messages = provider.completion_calls[1]["messages"]
    assert not any(
        message.get("role") == "assistant" for message in second_messages
    ), "裸文本轮不得作为 assistant 消息进入下一轮"
    assert record["budget"]["rounds_used"] == 2
    assert record["budget"]["tool_calls_used"] == 0
    assert record["errors"], "轮次耗尽协议失败必须进入诊断错误列表"
