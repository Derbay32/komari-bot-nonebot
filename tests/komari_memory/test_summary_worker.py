"""KomariMemory 总结业务处理器 L2 测试（TSK-151）。

被测 interface：komari_bot/plugins/komari_memory/handlers/summary_worker.py 的
ConversationSummaryProcessor / SummaryCollectorProvider（经 TDD 红绿流程落地，本文件
先行于实现提交）。

- 用例清单：docs/research/2026-08-15-processing-lifecycle-test-plan.md §4；
- 行为裁判：docs/research/2026-08-14-summary-processing-lifecycle-facts.md
  F17-F23 业务半边与 F25 观测形态；
- 业务 seam：直接构造 ProcessingSession（dict 内存账本 + 假 collector）喂
  ConversationSummaryProcessor.process()，不搭 claim/心跳环境（那是 L1 的职责，
  编排语义不在本文件复制）；
- 断言只落在业务 interface 可观察结果：账本字段读写（dict fake 内容）、
  LLM/agent 调用与否与入参、store_conversation 入参与次数、异常类型、
  collector 透传；不出现 owner token / processing 动词 / 心跳痕迹；
- 零真实时钟；消息用 MessageSchema 直接构造；pytest asyncio_mode = "auto"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

import pytest
from apscheduler.jobstores.base import JobLookupError

from komari_bot.plugins.komari_memory.config_schema import KomariMemoryConfigSchema
from komari_bot.plugins.komari_memory.handlers import (
    summary_worker as summary_worker_module,
)
from komari_bot.plugins.komari_memory.handlers.summary_worker import (
    ConversationSummaryProcessor,
    IncompleteProfileAgentError,
    InvalidSummaryResultError,
    SummaryCollectorProvider,
)
from komari_bot.plugins.komari_memory.services.conversation_processing_lifecycle import (
    InvalidChunkLedgerError,
    ProcessingSession,
)
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema


def _make_message(
    *,
    content: str = "今天一起吃拉面吧",
    user_id: str = "10001",
    user_nickname: str = "阿明",
    group_id: str = "114514",
    is_bot: bool = False,
    timestamp: float = 1.0,
) -> MessageSchema:
    return MessageSchema(
        user_id=user_id,
        user_nickname=user_nickname,
        group_id=group_id,
        content=content,
        timestamp=timestamp,
        message_id=f"msg-{user_id}",
        is_bot=is_bot,
    )


class DictChunkLedger:
    """ChunkLedger 协议的 dict 内存实现：manifest 调用留痕、字段可预置、异常可脚本化。"""

    def __init__(self) -> None:
        self.fields: dict[str, str] = {}
        self.manifest_calls: list[str] = []
        self.manifest_error: Exception | None = None

    async def initialize_manifest(self, manifest_json: str) -> str:
        self.manifest_calls.append(manifest_json)
        if self.manifest_error is not None:
            raise self.manifest_error
        return self.fields.setdefault("manifest", manifest_json)

    async def get(self, field: str) -> str | None:
        return self.fields.get(field)

    async def set(self, field: str, value: str) -> None:
        self.fields[field] = value


class _FakeCollector:
    """记录 set_input_data 的假 collector。"""

    def __init__(self) -> None:
        self.input_data_calls: list[object] = []

    def set_input_data(self, value: object) -> None:
        self.input_data_calls.append(value)


class _FakeRedisManager:
    """processor 业务侧只需要画像 agent 的 raw client（redis.redis）。"""

    def __init__(self) -> None:
        self.redis = object()


class _FakeMemory:
    """最小 memory fake：store_conversation 记录入参并按脚本返回 id/None/异常。"""

    def __init__(self, results: list[int | None | Exception] | None = None) -> None:
        self.results = list(results) if results is not None else []
        self.store_conversation_calls: list[dict[str, Any]] = []
        self._index = 0

    async def store_conversation(
        self,
        *,
        group_id: str,
        summary: str,
        participants: list[str],
        importance_initial: int = 3,
        dedup_key: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> int | None:
        self.store_conversation_calls.append(
            {
                "group_id": group_id,
                "summary": summary,
                "participants": participants,
                "importance_initial": importance_initial,
                "dedup_key": dedup_key,
                "start_time": start_time,
                "end_time": end_time,
            }
        )
        if self._index < len(self.results):
            entry = self.results[self._index]
            self._index += 1
            if isinstance(entry, Exception):
                raise entry
            return entry
        return len(self.store_conversation_calls)


class _FakeSummarize:
    """summarize_conversation 替身：记录调用；result 为对象或 (chunk_messages) -> object 工厂。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        chunk_messages: list[MessageSchema],
        config: KomariMemoryConfigSchema,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "chunk_messages": list(chunk_messages),
                "config": config,
                **kwargs,
            }
        )
        if callable(self._result):
            return self._result(list(chunk_messages))
        return self._result


