"""对话 processing 生命周期 module（TSK-150）L1 验收测试。

被测 module：komari_bot/plugins/komari_memory/services/conversation_processing_lifecycle.py
（经 TDD 红绿流程落地，本文件先行于实现提交）。

- fake 形态与用例清单：docs/research/2026-08-15-processing-lifecycle-test-plan.md §1-§3；
- 行为裁判：docs/research/2026-08-14-summary-processing-lifecycle-facts.md F1-F25；
- 所有断言只落在 interface 可观察结果（返回值、异常类型、fake 边界记录）；
- 零真实时钟：asyncio.sleep 全局替换 + 脚本化 wait_for 驱动心跳，整套数秒内跑完。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

from komari_bot.plugins.komari_memory.services import (
    conversation_processing_lifecycle as lifecycle_module,
)
from komari_bot.plugins.komari_memory.services.conversation_processing import (
    ConversationLeaseLostError,
    ConversationSnapshotClaim,
)
from komari_bot.plugins.komari_memory.services.conversation_processing_lifecycle import (
    ConversationProcessingLifecycle,
    InvalidChunkLedgerError,
)
from komari_bot.plugins.komari_memory.services.redis_manager import MessageSchema

_VALID_MESSAGE: dict[str, object] = {
    "user_id": "10001",
    "user_nickname": "阿明",
    "group_id": "g1",
    "content": "今天一起吃拉面吧",
    "timestamp": 1.0,
    "message_id": "m1",
}


class _ChunkLedgerView(Protocol):
    """module 的 ChunkLedger 协议在测试侧的结构视图（运行时不检查）。"""

    async def initialize_manifest(self, manifest_json: str) -> str: ...

    async def get(self, field: str) -> str | None: ...

    async def set(self, field: str, value: str) -> None: ...


class _SessionView(Protocol):
    """module 的 ProcessingSession 在测试侧的结构视图（运行时不检查）。"""

    group_id: str
    processing_key: str
    messages: list[MessageSchema]
    collector: FakeCollector | None
    ledger: _ChunkLedgerView


class FakeProcessingStorage:
    """私有 storage 协议的内存 fake：14 个动词，行为可脚本化。

    - claim 三态可脚本化（ConversationSnapshotClaim 或 Exception 条目）；
    - renew 接受「返回 bool / 异常」序列，耗尽后默认返回 True；
    - ack/restore/dead-letter 记录入参；账本为 dict；
    - 孤儿键、活跃群、should_trigger_summary 可注入；
    - 不复刻 Lua 语义（那是动词层 L3 测试的职责）；
    - config.conversation_processing_lease_seconds 与 RedisManager 同形，
      保证 module 可零适配直接注入真实 RedisManager。
    """

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events
        self.config = SimpleNamespace(conversation_processing_lease_seconds=120)
        self.claim_script: list[ConversationSnapshotClaim | Exception] = []
        self.claim_existing_script: list[ConversationSnapshotClaim | Exception] = []
        self.claim_conversation_buffer_calls: list[dict[str, str]] = []
        self.claim_existing_calls: list[dict[str, str]] = []
        self.buffer_items: list[object] = []
        self.get_calls: list[dict[str, str]] = []
        self.get_error: Exception | None = None
        self.renew_script: list[object] = []
        self.renew_calls: list[dict[str, str]] = []
        self.stop_event: asyncio.Event | None = None
        self.set_stop_after_renew: int | None = None
        self.ack_calls: list[dict[str, str]] = []
        self.ack_result: bool = True
        self.ack_error: Exception | None = None
        self.restore_calls: list[dict[str, str]] = []
        self.restore_result: bool = True
        self.restore_error: Exception | None = None
        self.dead_letter_calls: list[dict[str, object]] = []
        self.dead_letter_result: bool = True
        self.dead_letter_error: Exception | None = None
        self.chunk_ledger: dict[str, str] = {}
        self.ledger_calls: list[dict[str, str]] = []
        self.ledger_error: Exception | None = None
        self.orphaned_keys: list[tuple[str, str]] = []
        self.orphan_scan_error: Exception | None = None
        self.active_groups: list[str] = []
        self.should_trigger_results: dict[str, bool] = {}
        self.should_trigger_errors: dict[str, Exception] = {}
        self.update_last_summary_calls: list[str] = []
        self._renew_index = 0

    def _mark(self, name: str) -> None:
        if self.events is not None:
            self.events.append(name)

    async def claim_conversation_buffer(
        self,
        group_id: str,
        owner_token: str,
        token: str,
    ) -> ConversationSnapshotClaim:
        self.claim_conversation_buffer_calls.append(
            {"group_id": group_id, "owner_token": owner_token, "token": token}
        )
        self._mark("claim_conversation_buffer")
        if self.claim_script:
            entry: ConversationSnapshotClaim | Exception = self.claim_script.pop(0)
            if isinstance(entry, Exception):
                raise entry
            return entry
        return ConversationSnapshotClaim(
            status="claimed",
            processing_key=f"komari_memory:buffer:processing:{group_id}:{token}",
        )

    async def claim_existing_conversation_processing(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> ConversationSnapshotClaim:
        self.claim_existing_calls.append(
            {
                "group_id": group_id,
                "processing_key": processing_key,
                "owner_token": owner_token,
            }
        )
        self._mark("claim_existing_conversation_processing")
        if self.claim_existing_script:
            entry = self.claim_existing_script.pop(0)
            if isinstance(entry, Exception):
                raise entry
            return entry
        return ConversationSnapshotClaim(status="claimed", processing_key=processing_key)

    async def get_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> list[MessageSchema]:
        self.get_calls.append(
            {"group_id": group_id, "processing_key": processing_key, "owner_token": owner_token}
        )
        self._mark("get")
        if self.get_error is not None:
            raise self.get_error
        messages: list[MessageSchema] = []
        for item in self.buffer_items:
            try:
                messages.append(self._deserialize_item(item))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return messages

    def _deserialize_item(self, item: object) -> MessageSchema:
        """dict 或 JSON 字符串解析为 MessageSchema；坏数据抛异常由调用方跳过。"""
        if isinstance(item, MessageSchema):
            return item
        payload = json.loads(item) if isinstance(item, str) else item
        if not isinstance(payload, dict):
            raise TypeError("消息项不是对象")
        return MessageSchema(
            user_id=str(payload["user_id"]),
            user_nickname=str(payload.get("user_nickname", "")),
            group_id=str(payload.get("group_id", "")),
            content=str(payload["content"]),
            timestamp=float(payload["timestamp"]),
            message_id=str(payload["message_id"]),
            is_bot=bool(payload.get("is_bot", False)),
        )

    async def renew_processing_conversation_lease(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        self.renew_calls.append(
            {"group_id": group_id, "processing_key": processing_key, "owner_token": owner_token}
        )
        self._mark("renew")
        entry: object = (
            self.renew_script[self._renew_index]
            if self._renew_index < len(self.renew_script)
            else True
        )
        self._renew_index += 1
        if (
            self.set_stop_after_renew is not None
            and self._renew_index >= self.set_stop_after_renew
            and self.stop_event is not None
        ):
            self.stop_event.set()
        if isinstance(entry, Exception):
            raise entry
        return bool(entry)

    async def ack_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        self.ack_calls.append(
            {"group_id": group_id, "processing_key": processing_key, "owner_token": owner_token}
        )
        self._mark("ack")
        if self.ack_error is not None:
            raise self.ack_error
        return self.ack_result

    async def restore_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
    ) -> bool:
        self.restore_calls.append(
            {"group_id": group_id, "processing_key": processing_key, "owner_token": owner_token}
        )
        self._mark("restore")
        if self.restore_error is not None:
            raise self.restore_error
        return self.restore_result

    async def dead_letter_processing_conversation_buffer(
        self,
        group_id: str,
        processing_key: str,
        owner_token: str,
        *,
        failure_code: str,
        attempt_count: int,
    ) -> bool:
        self.dead_letter_calls.append(
            {
                "group_id": group_id,
                "processing_key": processing_key,
                "owner_token": owner_token,
                "failure_code": failure_code,
                "attempt_count": attempt_count,
            }
        )
        self._mark("dead_letter")
        if self.dead_letter_error is not None:
            raise self.dead_letter_error
        return self.dead_letter_result

    async def initialize_conversation_chunk_manifest(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        manifest_json: str,
    ) -> str:
        del group_id, processing_key, owner_token
        self.ledger_calls.append({"operation": "initialize_manifest", "field": "manifest"})
        if self.ledger_error is not None:
            raise self.ledger_error
        return self.chunk_ledger.setdefault("manifest", manifest_json)

    async def get_conversation_chunk_state(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        field: str,
    ) -> str | None:
        del group_id, processing_key, owner_token
        self.ledger_calls.append({"operation": "get", "field": field})
        if self.ledger_error is not None:
            raise self.ledger_error
        return self.chunk_ledger.get(field)

    async def set_conversation_chunk_state(
        self,
        *,
        group_id: str,
        processing_key: str,
        owner_token: str,
        field: str,
        value: str,
    ) -> None:
        del group_id, processing_key, owner_token
        self.ledger_calls.append({"operation": "set", "field": field})
        if self.ledger_error is not None:
            raise self.ledger_error
        self.chunk_ledger[field] = value

    async def get_orphaned_conversation_processing_keys(self) -> list[tuple[str, str]]:
        self._mark("orphan_scan")
        if self.orphan_scan_error is not None:
            raise self.orphan_scan_error
        return list(self.orphaned_keys)

    async def get_active_groups(self) -> list[str]:
        return list(self.active_groups)

    async def should_trigger_summary(self, group_id: str) -> bool:
        if group_id in self.should_trigger_errors:
            raise self.should_trigger_errors[group_id]
        return self.should_trigger_results.get(group_id, False)

    async def update_last_summary(self, group_id: str) -> None:
        self.update_last_summary_calls.append(group_id)
        self._mark("update_last_summary")


class FakeCollector:
    """记录型假 collector：set_input_data 留痕。"""

    def __init__(self) -> None:
        self.input_data_calls: list[object] = []

    def set_input_data(self, value: object) -> None:
        self.input_data_calls.append(value)


class FakeCollectorProvider:
    """CollectorProvider 假实现：记录 create/finalize 调用序列。"""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events
        self.create_calls: list[tuple[str, str]] = []
        self.finalize_calls: list[tuple[str, BaseException | None]] = []
        self.collector = FakeCollector()

    def create(self, group_id: str, processing_key: str) -> FakeCollector | None:
        self.create_calls.append((group_id, processing_key))
        if self.events is not None:
            self.events.append("create")
        return self.collector

    async def finalize(
        self,
        collector: FakeCollector | None,
        *,
        status: str,
        error: BaseException | None = None,
    ) -> bool:
        del collector
        self.finalize_calls.append((status, error))
        if self.events is not None:
            self.events.append(f"finalize:{status}")
        return True


class RecordingProcessor:
    """记录收到的 session；process 立即成功。"""

    def __init__(self) -> None:
        self.process_calls: list[_SessionView] = []

    async def process(self, session: _SessionView) -> None:
        self.process_calls.append(session)


class _BlockingProcessor:
    """process 挂起直到被取消；记录取消事实。"""

    def __init__(self, started: asyncio.Event) -> None:
        self.started = started
        self.cancelled = False
        self._release = asyncio.Event()

    async def process(self, session: _SessionView) -> None:
        del session
        if self.started is not None:
            self.started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _AlwaysFailProcessor:
    """每次调用都抛指定异常；统计调用次数（重试恰好 3 次的裁判）。"""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.process_calls = 0

    async def process(self, session: _SessionView) -> None:
        del session
        self.process_calls += 1
        raise self.error


class _ScriptedErrorsProcessor:
    """按脚本序列依次抛异常，耗尽后重复最后一个；重试停止时机的裁判（TSK-155）。

    钉「lease-lost 中途出现立即停止重试」时第 3 次调用绝不发生；若误发生，
    抛出的仍是脚本最后一个异常实例，脚本不因越界改变异常类型。
    """

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        self.process_calls = 0

    async def process(self, session: _SessionView) -> None:
        del session
        self.process_calls += 1
        last_index = min(self.process_calls, len(self.errors)) - 1
        raise self.errors[last_index]


class _ManifestMismatchProcessor:
    """经 ledger.initialize_manifest 读回 manifest 并逐字节比对；不一致抛 manifest_mismatch。"""

    def __init__(self, manifest_json: str) -> None:
        self.manifest_json = manifest_json
        self.process_calls = 0

    async def process(self, session: _SessionView) -> None:
        self.process_calls += 1
        stored = await session.ledger.initialize_manifest(self.manifest_json)
        if stored != self.manifest_json:
            raise InvalidChunkLedgerError("manifest_mismatch")


class _LedgerErrorProcessor:
    """调用账本 set 动词；storage 侧 owner 失效时抛 ConversationLeaseLostError。"""

    def __init__(self) -> None:
        self.process_calls = 0

    async def process(self, session: _SessionView) -> None:
        self.process_calls += 1
        await session.ledger.set("summary:c1", "{}")


class _ScriptedWaitFor:
    """替代 asyncio.wait_for：前 N 次立即抛 TimeoutError，之后直接等待底层协程。

    心跳循环靠 TimeoutError 推进续租、stop 置位即退出（F10 冻结语义）；
    该 fake 让续租推进零真实时钟，超时路径显式 close 被丢弃的 stop.wait() 协程。
    """

    def __init__(self, timeout_count: int) -> None:
        self._remaining = timeout_count

    async def __call__(
        self, awaitable: Coroutine[Any, Any, bool], timeout: float  # noqa: ASYNC109 — 参数名须对齐 asyncio.wait_for(timeout=) 关键字调用
    ) -> bool:
        del timeout
        if self._remaining > 0:
            self._remaining -= 1
            awaitable.close()
            raise TimeoutError
        return await awaitable


@pytest.fixture(autouse=True)
def _noop_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """消除 retry_async 1s/2s 退避等所有真实等待——测试必须零真实时钟。"""

    async def _noop(_delay: float) -> None:
        """不等待。"""

    monkeypatch.setattr(asyncio, "sleep", _noop)


@pytest.fixture(autouse=True)
def _heartbeat_wait_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认把 asyncio.wait_for 替换为前 2 次立即超时的脚本化驱动。

    心跳循环每轮一次 wait_for（F10）；默认 2 次超时意味着心跳恰好续租 2 次后
    阻塞在 stop.wait()，显式门控续租成为第 3 次——所有 L1 用例的续租序列由此确定。
    """
    monkeypatch.setattr(asyncio, "wait_for", _ScriptedWaitFor(2))


