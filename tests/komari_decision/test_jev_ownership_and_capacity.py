"""TSK-182 引擎修正验收 D/E：清理期名额、所有权发布与租约恢复。

并发与所有权断言都以**真实外部效果**（SDK transport 进入、引用补取、调用方
连接可用性、pg_locks 会话锁）为预言机，而不是只看行标记。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from komari_bot.decision.jev import DecisionResult
from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    HarnessOptions,
    ScriptedTransport,
    admin_engine,
    advisory_session_lock_count,
    build_answers,
    create_scratch_database,
    drop_scratch_database,
    kill_connections,
    make_identity,
    make_message,
    make_request,
    read_dispatch,
    skip_without_services,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import httpx2

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevown{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


class CleanupBlockingTransport(ScriptedTransport):
    """第一次关闭被显式挂起的 transport：模拟取消之后的真实清理。"""

    def __init__(self, responder: Callable[[httpx2.Request, int], Any]) -> None:
        super().__init__(responder)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            self.close_started.set()
            await self.release_close.wait()
        await super().aclose()


async def test_capacity_is_held_until_cancelled_cleanup_finishes(
    decision_database: str,
) -> None:
    """AC：撤销不立即释放名额；替代任务的外部效果必须等到清理结束。"""
    entries: list[tuple[str, float]] = []
    hold_first = asyncio.Event()
    admitted = {"a": True}

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        body = json.loads(request.content)
        conversation = body["state"]["untrusted_group_conversation"]
        target_text = conversation["messages"][conversation["target"]]["text"]
        entries.append((target_text, time.monotonic()))
        if index == 0:
            await hold_first.wait()
        return build_answers(body["questions"])

    transport = CleanupBlockingTransport(responder)
    options = HarnessOptions(
        responder=responder,
        transport=transport,
        admission=lambda identity: identity.group_id != "group-a" or admitted["a"],
        # 本用例的预言机是「名额 vs 清理」；排队预算放宽，避免与 3s 截止竞争
        config={"max_concurrent_decisions": 1, "queue_timeout_seconds": 15.0},
    )
    first_task: asyncio.Task[Any] | None = None
    second_task: asyncio.Task[Any] | None = None
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        try:
            identity_a = make_identity(group_id="group-a", message_id="a1")
            first_task = asyncio.create_task(
                harness.decision.evaluate(
                    make_request(
                        identity=identity_a,
                        target=make_message(identity=identity_a, text="A-任务"),
                    )
                )
            )
            await wait_until(lambda: len(entries) == 1)

            # 撤销 A：观察者持久化撤销并取消本地任务，取消后进入真实关闭清理
            admitted["a"] = False
            await asyncio.wait_for(transport.close_started.wait(), timeout=10)

            identity_b = make_identity(group_id="group-b", message_id="b1")
            second_task = asyncio.create_task(
                harness.decision.evaluate(
                    make_request(
                        identity=identity_b,
                        target=make_message(identity=identity_b, text="B-任务"),
                    )
                )
            )
            # 外部效果预言机先于任何行状态断言：清理未放行前 B 绝不能进入 SDK。
            await asyncio.sleep(0.6)
            assert [target_text for target_text, _ in entries] == ["A-任务"]

            # 补充观测：清理期间该行仍持有名额（不是并发预言机）
            row_a = await read_dispatch(harness.engine, identity_a)
            assert row_a is not None
            assert row_a["state"] == "cancelled"
            assert row_a["reason_code"] == "admission_revoked"
            assert row_a["slot_held"] is True

            transport.release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first_task, timeout=15)
            first_task = None
            result_b = await asyncio.wait_for(second_task, timeout=15)
            second_task = None
            assert result_b.status == "success", result_b.reason_code
            assert [target_text for target_text, _ in entries] == [
                "A-任务",
                "B-任务",
            ]
            assert entries[1][1] >= entries[0][1]
        finally:
            # 无论断言是否失败都必须放行清理并回收任务，避免夹具永久等待
            transport.release_close.set()
            hold_first.set()
            pending = [
                task
                for task in (first_task, second_task)
                if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


async def test_commit_failure_does_not_publish_ownership(
    decision_database: str,
) -> None:
    """AC：进程实例行的提交失败时不得发布所有权，也不得泄漏自有连接。"""
    armed = {"value": True}
    statements: list[str] = []

    class CommitFailingSession:
        """外部 SQLAlchemy 会话边界上的提交故障注入。"""

        def __init__(self, inner: AsyncSession) -> None:
            self._inner = inner

        async def execute(self, statement: Any, params: Any = None) -> Any:
            statements.append(str(statement))
            if params is None:
                return await self._inner.execute(statement)
            return await self._inner.execute(statement, params)

        async def scalar(self, statement: Any, params: Any = None) -> Any:
            statements.append(str(statement))
            if params is None:
                return await self._inner.scalar(statement)
            return await self._inner.scalar(statement, params)

        def scalars(self, statement: Any, params: Any = None) -> Any:
            statements.append(str(statement))
            if params is None:
                return self._inner.scalars(statement)
            return self._inner.scalars(statement, params)

        async def commit(self) -> None:
            if armed["value"] and any(
                "komari_decision_processes" in item for item in statements
            ):
                msg = "synthetic commit failure"
                raise RuntimeError(msg)
            await self._inner.commit()

        async def rollback(self) -> None:
            await self._inner.rollback()

        async def close(self) -> None:
            await self._inner.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    def wrap(factory: Callable[[], AsyncSession]) -> Callable[[], AsyncSession]:
        def wrapped() -> AsyncSession:
            return CommitFailingSession(factory())  # pyright: ignore[reportReturnType]

        return wrapped

    options = HarnessOptions(session_factory_wrapper=wrap)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        health = harness.decision.get_health()
        assert health.runtime_status == "failed"
        assert health.reason_code == "storage_unavailable"

        admin = await admin_engine(decision_database)
        try:
            # 自有连接必须已释放：该应用名下不再持有会话级 advisory lock
            assert (
                await advisory_session_lock_count(admin, harness.application_name)
                == 0
            )
        finally:
            await admin.dispose()

        # 修正外部存储之后，refresh 可以重新取得所有权
        armed["value"] = False
        await harness.decision.refresh()
        assert harness.decision.get_health().runtime_status == "ready"
        result = await harness.decision.evaluate(make_request())
        assert result.status == "success"


async def test_async_connection_bound_session_factory_keeps_caller_connection(
    decision_database: str,
) -> None:
    """AC：会话绑定在调用方连接上时另取自有连接，且不关闭调用方连接。"""
    admin = await admin_engine(decision_database, application_name="jev-caller-conn")
    try:
        async with admin.connect() as caller_connection:

            def bind_caller(_factory: Callable[[], AsyncSession]) -> Any:
                def factory() -> AsyncSession:
                    return AsyncSession(bind=caller_connection, expire_on_commit=False)

                return factory

            options = HarnessOptions(session_factory_wrapper=bind_caller)
            async with DecisionHarness(decision_database, options) as harness:
                assert harness.decision is not None
                result = await harness.decision.evaluate(make_request())
                assert result.status == "success"
                assert harness.decision.get_health().runtime_status == "ready"

            # 调用方连接没有被借用/关闭，仍然可用
            assert not caller_connection.closed
            assert await caller_connection.scalar(text("SELECT 1")) == 1
    finally:
        await admin.dispose()


async def test_lease_loss_recovery_replaces_incarnation_and_blocks_old_events(
    decision_database: str,
) -> None:
    """AC：租约丢失后 refresh 建立新 incarnation；旧事件保持不可交接。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
            await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(
            identity=identity, target=make_message(identity=identity)
        )
        pending = asyncio.create_task(harness.decision.evaluate(request))
        await wait_until(lambda: bool(harness.transport.requests))

        admin = await admin_engine(decision_database)
        try:
            assert await kill_connections(admin, harness.application_name) > 0
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=15)

            # 租约丢失后必须先如实报告失败，再由 refresh 建立新 incarnation
            await wait_until(
                lambda: harness.decision.get_health().runtime_status == "failed"  # type: ignore[union-attr]
            )
            await harness.decision.refresh()
            assert harness.decision.get_health().runtime_status == "ready"

            fresh = make_request()
            result = await harness.decision.evaluate(fresh)
            assert result.status == "success"
            assert await harness.decision.claim_handoff(fresh, result, "chat") is True

            redelivered = await harness.decision.evaluate(
                make_request(
                    identity=identity, target=make_message(identity=identity)
                )
            )
            assert redelivered.status == "duplicate"
            stale = DecisionResult(
                decision_id=str(uuid4()),
                status="success",
                reason_code="classified",
                route="chat",
            )
            assert (
                await harness.decision.claim_handoff(request, stale, "chat") is False
            )
        finally:
            gate.set()
            await admin.dispose()