class _FakeProfileAgent:
    """run_profile_agent 替身：记录调用；result 为对象或 (conversation_text) -> object 工厂。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if callable(self._result):
            return self._result(str(kwargs["conversation_text"]))
        return self._result


def _profile_result(
    *,
    status: str = "committed",
    changed_user_ids: set[str] | None = None,
) -> SimpleNamespace:
    changed = changed_user_ids if changed_user_ids is not None else set()
    return SimpleNamespace(
        status=status,
        committed_count=len(changed),
        changed_user_ids=changed,
    )


def _canonical(value: object) -> str:
    """canonical JSON：sort_keys、无空格、ensure_ascii=False（F19 账本值形态）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _chunk_ids(ledger: DictChunkLedger) -> list[str]:
    """从账本 manifest 读回各块的 chunk_id（按 chunk index 顺序）。"""
    manifest = json.loads(ledger.manifest_calls[-1])
    return [entry["chunk_id"] for entry in manifest["chunks"]]


def _make_session(
    *,
    group_id: str = "114514",
    processing_key: str = "pk-1",
    messages: list[MessageSchema] | None = None,
    ledger: DictChunkLedger | None = None,
    collector: _FakeCollector | None = None,
) -> ProcessingSession:
    return ProcessingSession(
        group_id=group_id,
        processing_key=processing_key,
        messages=messages if messages is not None else [_make_message()],
        collector=collector,
        ledger=ledger if ledger is not None else DictChunkLedger(),
    )


def _make_processor(
    redis: _FakeRedisManager | None = None,
    memory: _FakeMemory | None = None,
) -> ConversationSummaryProcessor:
    return ConversationSummaryProcessor(
        redis if redis is not None else _FakeRedisManager(),
        memory if memory is not None else _FakeMemory(),
    )


def _install_small_chunker(monkeypatch: pytest.MonkeyPatch) -> None:
    """调小分块预算使少量消息切出多块（阈值对 processor 不可见）。"""

    production_chunker = summary_worker_module.chunk_messages_for_memory_processing

    def _small_chunker(*args: Any, **kwargs: Any) -> object:
        return production_chunker(
            *args,
            **kwargs,
            max_utf8_bytes=520,
            max_estimated_tokens=174,
        )

    monkeypatch.setattr(
        summary_worker_module,
        "chunk_messages_for_memory_processing",
        _small_chunker,
    )