@pytest.fixture
def install_wait_for(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """覆盖默认心跳驱动：指定前 N 次立即超时。"""

    def _install(count: int) -> None:
        monkeypatch.setattr(asyncio, "wait_for", _ScriptedWaitFor(count))

    return _install


@pytest.fixture
def fake_storage() -> FakeProcessingStorage:
    return FakeProcessingStorage()


@pytest.fixture
def fake_provider() -> FakeCollectorProvider:
    return FakeCollectorProvider()


@pytest.fixture
def lifecycle(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
) -> ConversationProcessingLifecycle:
    return ConversationProcessingLifecycle(fake_storage, fake_provider)


class _LoggerLogs:
    """logger.exception / logger.warning 双形态留痕容器（TSK-167）。"""

    def __init__(self) -> None:
        self.exception: list[tuple[object, tuple[object, ...]]] = []
        self.warning: list[tuple[object, tuple[object, ...]]] = []


@pytest.fixture
def logger_logs(monkeypatch: pytest.MonkeyPatch) -> _LoggerLogs:
    """logger.exception / logger.warning 双形态留痕（TSK-167）。"""

    logs = _LoggerLogs()
    monkeypatch.setattr(
        lifecycle_module.logger,
        "exception",
        lambda message, *args: logs.exception.append((message, args)),
    )
    monkeypatch.setattr(
        lifecycle_module.logger,
        "warning",
        lambda message, *args: logs.warning.append((message, args)),
    )
    return logs


def _cleanup_warnings(
    logs: _LoggerLogs,
) -> list[tuple[object, tuple[object, ...]]]:
    """仅取 cleanup（dead-letter/快照恢复）相关 warning，排除 retry_async 的重试 warning。"""

    return [
        entry
        for entry in logs.warning
        if "dead-letter" in str(entry[0]) or "快照恢复" in str(entry[0])
    ]


def _assert_exception_log(
    entry: tuple[object, tuple[object, ...]],
    message_fragment: str,
    processing_key: object,
    error_type: str,
) -> None:
    """钉一条 logger.exception：消息片段、group/key/error_type 三槽位与位置参数。"""

    message, args = entry
    assert message_fragment in str(message)
    assert "group={}" in str(message)
    assert "key={}" in str(message)
    assert "error_type={}" in str(message)
    assert args[0] == "g1"
    assert args[1] == processing_key
    assert args[2] == error_type


def _assert_warning_log(
    entry: tuple[object, tuple[object, ...]],
    message_fragment: str,
    processing_key: object,
) -> None:
    """钉一条 logger.warning：消息片段与 group/key 两槽位（warning 无 error_type）。"""

    message, args = entry
    assert message_fragment in str(message)
    assert "group={}" in str(message)
    assert "key={}" in str(message)
    assert args[0] == "g1"
    assert args[1] == processing_key


def _assert_claim_abort(fake: FakeProcessingStorage) -> None:
    """F1 公共断言：claim 三态非 claimed 时生命周期动作全部为零。"""
    assert fake.get_calls == []
    assert fake.renew_calls == []
    assert fake.ack_calls == []
    assert fake.restore_calls == []
    assert fake.dead_letter_calls == []
    assert fake.update_last_summary_calls == []


async def test_claim_busy_returns_false_silently(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F1：busy → 返回 False，processor 未调、collector 未建、零副作用。"""
    fake_storage.claim_script = [
        ConversationSnapshotClaim(status="busy", processing_key="pk-busy")
    ]
    processor = RecordingProcessor()

    claimed = await lifecycle.process_conversation_snapshot("g1", processor)

    assert claimed is False
    assert processor.process_calls == []
    assert fake_provider.create_calls == []
    _assert_claim_abort(fake_storage)


async def test_claim_empty_returns_false_silently(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F1：empty → 返回 False，processor 未调、collector 未建、零副作用。"""
    fake_storage.claim_script = [ConversationSnapshotClaim(status="empty")]
    processor = RecordingProcessor()

    claimed = await lifecycle.process_conversation_snapshot("g1", processor)

    assert claimed is False
    assert processor.process_calls == []
    assert fake_provider.create_calls == []
    _assert_claim_abort(fake_storage)


async def test_messages_prefetched_and_bad_json_skipped(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F5：消息预读 + 坏 JSON 跳过（fake storage 供给），session.messages 直接可消费。"""
    fake_storage.buffer_items = [
        dict(_VALID_MESSAGE),
        "这不是合法 JSON {{{",
        42,
        {"user_id": "10002", "content": "缺少 timestamp 字段的消息"},
    ]
    processor = RecordingProcessor()

    claimed = await lifecycle.process_conversation_snapshot("g1", processor)

    assert claimed is True
    assert len(processor.process_calls) == 1
    session = processor.process_calls[0]
    assert isinstance(session.messages[0], MessageSchema)
    assert [message.message_id for message in session.messages] == ["m1"]
    assert len(fake_storage.get_calls) == 1


async def test_lease_lost_mid_processing_cancels_task_without_side_effects(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F8：心跳续租 False → 处理任务被取消 → ConversationLeaseLostError；零副作用。

    本用例同时证明心跳已接进生命周期（F8/F9 兼任心跳接线证明）。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.renew_script = [False]
    processor = _BlockingProcessor(started=asyncio.Event())

    with pytest.raises(ConversationLeaseLostError):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert processor.cancelled is True
    assert fake_storage.ack_calls == []
    assert fake_storage.restore_calls == []
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert len(fake_provider.finalize_calls) == 1
    assert fake_provider.finalize_calls[0][0] == "error"
    assert isinstance(fake_provider.finalize_calls[0][1], ConversationLeaseLostError)


async def test_gate_renew_false_before_ack_raises_lease_lost(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F9：门控孤立路径——心跳正常、ack 前显式 renew 返回 False → lease-lost，ack 未发出。

    心跳恰好 2 次续租（脚本化 wait_for 驱动），第 3 次续租即显式门控（F12 冻结四步）。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.renew_script = [True, True, False]
    processor = RecordingProcessor()

    with pytest.raises(ConversationLeaseLostError):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert len(processor.process_calls) == 1
    assert len(fake_storage.renew_calls) == 3
    assert fake_storage.ack_calls == []
    assert fake_storage.restore_calls == []
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.update_last_summary_calls == []


async def test_gate_renew_exception_dead_letters_with_attempt_count_one(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """TSK-164：门控续租抛非租约异常 → finalize(error) 原实例 + dead-letter 一次 → re-raise。

    心跳恰好 2 次续租（脚本化 wait_for 驱动），第 3 次续租即显式门控；续租抛出
    未经过重试包装层的 RuntimeError，attempt_count 经 get_retry_attempts 取空后
    兜底为 1；dead-letter 成功故不触发 restore 兜底；update_last_summary 仅在
    ack 成功之后执行，此路径不得调用；续租已抛故 ack 未发出。
    """
    gate_error = RuntimeError("门控续租 redis 抖动")
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.renew_script = [True, True, gate_error]
    processor = RecordingProcessor()

    with pytest.raises(RuntimeError) as exc_info:
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert exc_info.value is gate_error
    assert len(processor.process_calls) == 1
    assert len(fake_storage.renew_calls) == 3
    assert fake_storage.ack_calls == []
    assert len(fake_storage.dead_letter_calls) == 1
    assert fake_storage.dead_letter_calls[0]["failure_code"] == "RuntimeError"
    assert fake_storage.dead_letter_calls[0]["attempt_count"] == 1
    assert fake_storage.restore_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert len(fake_provider.finalize_calls) == 1
    assert fake_provider.finalize_calls[0][0] == "error"
    assert fake_provider.finalize_calls[0][1] is gate_error


async def test_gate_ack_exception_dead_letters_with_attempt_count_one(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """TSK-164：门控 ack 抛非租约异常 → finalize(error) 原实例 + dead-letter 一次 → re-raise。

    与门控续租抛错用例对称：renew 全部成功、ack 抛出未经过重试包装层的
    RuntimeError，attempt_count 兜底为 1；dead-letter 成功故不触发 restore 兜底；
    update_last_summary 不得执行；ack 恰好发出 1 次。
    """
    ack_error = RuntimeError("门控 ack redis 抖动")
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.renew_script = [True, True, True]
    fake_storage.ack_error = ack_error
    processor = RecordingProcessor()

    with pytest.raises(RuntimeError) as exc_info:
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert exc_info.value is ack_error
    assert len(processor.process_calls) == 1
    assert len(fake_storage.renew_calls) == 3
    assert len(fake_storage.ack_calls) == 1
    assert len(fake_storage.dead_letter_calls) == 1
    assert fake_storage.dead_letter_calls[0]["failure_code"] == "RuntimeError"
    assert fake_storage.dead_letter_calls[0]["attempt_count"] == 1
    assert fake_storage.restore_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert len(fake_provider.finalize_calls) == 1
    assert fake_provider.finalize_calls[0][0] == "error"
    assert fake_provider.finalize_calls[0][1] is ack_error


async def test_cancel_finalizes_cancelled_first_and_restores_once(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F10/F25：取消 → finalize(cancelled) 最先 → shield(restore) 恰好一次 → re-raise。"""
    events: list[str] = []
    fake_storage.events = events
    fake_provider.events = events
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    processor = _BlockingProcessor(started=asyncio.Event())
    task = asyncio.create_task(lifecycle.process_conversation_snapshot("g1", processor))
    await processor.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert processor.cancelled is True
    assert len(fake_storage.restore_calls) == 1
    assert fake_provider.finalize_calls[0][0] == "cancelled"
    assert events.index("finalize:cancelled") < events.index("restore")
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []


async def test_cancel_with_restore_rejected_still_reraises(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
    logger_logs: _LoggerLogs,
) -> None:
    """F10 缺口补测 + TSK-167：取消后 restore 被拒（返回 False）仍 re-raise。

    被拒属正常控制流，仅 warning 记录、不产生误导性 traceback（零 exception 日志）。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.restore_result = False
    processor = _BlockingProcessor(started=asyncio.Event())
    task = asyncio.create_task(lifecycle.process_conversation_snapshot("g1", processor))
    await processor.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(fake_storage.restore_calls) == 1
    assert fake_provider.finalize_calls[0][0] == "cancelled"
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert len(logger_logs.warning) == 1
    _assert_warning_log(
        logger_logs.warning[0],
        "取消总结后的快照恢复被拒绝",
        fake_storage.restore_calls[0]["processing_key"],
    )
    assert logger_logs.exception == []


async def test_cancel_with_restore_exception_still_reraises(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
    logger_logs: _LoggerLogs,
) -> None:
    """F10 + TSK-167：取消后 restore 抛异常仍 re-raise；经 logger.exception 记录完整 traceback。

    消息模板与既有 group/key/error_type 三槽位保持不变；该路径不再使用 warning。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.restore_error = RuntimeError("恢复快照失败")
    processor = _BlockingProcessor(started=asyncio.Event())
    task = asyncio.create_task(lifecycle.process_conversation_snapshot("g1", processor))
    await processor.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(fake_storage.restore_calls) == 1
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert len(logger_logs.exception) == 1
    _assert_exception_log(
        logger_logs.exception[0],
        "取消总结后的快照恢复失败",
        fake_storage.restore_calls[0]["processing_key"],
        "RuntimeError",
    )
    assert logger_logs.warning == []


async def test_business_error_dead_letters_with_params_and_reraises(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F11/F12/F25：业务异常 → dead-letter（failure_code=类型名、attempt_count=3）→ re-raise。

    finalize(error) 必须先于 dead-letter（F25 三态时机）。
    """
    events: list[str] = []
    fake_storage.events = events
    fake_provider.events = events
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    processor = _AlwaysFailProcessor(RuntimeError("总结失败"))

    with pytest.raises(RuntimeError, match="总结失败"):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert processor.process_calls == 3
    assert len(fake_storage.dead_letter_calls) == 1
    assert fake_storage.dead_letter_calls[0]["failure_code"] == "RuntimeError"
    assert fake_storage.dead_letter_calls[0]["attempt_count"] == 3
    assert fake_storage.restore_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert events.index("finalize:error") < events.index("dead_letter")


async def test_get_lease_lost_skips_dead_letter_and_restore(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F11：预读消息时 owner 失效 → ConversationLeaseLostError 传播；无 dead-letter/restore。"""
    fake_storage.get_error = ConversationLeaseLostError("pk")
    processor = RecordingProcessor()

    with pytest.raises(ConversationLeaseLostError):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert processor.process_calls == []
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.restore_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []


async def test_dead_letter_failure_falls_back_to_restore(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
    logger_logs: _LoggerLogs,
) -> None:
    """F13 + TSK-167：dead-letter 抛异常 → logger.exception 记录 → restore 兜底 → 仍 re-raise。

    异常路径只走 exception 形态；restore 成功不产生额外 cleanup warning。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.dead_letter_error = RuntimeError("dead-letter 失败")
    processor = _AlwaysFailProcessor(RuntimeError("总结失败"))

    with pytest.raises(RuntimeError, match="总结失败"):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert len(fake_storage.dead_letter_calls) == 1
    assert len(fake_storage.restore_calls) == 1
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert len(logger_logs.exception) == 1
    _assert_exception_log(
        logger_logs.exception[0],
        "对话快照移入 dead-letter 失败",
        fake_storage.dead_letter_calls[0]["processing_key"],
        "RuntimeError",
    )
    assert _cleanup_warnings(logger_logs) == []


async def test_dead_letter_rejected_falls_back_to_restore(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
    logger_logs: _LoggerLogs,
) -> None:
    """F13 + TSK-167：dead-letter 返回 False → restore 兜底 → 仍 re-raise。

    纯控制流拒绝不产生 cleanup 日志（零 exception / 零 cleanup warning），避免误导性 traceback。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.dead_letter_result = False
    processor = _AlwaysFailProcessor(RuntimeError("总结失败"))

    with pytest.raises(RuntimeError, match="总结失败"):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert len(fake_storage.dead_letter_calls) == 1
    assert len(fake_storage.restore_calls) == 1
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert logger_logs.exception == []
    assert _cleanup_warnings(logger_logs) == []


async def test_dead_letter_and_fallback_restore_exceptions_log_both_via_exception(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
    logger_logs: _LoggerLogs,
) -> None:
    """TSK-167：dead-letter 与兜底 restore 双重失败 → 两处均 exception 形态 → 仍 re-raise。

    第一跳钉 dead-letter 失败日志，第二跳钉兜底恢复失败日志；消息模板与既有
    group/key/error_type 三槽位不变；最终重新抛出原业务异常。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.dead_letter_error = RuntimeError("dead-letter 失败")
    fake_storage.restore_error = ConnectionError("兜底恢复失败")
    processor = _AlwaysFailProcessor(RuntimeError("总结失败"))

    with pytest.raises(RuntimeError, match="总结失败"):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert len(fake_storage.dead_letter_calls) == 1
    assert len(fake_storage.restore_calls) == 1
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert len(logger_logs.exception) == 2
    _assert_exception_log(
        logger_logs.exception[0],
        "对话快照移入 dead-letter 失败",
        fake_storage.dead_letter_calls[0]["processing_key"],
        "RuntimeError",
    )
    _assert_exception_log(
        logger_logs.exception[1],
        "dead-letter 失败后的快照恢复失败",
        fake_storage.restore_calls[0]["processing_key"],
        "ConnectionError",
    )
    assert _cleanup_warnings(logger_logs) == []


async def test_dead_letter_exception_fallback_restore_rejected_warns(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
    logger_logs: _LoggerLogs,
) -> None:
    """TSK-167：dead-letter 抛异常后兜底 restore 返回 False → 恢复被拒仍 warning。

    dead-letter 异常本身走 exception 形态；被拒属控制流，warning 无 traceback。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.dead_letter_error = RuntimeError("dead-letter 失败")
    fake_storage.restore_result = False
    processor = _AlwaysFailProcessor(RuntimeError("总结失败"))

    with pytest.raises(RuntimeError, match="总结失败"):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert len(fake_storage.dead_letter_calls) == 1
    assert len(fake_storage.restore_calls) == 1
    assert len(logger_logs.exception) == 1
    _assert_exception_log(
        logger_logs.exception[0],
        "对话快照移入 dead-letter 失败",
        fake_storage.dead_letter_calls[0]["processing_key"],
        "RuntimeError",
    )
    cleanup_warnings = _cleanup_warnings(logger_logs)
    assert len(cleanup_warnings) == 1
    _assert_warning_log(
        cleanup_warnings[0],
        "dead-letter 失败后的快照恢复被拒绝",
        fake_storage.restore_calls[0]["processing_key"],
    )


async def test_dead_letter_rejected_fallback_restore_rejected_warns(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
    logger_logs: _LoggerLogs,
) -> None:
    """TSK-167：dead-letter 返回 False 后兜底 restore 亦被拒 → 仅 warning，零 exception。"""
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.dead_letter_result = False
    fake_storage.restore_result = False
    processor = _AlwaysFailProcessor(RuntimeError("总结失败"))

    with pytest.raises(RuntimeError, match="总结失败"):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert len(fake_storage.dead_letter_calls) == 1
    assert len(fake_storage.restore_calls) == 1
    assert logger_logs.exception == []
    cleanup_warnings = _cleanup_warnings(logger_logs)
    assert len(cleanup_warnings) == 1
    _assert_warning_log(
        cleanup_warnings[0],
        "dead-letter 失败后的快照恢复被拒绝",
        fake_storage.restore_calls[0]["processing_key"],
    )


async def test_lease_lost_from_processor_diverts_without_retry(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """TSK-155：ConversationLeaseLostError 首次出现即分流，不再重试。

    process 第一次调用即抛 lease-lost → 恰好 1 次调用；finalize(error) 以该异常收尾；
    dead-letter / restore 零调用（F11 零副作用语义不变）；原异常实例原样抛出。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    lease_lost_error = ConversationLeaseLostError("pk")
    processor = _AlwaysFailProcessor(lease_lost_error)

    with pytest.raises(ConversationLeaseLostError) as exc_info:
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert exc_info.value is lease_lost_error  # 原异常实例原样抛出
    assert processor.process_calls == 1  # 首次即分流，零重试
    assert len(fake_provider.finalize_calls) == 1
    assert fake_provider.finalize_calls[0][0] == "error"
    assert fake_provider.finalize_calls[0][1] is lease_lost_error
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.restore_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []


async def test_ordinary_error_retried_three_times_then_dead_lettered(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """TSK-155：普通异常仍重试恰好 3 次后 dead-letter，attempt_count 透传真实重试次数。

    对照式断言：dead-letter 的 attempt_count 必须等于 fake 的 process 实际调用计数，
    钉住「透传真实重试次数」语义而非写死字面量。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    value_error = ValueError("总结失败")
    processor = _AlwaysFailProcessor(value_error)

    with pytest.raises(ValueError, match="总结失败") as exc_info:
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert exc_info.value is value_error  # 原异常实例原样抛出
    assert processor.process_calls == 3  # 普通异常重试 3 次语义不变
    assert len(fake_storage.dead_letter_calls) == 1
    assert fake_storage.dead_letter_calls[0]["failure_code"] == "ValueError"
    assert fake_storage.dead_letter_calls[0]["attempt_count"] == processor.process_calls
    assert fake_storage.restore_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert fake_provider.finalize_calls[0][0] == "error"


async def test_lease_lost_midway_stops_retry_immediately(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """TSK-155：重试中途首次出现 lease-lost → 立即停止，第 3 次不发生。

    脚本化：第 1 次 ValueError、第 2 次 ConversationLeaseLostError → 恰好 2 次
    process 调用；按 lease-lost 分流（dead-letter / restore 零调用）；抛出的
    就是那个 lease-lost 实例。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    lease_lost_error = ConversationLeaseLostError("pk")
    processor = _ScriptedErrorsProcessor([ValueError("第一次失败"), lease_lost_error])

    with pytest.raises(ConversationLeaseLostError) as exc_info:
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert exc_info.value is lease_lost_error  # 抛出的就是那个 lease-lost 实例
    assert processor.process_calls == 2  # 第 3 次调用不发生
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.restore_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []
    assert fake_provider.finalize_calls[0][0] == "error"
    assert fake_provider.finalize_calls[0][1] is lease_lost_error


async def test_manifest_mismatch_dead_letters_invalid_chunk_ledger(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F18 缺口补测：manifest 读回不一致 → InvalidChunkLedgerError → 走 dead-letter。"""
    canonical_manifest = json.dumps(
        {"version": 1, "chunks": [{"index": 0, "message_ids": ["m1"]}]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fake_storage.chunk_ledger["manifest"] = '{"version": 999}'
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    processor = _ManifestMismatchProcessor(canonical_manifest)

    with pytest.raises(InvalidChunkLedgerError):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert processor.process_calls == 3
    assert len(fake_storage.dead_letter_calls) == 1
    assert fake_storage.dead_letter_calls[0]["failure_code"] == "InvalidChunkLedgerError"
    assert fake_storage.dead_letter_calls[0]["attempt_count"] == 3


async def test_ledger_owner_loss_propagates_lease_lost(
    fake_storage: FakeProcessingStorage,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F22 + TSK-155：账本动词 owner 失效 → lease-lost 首次出现即分流，不再被重试。

    原用例钉「被重试恰好 3 次」（TSK-146 ⑥ 冻结怪癖），本票解除该怪癖后
    同步改为恰好 1 次；零副作用语义（无 dead-letter/restore）不变。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.ledger_error = ConversationLeaseLostError("pk")
    processor = _LedgerErrorProcessor()

    with pytest.raises(ConversationLeaseLostError):
        await lifecycle.process_conversation_snapshot("g1", processor)

    assert processor.process_calls == 1  # lease-lost 不再被重试
    assert fake_storage.dead_letter_calls == []
    assert fake_storage.restore_calls == []
    assert fake_storage.ack_calls == []
    assert fake_storage.update_last_summary_calls == []


async def test_empty_buffer_consumed_without_processor(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F23 缺口补测：空缓冲 → processor 未调用、照常门控+ack、返回 True。"""
    processor = RecordingProcessor()

    claimed = await lifecycle.process_conversation_snapshot("g1", processor)

    assert claimed is True
    assert processor.process_calls == []
    assert len(fake_storage.ack_calls) == 1
    assert fake_storage.update_last_summary_calls == ["g1"]
    assert fake_provider.finalize_calls == [("success", None)]


async def test_run_worker_cycle_takeover_failure_does_not_interrupt(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F24 缺口补测：孤儿接管失败仅 log 不中断；resumed_groups 去重；should_trigger 过滤。

    processor_factory 每个触发尝试调用一次（接管失败的孤儿也会创建 processor）。
    """
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    fake_storage.orphaned_keys = [("g1", "pk-orphan-1"), ("g2", "pk-orphan-2")]
    fake_storage.claim_existing_script = [
        ConversationSnapshotClaim(status="claimed", processing_key="pk-orphan-1"),
        RuntimeError("接管失败"),
    ]
    fake_storage.active_groups = ["g1", "g3", "g4"]
    fake_storage.should_trigger_results = {"g3": True, "g4": False}
    processors: list[RecordingProcessor] = []

    def processor_factory() -> RecordingProcessor:
        processor = RecordingProcessor()
        processors.append(processor)
        return processor

    await lifecycle.run_worker_cycle(processor_factory)

    assert [call["group_id"] for call in fake_storage.claim_existing_calls] == ["g1", "g2"]
    assert [call["group_id"] for call in fake_storage.claim_conversation_buffer_calls] == ["g3"]
    assert len(processors) == 3
    assert len(processors[0].process_calls) == 1
    assert processors[1].process_calls == []
    assert len(processors[2].process_calls) == 1
    assert [call[0] for call in fake_provider.create_calls] == ["g1", "g3"]


async def test_run_worker_cycle_tolerates_scan_and_trigger_failure(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F24：孤儿扫描失败仅 log 仍触发活跃群；should_trigger 抛异常仅 log；全程不抛。"""
    fake_storage.orphan_scan_error = RuntimeError("孤儿扫描失败")
    fake_storage.active_groups = ["g1", "g2"]
    fake_storage.should_trigger_results = {"g1": True}
    fake_storage.should_trigger_errors = {"g2": RuntimeError("触发判断失败")}
    processors: list[RecordingProcessor] = []

    def processor_factory() -> RecordingProcessor:
        processor = RecordingProcessor()
        processors.append(processor)
        return processor

    await lifecycle.run_worker_cycle(processor_factory)

    assert [call["group_id"] for call in fake_storage.claim_conversation_buffer_calls] == ["g1"]
    assert len(processors) == 1
    assert [call[0] for call in fake_provider.create_calls] == ["g1"]


async def test_success_finalizes_after_ack_and_updates_last_summary(
    fake_storage: FakeProcessingStorage,
    fake_provider: FakeCollectorProvider,
    lifecycle: ConversationProcessingLifecycle,
) -> None:
    """F25：success finalize 在 ack 之后；update_last_summary 仅成功路径（ack 之后）。"""
    events: list[str] = []
    fake_storage.events = events
    fake_provider.events = events
    fake_storage.buffer_items = [dict(_VALID_MESSAGE)]
    processor = RecordingProcessor()

    claimed = await lifecycle.process_conversation_snapshot("g1", processor)

    assert claimed is True
    assert len(processor.process_calls) == 1
    session = processor.process_calls[0]
    assert session.group_id == "g1"
    assert session.processing_key == fake_storage.ack_calls[0]["processing_key"]
    assert session.collector is fake_provider.collector
    assert session.ledger is not None
    assert len(fake_storage.ack_calls) == 1
    assert fake_storage.update_last_summary_calls == ["g1"]
    assert fake_provider.finalize_calls == [("success", None)]
    assert events.index("ack") < events.index("finalize:success")
    assert events.index("finalize:success") < events.index("update_last_summary")


def test_heartbeat_interval_boundaries() -> None:
    """F6 缺口补测：interval = max(1.0, lease_seconds / 3)。"""
    assert lifecycle_module._heartbeat_interval(30) == 10.0
    assert lifecycle_module._heartbeat_interval(120) == 40.0
    assert lifecycle_module._heartbeat_interval(900) == 300.0
    assert lifecycle_module._heartbeat_interval(1) == 1.0


async def test_heartbeat_loop_loses_lease_after_two_consecutive_errors(
    install_wait_for: Callable[[int], None],
) -> None:
    """F7：异常连续 2 次才置 lost（consecutive_errors < 2 时 continue）。"""
    install_wait_for(2)
    fake = FakeProcessingStorage()
    fake.renew_script = [RuntimeError("续租异常"), RuntimeError("续租异常")]
    stop, lease_lost = asyncio.Event(), asyncio.Event()

    await lifecycle_module._heartbeat_loop(
        storage=fake,
        group_id="g1",
        processing_key="pk",
        owner_token="owner",
        stop=stop,
        lease_lost=lease_lost,
        interval=40.0,
    )

    assert lease_lost.is_set()
    assert len(fake.renew_calls) == 2


async def test_heartbeat_loop_renew_true_resets_error_counter(
    install_wait_for: Callable[[int], None],
) -> None:
    """F7：[异常, True, 异常] → renewed=True 清零计数 → 不 lost；stop 置位后干净退出。"""
    install_wait_for(3)
    fake = FakeProcessingStorage()
    fake.renew_script = [RuntimeError("续租异常"), True, RuntimeError("续租异常")]
    stop, lease_lost = asyncio.Event(), asyncio.Event()
    fake.stop_event = stop
    fake.set_stop_after_renew = 3

    await lifecycle_module._heartbeat_loop(
        storage=fake,
        group_id="g1",
        processing_key="pk",
        owner_token="owner",
        stop=stop,
        lease_lost=lease_lost,
        interval=40.0,
    )

    assert not lease_lost.is_set()
    assert len(fake.renew_calls) == 3


async def test_heartbeat_loop_renew_false_loses_lease_immediately(
    install_wait_for: Callable[[int], None],
) -> None:
    """F7：renewed=False 不计数、立即置 lost。"""
    install_wait_for(1)
    fake = FakeProcessingStorage()
    fake.renew_script = [False]
    stop, lease_lost = asyncio.Event(), asyncio.Event()

    await lifecycle_module._heartbeat_loop(
        storage=fake,
        group_id="g1",
        processing_key="pk",
        owner_token="owner",
        stop=stop,
        lease_lost=lease_lost,
        interval=40.0,
    )

    assert lease_lost.is_set()
    assert len(fake.renew_calls) == 1


async def test_heartbeat_loop_stop_set_exits_cleanly(
    install_wait_for: Callable[[int], None],
) -> None:
    """F7：stop 置位 → 心跳干净退出（零续租、不置 lost）。"""
    install_wait_for(5)
    fake = FakeProcessingStorage()
    stop, lease_lost = asyncio.Event(), asyncio.Event()
    stop.set()

    await lifecycle_module._heartbeat_loop(
        storage=fake,
        group_id="g1",
        processing_key="pk",
        owner_token="owner",
        stop=stop,
        lease_lost=lease_lost,
        interval=40.0,
    )

    assert len(fake.renew_calls) == 0
    assert not lease_lost.is_set()


async def test_heartbeat_loop_logs_renew_error_via_exception(
    install_wait_for: Callable[[int], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-156：续租异常改用 logger.exception（带 traceback），不再走 logger.warning。

    与互动/忘却生命周期日志形态一致（F7 容错不对称语义不变，本用例只钉日志形态）；
    message 沿用四个槽位 group/key/failures/error_type，failures 即连续失败计数。
    """
    install_wait_for(2)
    fake = FakeProcessingStorage()
    fake.renew_script = [RuntimeError("续租异常"), RuntimeError("续租异常")]
    stop, lease_lost = asyncio.Event(), asyncio.Event()
    exception_logs: list[tuple[object, tuple[object, ...]]] = []
    warning_logs: list[tuple[object, tuple[object, ...]]] = []
    monkeypatch.setattr(
        lifecycle_module.logger,
        "exception",
        lambda message, *args: exception_logs.append((message, args)),
    )
    monkeypatch.setattr(
        lifecycle_module.logger,
        "warning",
        lambda message, *args: warning_logs.append((message, args)),
    )

    await lifecycle_module._heartbeat_loop(
        storage=fake,
        group_id="g1",
        processing_key="pk",
        owner_token="owner",
        stop=stop,
        lease_lost=lease_lost,
        interval=40.0,
    )

    # 容错不对称（F7）：异常连续 2 次才置 lost，续租恰好 2 次
    assert lease_lost.is_set()
    assert len(fake.renew_calls) == 2
    # 每次续租异常都经 logger.exception 记录，恰好 2 次，四个槽位齐全
    assert len(exception_logs) == 2
    for message, args in exception_logs:
        assert "续租异常" in str(message)
        assert "group={}" in str(message)
        assert "key={}" in str(message)
        assert "failures={}" in str(message)
        assert "error_type={}" in str(message)
        assert args[0] == "g1"
        assert args[1] == "pk"
        assert args[3] == "RuntimeError"
    # failures 计数随连续失败递增：第 1 次为 1、第 2 次为 2
    assert exception_logs[0][1][2] == 1
    assert exception_logs[1][1][2] == 2
    # 该路径不再用 logger.warning 记录续租异常
    assert warning_logs == []


def test_module_reexports_lease_lost_error() -> None:
    """ConversationLeaseLostError 定义在 conversation_processing.py，module 重导出成立。"""
    assert lifecycle_module.ConversationLeaseLostError is ConversationLeaseLostError