def _two_chunk_messages() -> list[MessageSchema]:
    return [
        _make_message(user_id="10001", content="第一块" + "甲" * 90),
        _make_message(user_id="10002", content="第二块" + "乙" * 90),
    ]


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 worker 命名空间内的 get_config 换成固定配置（bot_nickname 供分块与渲染）。"""

    monkeypatch.setattr(
        summary_worker_module,
        "get_config",
        lambda: KomariMemoryConfigSchema(
            bot_nickname="小鞠知花",
            summary_max_buffer_size=100,
            profile_trait_limit=20,
        ),
    )


@pytest.fixture(autouse=True)
def _llm_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeSummarize, _FakeProfileAgent]:
    """默认替身：任何处理器用例都不得触达真实 LLM/画像 agent。

    需要特定行为的用例在函数体内再次 monkeypatch 同名属性覆盖。
    """

    fake_summarize = _FakeSummarize(
        {"memories": [{"content": "默认有效摘要", "importance": 3}]}
    )
    fake_profile = _FakeProfileAgent(_profile_result())
    monkeypatch.setattr(summary_worker_module, "summarize_conversation", fake_summarize)
    monkeypatch.setattr(summary_worker_module, "run_profile_agent", fake_profile)
    return fake_summarize, fake_profile


# ---------------------------------------------------------------- L2-1 分块 + manifest


async def test_process_initializes_manifest_as_canonical_json() -> None:
    """L2-1：process() 先以 canonical JSON 初始化账本 manifest（F18 语义）。"""

    ledger = DictChunkLedger()
    session = _make_session(ledger=ledger)
    await _make_processor().process(session)

    assert len(ledger.manifest_calls) == 1
    manifest_json = ledger.manifest_calls[0]
    # canonical 形态：sort_keys + 无空格 + ensure_ascii=False
    assert manifest_json == _canonical(json.loads(manifest_json))
    manifest = json.loads(manifest_json)
    assert manifest["version"] == 1
    assert manifest["chunk_count"] == 1
    assert manifest["snapshot_fingerprint"] == (
        summary_worker_module._build_processing_snapshot_fingerprint(
            session.group_id,
            session.messages,
        )
    )
    chunk_entry = manifest["chunks"][0]
    assert chunk_entry["index"] == 0
    assert len(chunk_entry["chunk_id"]) == 64


async def test_process_propagates_ledger_manifest_mismatch(
    _llm_fakes: tuple[_FakeSummarize, _FakeProfileAgent],
) -> None:
    """L2-1：账本 initialize_manifest 抛 InvalidChunkLedgerError 时原样传播（单次尝试）。"""

    fake_summarize, fake_profile = _llm_fakes
    ledger = DictChunkLedger()
    mismatch = InvalidChunkLedgerError("manifest_mismatch")
    ledger.manifest_error = mismatch
    session = _make_session(ledger=ledger)

    with pytest.raises(InvalidChunkLedgerError) as exc_info:
        await _make_processor().process(session)

    assert exc_info.value is mismatch
    assert len(ledger.manifest_calls) == 1  # 单次尝试语义：无重试
    assert fake_summarize.calls == []  # manifest 门控先于任何 LLM 调用
    assert fake_profile.calls == []


# ------------------------------------------------------- L2-2 summary 缓存未命中


async def test_summary_cache_miss_calls_llm_and_writes_canonical_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-2：summary 未命中 → 调 LLM，规范化记忆以 canonical JSON（version:1）写入账本。"""

    fake_summarize = _FakeSummarize(
        {
            "memories": [
                {"content": "大家讨论了周末吃拉面。", "importance": 4},
                {"content": "阿明提到最近在追新番。", "importance": 3},
            ]
        }
    )
    monkeypatch.setattr(summary_worker_module, "summarize_conversation", fake_summarize)
    ledger = DictChunkLedger()
    session = _make_session(ledger=ledger)
    await _make_processor().process(session)

    assert len(fake_summarize.calls) == 1
    call = fake_summarize.calls[0]
    assert call["chunk_messages"] == session.messages
    assert call["participants"] == ["10001"]
    assert call["display_name_map"] == {"10001": "阿明"}
    assert call["collector"] is session.collector
    chunk_id = _chunk_ids(ledger)[0]
    assert ledger.fields[f"summary:{chunk_id}"] == _canonical(
        {
            "version": 1,
            "memories": [
                {"index": 0, "content": "大家讨论了周末吃拉面。", "importance": 4},
                {"index": 1, "content": "阿明提到最近在追新番。", "importance": 3},
            ],
        }
    )


# --------------------------------------------------------- L2-3 summary 缓存命中


async def test_summary_cache_hit_skips_llm_and_decodes_cached_memories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-3：summary 缓存命中 → 不调 LLM，记忆从缓存解码后继续存储。"""

    fake_summarize = _FakeSummarize(
        {"memories": [{"content": "不应被调用的摘要", "importance": 3}]}
    )
    monkeypatch.setattr(summary_worker_module, "summarize_conversation", fake_summarize)
    scratch = DictChunkLedger()
    await _make_processor().process(_make_session(ledger=scratch))
    chunk_id = _chunk_ids(scratch)[0]
    fake_summarize.calls.clear()

    ledger = DictChunkLedger()
    cached_summary = _canonical(
        {
            "version": 1,
            "memories": [{"index": 0, "content": "缓存的摘要记忆", "importance": 2}],
        }
    )
    ledger.fields[f"summary:{chunk_id}"] = cached_summary
    memory = _FakeMemory()
    session = _make_session(ledger=ledger)
    await _make_processor(memory=memory).process(session)

    assert fake_summarize.calls == []
    assert ledger.fields[f"summary:{chunk_id}"] == cached_summary  # 缓存未被重写
    assert memory.store_conversation_calls[0]["summary"] == "缓存的摘要记忆"
    assert memory.store_conversation_calls[0]["importance_initial"] == 2


# ----------------------------------------------------- L2-4 profile 缓存未命中


async def test_profile_cache_miss_runs_agent_and_writes_committed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-4：profile 未命中 → 调画像 agent；committed 状态以 canonical JSON 写入账本。"""

    fake_profile = _FakeProfileAgent(
        _profile_result(status="committed", changed_user_ids={"10002", "10001"})
    )
    monkeypatch.setattr(summary_worker_module, "run_profile_agent", fake_profile)
    ledger = DictChunkLedger()
    session = _make_session(
        ledger=ledger,
        messages=[
            _make_message(user_id="10001", user_nickname="阿明"),
            _make_message(user_id="10002", user_nickname="小红", is_bot=True),
        ],
    )
    redis = _FakeRedisManager()
    memory = _FakeMemory()
    await _make_processor(redis=redis, memory=memory).process(session)

    assert len(fake_profile.calls) == 1
    call = fake_profile.calls[0]
    assert call["redis"] is redis.redis
    assert call["memory"] is memory
    assert call["group_id"] == "114514"
    assert call["participants"] == ["10001"]
    assert call["display_name_map"] == {"10001": "阿明"}
    assert call["bot_user_ids"] == {"10002"}
    assert call["trace_id"]
    expected_text = "\n".join(
        summary_worker_module.format_message_line(
            message,
            bot_nickname=call["config"].bot_nickname,
        )
        for message in session.messages
    )
    assert call["conversation_text"] == expected_text
    chunk_id = _chunk_ids(ledger)[0]
    assert ledger.fields[f"profile:{chunk_id}"] == _canonical(
        {
            "version": 1,
            "status": "committed",
            "changed_user_ids": ["10001", "10002"],
            "committed_count": 2,
        }
    )


async def test_profile_cache_miss_nothing_to_commit_writes_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-4：nothing_to_commit 也写账本状态，且不算失败、存储循环照常。"""

    fake_profile = _FakeProfileAgent(_profile_result(status="nothing_to_commit"))
    monkeypatch.setattr(summary_worker_module, "run_profile_agent", fake_profile)
    ledger = DictChunkLedger()
    memory = _FakeMemory()
    session = _make_session(ledger=ledger)
    await _make_processor(memory=memory).process(session)

    chunk_id = _chunk_ids(ledger)[0]
    assert ledger.fields[f"profile:{chunk_id}"] == _canonical(
        {
            "version": 1,
            "status": "nothing_to_commit",
            "changed_user_ids": [],
            "committed_count": 0,
        }
    )
    assert len(memory.store_conversation_calls) == 1


async def test_profile_discarded_raises_incomplete_profile_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-4：discarded → IncompleteProfileAgentError，不写 profile 状态、不进入存储。"""

    fake_profile = _FakeProfileAgent(_profile_result(status="discarded"))
    monkeypatch.setattr(summary_worker_module, "run_profile_agent", fake_profile)
    ledger = DictChunkLedger()
    session = _make_session(ledger=ledger)

    with pytest.raises(IncompleteProfileAgentError, match="discarded"):
        await _make_processor().process(session)

    assert not any(field.startswith("profile:") for field in ledger.fields)
    assert not any(field.startswith("store:") for field in ledger.fields)


# ----------------------------------------------------------- L2-5 profile 缓存命中


async def test_profile_cache_hit_skips_agent_and_keeps_cached_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-5：profile 缓存命中 → 不调 agent，状态从缓存解码，存储照常。"""

    fake_profile = _FakeProfileAgent(
        _profile_result(status="committed", changed_user_ids={"10009"})
    )
    monkeypatch.setattr(summary_worker_module, "run_profile_agent", fake_profile)
    scratch = DictChunkLedger()
    await _make_processor().process(_make_session(ledger=scratch))
    chunk_id = _chunk_ids(scratch)[0]
    fake_profile.calls.clear()

    ledger = DictChunkLedger()
    cached_state = _canonical(
        {
            "version": 1,
            "status": "nothing_to_commit",
            "changed_user_ids": [],
            "committed_count": 0,
        }
    )
    ledger.fields[f"profile:{chunk_id}"] = cached_state
    memory = _FakeMemory()
    session = _make_session(ledger=ledger)
    await _make_processor(memory=memory).process(session)

    assert fake_profile.calls == []
    assert ledger.fields[f"profile:{chunk_id}"] == cached_state  # 缓存未被重写
    assert len(memory.store_conversation_calls) == 1


# ------------------------------------------------------------------- L2-6 store 门控


async def test_store_field_present_skips_chunk_storage_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-6：store:{chunk_id} 存在 → 跳过整块存储循环，summary/profile 阶段不受影响。"""

    _install_small_chunker(monkeypatch)
    messages = _two_chunk_messages()
    fake_summarize = _FakeSummarize(
        lambda chunk_messages: {
            "memories": [
                {
                    "content": f"{chunk_messages[0].content[:3]}的有效摘要",
                    "importance": 3,
                }
            ]
        }
    )
    monkeypatch.setattr(summary_worker_module, "summarize_conversation", fake_summarize)
    scratch = DictChunkLedger()
    await _make_processor().process(_make_session(messages=messages, ledger=scratch))
    first_chunk_id, second_chunk_id = _chunk_ids(scratch)
    fake_summarize.calls.clear()

    ledger = DictChunkLedger()
    ledger.fields[f"store:{first_chunk_id}"] = _canonical(
        {"version": 1, "status": "completed"}
    )
    memory = _FakeMemory()
    session = _make_session(messages=messages, ledger=ledger)
    await _make_processor(memory=memory).process(session)

    assert [call["summary"] for call in memory.store_conversation_calls] == [
        "第二块的有效摘要"
    ]
    assert (
        len(fake_summarize.calls) == 2
    )  # 两块都仍走 summary（store 门控不影响 LLM 阶段）
    assert ledger.fields[f"store:{second_chunk_id}"] == _canonical(
        {"version": 1, "status": "completed"}
    )


async def test_store_conversation_none_skipped_without_error() -> None:
    """L2-6：store_conversation 返回 None（重复）→ 跳过不算错误，store: 照常写。"""

    memory = _FakeMemory(results=[None])
    ledger = DictChunkLedger()
    session = _make_session(ledger=ledger)
    await _make_processor(memory=memory).process(session)

    assert len(memory.store_conversation_calls) == 1
    chunk_id = _chunk_ids(ledger)[0]
    assert ledger.fields[f"store:{chunk_id}"] == _canonical(
        {"version": 1, "status": "completed"}
    )


async def test_store_resumes_after_partial_write_skipping_stored_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-6：部分写入中断后续跑——已有 store: 的块跳过存储循环，未存的块补齐（F19）。"""

    _install_small_chunker(monkeypatch)
    messages = _two_chunk_messages()
    fake_summarize = _FakeSummarize(
        lambda chunk_messages: {
            "memories": [
                {
                    "content": f"{chunk_messages[0].content[:3]}的有效摘要",
                    "importance": 3,
                }
            ]
        }
    )
    fake_profile = _FakeProfileAgent(_profile_result())
    monkeypatch.setattr(summary_worker_module, "summarize_conversation", fake_summarize)
    monkeypatch.setattr(summary_worker_module, "run_profile_agent", fake_profile)
    ledger = DictChunkLedger()
    session = _make_session(messages=messages, ledger=ledger)

    memory_first = _FakeMemory(results=[1, RuntimeError("模拟存储中断")])
    with pytest.raises(RuntimeError, match="模拟存储中断"):
        await _make_processor(memory=memory_first).process(session)

    # 第一轮：两块 summary/profile 均已缓存（每块一次），第一块已存，第二块存储中断
    assert len(fake_summarize.calls) == 2
    assert len(fake_profile.calls) == 2
    first_chunk_id, second_chunk_id = _chunk_ids(ledger)
    assert ledger.fields[f"store:{first_chunk_id}"] == _canonical(
        {"version": 1, "status": "completed"}
    )
    assert f"store:{second_chunk_id}" not in ledger.fields

    # 第二轮：同一账本续跑——第一块 store 门控跳过，第二块补齐存储
    memory_second = _FakeMemory()
    await _make_processor(memory=memory_second).process(session)

    assert len(fake_summarize.calls) == 2  # LLM 零新增（缓存命中）
    assert len(fake_profile.calls) == 2  # agent 零新增（缓存命中）
    assert len(memory_second.store_conversation_calls) == 1
    assert memory_second.store_conversation_calls[0]["summary"] == "第二块的有效摘要"
    assert ledger.fields[f"store:{second_chunk_id}"] == _canonical(
        {"version": 1, "status": "completed"}
    )


# --------------------------------------------------- L2-7 空记忆/非法载荷


@pytest.mark.parametrize(
    "bad_payload",
    [
        "不是 dict 的载荷",
        {"memories": "不是数组"},
        {"memories": [{"content": "   "}, "非对象条目"]},
    ],
)
async def test_process_raises_invalid_summary_result_for_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
    bad_payload: object,
) -> None:
    """L2-7：非 dict 载荷 / memories 非数组 / 规范化后为空 → InvalidSummaryResultError。"""

    fake_summarize = _FakeSummarize(bad_payload)
    monkeypatch.setattr(summary_worker_module, "summarize_conversation", fake_summarize)
    ledger = DictChunkLedger()
    session = _make_session(ledger=ledger)

    with pytest.raises(InvalidSummaryResultError):
        await _make_processor().process(session)

    assert not any(
        field.startswith(("summary:", "profile:", "store:")) for field in ledger.fields
    )


# ------------------------------------------------------------ L2-8 collector 透传


async def test_collector_passed_through_with_enriched_input_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-8：session.collector 原样透传给 LLM/agent；set_input_data 以快照三元组富化。"""

    seen: dict[str, object] = {}

    async def _capture_summarize(
        chunk_messages: list[MessageSchema],
        config: KomariMemoryConfigSchema,
        **kwargs: object,
    ) -> dict[str, object]:
        del chunk_messages, config
        seen["summary_collector"] = kwargs["collector"]
        return {"memories": [{"content": "透传验证", "importance": 3}]}

    async def _capture_profile(**kwargs: object) -> object:
        seen["profile_collector"] = kwargs["collector"]
        return _profile_result()

    monkeypatch.setattr(
        summary_worker_module, "summarize_conversation", _capture_summarize
    )
    monkeypatch.setattr(summary_worker_module, "run_profile_agent", _capture_profile)
    collector = _FakeCollector()
    session = _make_session(collector=collector)
    await _make_processor().process(session)

    assert seen["summary_collector"] is collector
    assert seen["profile_collector"] is collector
    assert collector.input_data_calls == [
        {"group_id": "114514", "processing_key": "pk-1", "messages": session.messages}
    ]


async def test_process_tolerates_none_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-8 补充：collector 为 None（日志关闭时 provider 可返回 None）不得崩溃。"""

    seen: dict[str, object] = {}

    async def _capture_summarize(
        chunk_messages: list[MessageSchema],
        config: KomariMemoryConfigSchema,
        **kwargs: object,
    ) -> dict[str, object]:
        del chunk_messages, config
        seen["summary_collector"] = kwargs["collector"]
        return {"memories": [{"content": "无 collector 摘要", "importance": 3}]}

    async def _capture_profile(**kwargs: object) -> object:
        seen["profile_collector"] = kwargs["collector"]
        return _profile_result()

    monkeypatch.setattr(
        summary_worker_module, "summarize_conversation", _capture_summarize
    )
    monkeypatch.setattr(summary_worker_module, "run_profile_agent", _capture_profile)
    session = _make_session(collector=None)
    await _make_processor().process(session)

    assert seen["summary_collector"] is None
    assert seen["profile_collector"] is None


# ----------------------------------------------------------------- L2-9 存储幂等键


async def test_store_dedup_key_matches_sha256_formula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-9：单块场景 store_conversation 的 dedup_key 符合 sha256 公式（F21）。"""

    fake_summarize = _FakeSummarize(
        {
            "memories": [
                {"content": "记忆 A", "importance": 3},
                {"content": "记忆 B", "importance": 3},
            ]
        }
    )
    monkeypatch.setattr(summary_worker_module, "summarize_conversation", fake_summarize)
    session = _make_session()
    fingerprint = summary_worker_module._build_processing_snapshot_fingerprint(
        session.group_id,
        session.messages,
    )
    memory = _FakeMemory()
    await _make_processor(memory=memory).process(session)

    assert [call["dedup_key"] for call in memory.store_conversation_calls] == [
        sha256(f"summary:114514:{fingerprint}:0".encode()).hexdigest(),
        sha256(f"summary:114514:{fingerprint}:1".encode()).hexdigest(),
    ]


async def test_store_dedup_key_uses_chunk_index_prefix_when_chunked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-9：分块时非首块 dedup_key 的 index 为 {chunk_index}:{index}（F21）。"""

    _install_small_chunker(monkeypatch)
    messages = _two_chunk_messages()
    fake_summarize = _FakeSummarize(
        lambda chunk_messages: {
            "memories": [
                {
                    "content": f"{chunk_messages[0].content[:3]}的有效摘要",
                    "importance": 3,
                }
            ]
        }
    )
    monkeypatch.setattr(summary_worker_module, "summarize_conversation", fake_summarize)
    session = _make_session(messages=messages)
    fingerprint = summary_worker_module._build_processing_snapshot_fingerprint(
        session.group_id,
        session.messages,
    )
    memory = _FakeMemory()
    await _make_processor(memory=memory).process(session)

    assert [call["dedup_key"] for call in memory.store_conversation_calls] == [
        # 首块（chunk_index=0）index 无前缀；第二块 index 为 {chunk_index}:{index}
        sha256(f"summary:114514:{fingerprint}:0".encode()).hexdigest(),
        sha256(f"summary:114514:{fingerprint}:1:0".encode()).hexdigest(),
    ]


# ----------------------------------------------------------- L2-10 规范化边界


def test_normalize_summary_memories_clamps_importance() -> None:
    """L2-10：importance 钳制到 1-5。"""

    memories = summary_worker_module._normalize_summary_memories(
        {
            "memories": [
                {"content": "低重要性", "importance": 0},
                {"content": "高重要性", "importance": 9},
            ]
        }
    )
    assert memories == [(0, "低重要性", 1), (1, "高重要性", 5)]


def test_normalize_summary_memories_falls_back_to_3_for_invalid_importance() -> None:
    """L2-10：importance 缺失或不可解析时回退 3。"""

    memories = summary_worker_module._normalize_summary_memories(
        {
            "memories": [
                {"content": "缺省重要性"},
                {"content": "非法数值", "importance": "abc"},
                {"content": "类型非法", "importance": None},
            ]
        }
    )
    assert memories == [(0, "缺省重要性", 3), (1, "非法数值", 3), (2, "类型非法", 3)]


def test_normalize_summary_memories_skips_empty_and_non_object_entries() -> None:
    """L2-10：空正文条目与非对象条目跳过，剩余条目保序。"""

    memories = summary_worker_module._normalize_summary_memories(
        {
            "memories": [
                {"content": "   "},
                "非对象条目",
                {"content": ""},
                {"content": "有效条目", "importance": 4},
            ]
        }
    )
    assert memories == [(3, "有效条目", 4)]


# -------------------------------------------------- L2-11 provider 装配壳（F25）


class _RecordingAgentRunLoggerPlugin:
    """记录 create_collector/finalize_collector 入参的 agent_run_logger 插件替身。"""

    def __init__(self) -> None:
        self.collector = _FakeCollector()
        self.create_collector_calls: list[dict[str, Any]] = []
        self.finalize_collector_calls: list[dict[str, Any]] = []

    def create_collector(self, **kwargs: Any) -> _FakeCollector:
        self.create_collector_calls.append(kwargs)
        return self.collector

    async def finalize_collector(self, collector: object, **kwargs: Any) -> bool:
        del collector
        self.finalize_collector_calls.append(kwargs)
        return True


@pytest.fixture
def provider_with_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SummaryCollectorProvider, _RecordingAgentRunLoggerPlugin]:
    """装配壳侧：provider 实例 + 记录型 agent_run_logger 插件替身。"""

    plugin = _RecordingAgentRunLoggerPlugin()
    monkeypatch.setattr(summary_worker_module, "agent_run_logger", plugin)
    return SummaryCollectorProvider(), plugin


async def test_provider_create_calls_create_collector_with_frozen_args(
    provider_with_plugin: tuple[
        SummaryCollectorProvider, _RecordingAgentRunLoggerPlugin
    ],
) -> None:
    """L2-11：create 以固定参数调 create_collector（run_type/task_kind/trace_id/input_data）。"""

    summary_provider, plugin = provider_with_plugin

    collector = summary_provider.create("g1", "pk-1")

    assert collector is plugin.collector
    assert len(plugin.create_collector_calls) == 1
    call = plugin.create_collector_calls[0]
    assert call["run_type"] == "scheduled_summary"
    assert call["task_kind"] == "conversation_processing"
    assert call["trace_id"] == "conversation-summary-pk-1"
    assert call["input_data"] == {"group_id": "g1", "processing_key": "pk-1"}


async def test_provider_finalize_success_includes_ack_output(
    provider_with_plugin: tuple[
        SummaryCollectorProvider, _RecordingAgentRunLoggerPlugin
    ],
) -> None:
    """L2-11：success finalize 带 output（group_id/processing_key/acknowledged）且 skip_if_no_calls。"""

    summary_provider, plugin = provider_with_plugin
    collector = summary_provider.create("g1", "pk-1")

    result = await summary_provider.finalize(collector, status="success")

    assert result is True
    assert len(plugin.finalize_collector_calls) == 1
    call = plugin.finalize_collector_calls[0]
    assert call["status"] == "success"
    assert call["output"] == {
        "group_id": "g1",
        "processing_key": "pk-1",
        "acknowledged": True,
    }
    assert call["skip_if_no_calls"] is True


async def test_provider_finalize_error_and_cancelled_pass_error_without_output(
    provider_with_plugin: tuple[
        SummaryCollectorProvider, _RecordingAgentRunLoggerPlugin
    ],
) -> None:
    """L2-11：error/cancelled finalize 带 error、无 output，同样 skip_if_no_calls。"""

    summary_provider, plugin = provider_with_plugin
    collector = summary_provider.create("g1", "pk-1")
    error = RuntimeError("处理失败")

    for status in ("error", "cancelled"):
        plugin.finalize_collector_calls.clear()
        result = await summary_provider.finalize(collector, status=status, error=error)
        assert result is True
        assert len(plugin.finalize_collector_calls) == 1
        call = plugin.finalize_collector_calls[0]
        assert call["status"] == status
        assert call["error"] is error
        assert call.get("output") is None
        assert call["skip_if_no_calls"] is True


# -------------------------------------------------------------- L2-12 调度注册


class _FakeScheduler:
    """记录 add_job/remove_job 的假 scheduler；remove_job 可脚本化异常。"""

    def __init__(self) -> None:
        self.add_job_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.remove_job_calls: list[str] = []
        self.remove_error: Exception | None = None

    def add_job(self, *args: object, **kwargs: object) -> None:
        self.add_job_calls.append((args, kwargs))

    def remove_job(self, job_id: str) -> None:
        self.remove_job_calls.append(job_id)
        if self.remove_error is not None:
            raise self.remove_error


def test_register_summary_task_adds_interval_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-12：register_summary_task 注册 5 分钟 interval 任务且 replace_existing。"""

    fake_scheduler = _FakeScheduler()
    monkeypatch.setattr(summary_worker_module, "scheduler", fake_scheduler)
    redis: Any = _FakeRedisManager()
    memory: Any = _FakeMemory()

    summary_worker_module.register_summary_task(redis, memory)

    assert len(fake_scheduler.add_job_calls) == 1
    args, kwargs = fake_scheduler.add_job_calls[0]
    assert callable(args[0])  # 任务函数
    trigger = args[1] if len(args) > 1 else kwargs.get("trigger")
    assert trigger == "interval"
    assert kwargs["minutes"] == 5
    assert kwargs["id"] == "komari_memory_summary_worker"
    assert kwargs["replace_existing"] is True
    assert kwargs["args"] == [redis, memory]


def test_unregister_summary_task_removes_job(monkeypatch: pytest.MonkeyPatch) -> None:
    """L2-12：unregister_summary_task 移除固定 id 的任务。"""

    fake_scheduler = _FakeScheduler()
    monkeypatch.setattr(summary_worker_module, "scheduler", fake_scheduler)

    summary_worker_module.unregister_summary_task()

    assert fake_scheduler.remove_job_calls == ["komari_memory_summary_worker"]


def test_unregister_summary_task_tolerates_scheduler_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2-12：remove_job 抛 JobLookupError 或任意异常都不上抛（语义不变）。"""

    for error in (JobLookupError("不存在"), RuntimeError("调度器故障")):
        fake_scheduler = _FakeScheduler()
        fake_scheduler.remove_error = error
        monkeypatch.setattr(summary_worker_module, "scheduler", fake_scheduler)

        summary_worker_module.unregister_summary_task()  # 不抛

        assert fake_scheduler.remove_job_calls == ["komari_memory_summary_worker"]


# ------------------------------------------------ 移植保留的旧 helper 级测试


def test_snapshot_fingerprint_stable_and_dedup_key_deterministic() -> None:
    """移植：快照指纹稳定、dedup 键随 index/chunk 区分（旧 test_summary_dedup_key_*）。"""

    messages = [_make_message(), _make_message(content="晚上看新番", user_id="10002")]
    first = summary_worker_module._build_processing_snapshot_fingerprint(
        "114514",
        messages,
    )
    second = summary_worker_module._build_processing_snapshot_fingerprint(
        "114514",
        list(messages),
    )
    assert first == second
    assert len(first) == 64
    key = summary_worker_module._build_summary_dedup_key("114514", first, 0)
    assert key == summary_worker_module._build_summary_dedup_key("114514", second, 0)
    assert key != summary_worker_module._build_summary_dedup_key("114514", first, 1)
    assert key != summary_worker_module._build_summary_dedup_key(
        "114514",
        first,
        0,
        chunk_index=1,
    )


def test_resolve_message_time_range_uses_real_timestamps() -> None:
    """移植：store 时间范围取真实消息时间戳的 min/max，非法时间戳跳过。"""

    start = 1_700_000_000.0
    end = 1_700_003_600.0
    messages = [
        _make_message(timestamp=end, user_id="10001"),
        _make_message(timestamp=start, user_id="10002"),
        _make_message(timestamp=float("nan"), user_id="10003"),
        _make_message(timestamp=-5.0, user_id="10004"),
    ]
    low, high = summary_worker_module._resolve_message_time_range(messages)
    assert low == datetime.fromtimestamp(start, UTC).replace(tzinfo=None)
    assert high == datetime.fromtimestamp(end, UTC).replace(tzinfo=None)
