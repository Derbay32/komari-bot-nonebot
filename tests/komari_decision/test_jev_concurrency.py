"""TSK-182 判定引擎验收：去重、共享调度、单次交接、停用撤销与进程所有权。

并发验收使用**真实效果区间**（各进程记录 SDK 调用与引用补取的进入/退出
时间戳，由父进程计算重叠峰值），数据库名额标记只作为补充观测。多进程用例
使用真实独立解释器、启动屏障与真实 Prompt 加载器（仅替换外部会话入口）。
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import redis.asyncio as redis_asyncio

from komari_bot.decision.jev import DecisionResult, EventIdentity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    import httpx2

from tests.komari_decision.jev_test_support import (
    TEST_REDIS_URL,
    DecisionHarness,
    HarnessOptions,
    admin_engine,
    build_answers,
    clean_test_scopes,
    count_requests_by_status,
    create_scratch_database,
    drop_scratch_database,
    kill_connections,
    make_identity,
    make_message,
    make_request,
    peak_overlap,
    read_dispatch,
    read_slot_rows,
    reset_decision_storage,
    skip_without_services,
    spawn_worker,
    update_config_row,
    worker_payload,
    write_config_row,
    write_scratch_prompt_row,
)


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevconc{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


@pytest.fixture(autouse=True)
def _clean_test_redis_scopes() -> Any:
    """只删除本用例创建的作用域键（同步执行，兼顾 sync/async 用例）。"""
    yield
    if not TEST_REDIS_URL:
        return

    async def _clean() -> None:
        client = redis_asyncio.Redis.from_url(TEST_REDIS_URL)
        try:
            await clean_test_scopes(client)
        finally:
            await client.aclose()

    asyncio.run(_clean())


class Probe:
    """记录并发峰值与请求顺序的 transport 探针。"""

    def __init__(self, *, hold_seconds: float = 0.0) -> None:
        self.active = 0
        self.peak = 0
        self.order: list[str] = []
        self.requests = 0
        self._hold = hold_seconds

    async def responder(self, request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        body = json.loads(request.content)
        conversation = body["state"]["untrusted_group_conversation"]
        node = conversation["messages"][conversation["target"]]
        self.requests += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.order.append(node["text"])
        try:
            if self._hold:
                await asyncio.sleep(self._hold)
        finally:
            self.active -= 1
        return build_answers(body["questions"])


async def _wait_for_row(
    engine: Any, identity: EventIdentity, state: str, *, within_seconds: float = 5.0
) -> None:
    """等待某事件的身份行进入指定状态（用持久事实定序，不靠 sleep 猜时序）。"""
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        row = await read_dispatch(engine, identity)
        if row is not None and row["state"] == state:
            return
        await asyncio.sleep(0.01)
    msg = f"等待身份行状态超时: {state}"
    raise AssertionError(msg)


async def _wait_for(
    predicate: Callable[[], bool], *, within_seconds: float = 5.0
) -> None:
    deadline = time.monotonic() + within_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    msg = "等待条件超时"
    raise AssertionError(msg)


# ------------------------------ 去重与本地复用 ------------------------------


@pytest.mark.asyncio
async def test_same_event_has_exactly_one_winner(decision_database: str) -> None:
    """AC：同一事件并发分发只有一个胜者，另一分发为重复事件。"""
    probe = Probe(hold_seconds=0.05)
    options = HarnessOptions(responder=probe.responder)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity()
        first = make_request(identity=identity, target=make_message(identity=identity))
        second = make_request(
            identity=identity, target=make_message(identity=identity)
        )

        results = await asyncio.gather(
            harness.decision.evaluate(first),
            harness.decision.evaluate(second),
        )
        statuses = sorted(result.status for result in results)
        assert statuses == ["duplicate", "success"]
        assert probe.requests == 1
        duplicate = next(r for r in results if r.status == "duplicate")
        assert duplicate.reason_code == "duplicate_event"
        assert duplicate.route == "none"


@pytest.mark.asyncio
async def test_same_dispatch_reuses_one_local_task_even_if_a_waiter_is_cancelled(
    decision_database: str,
) -> None:
    """AC：同次分发复用同一个本地任务；取消等待者不会重新发起请求。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        started.set()
        await release.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(responder=responder)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        request = make_request()
        first = asyncio.create_task(harness.decision.evaluate(request))
        await asyncio.wait_for(started.wait(), timeout=5)
        second = asyncio.create_task(harness.decision.evaluate(request))
        await asyncio.sleep(0.02)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await second

        assert result.status == "success"
        assert len(harness.transport.requests) == 1
        again = await harness.decision.evaluate(request)
        assert again.decision_id == result.decision_id
        assert len(harness.transport.requests) == 1


# ------------------------------ 队列与共享额度 ------------------------------


@pytest.mark.asyncio
async def test_queue_full_refuses_only_the_new_task(decision_database: str) -> None:
    """AC：队列满时拒绝新任务，不挤掉已接收任务。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder,
        config={
            "max_concurrent_decisions": 1,
            "max_queued_messages_per_group": 1,
        },
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"

        def request_for(message_id: str) -> Any:
            identity = make_identity(
                account_id=account_id, group_id=group_id, message_id=message_id
            )
            return make_request(
                identity=identity,
                target=make_message(identity=identity, text=f"消息-{message_id}"),
            )

        running_task = asyncio.create_task(
            harness.decision.evaluate(request_for("m1"))
        )
        await _wait_for(lambda: bool(harness.transport.requests))

        waiting_task = asyncio.create_task(
            harness.decision.evaluate(request_for("m2"))
        )
        await asyncio.sleep(0.05)
        refused_result = await harness.decision.evaluate(request_for("m3"))
        assert refused_result.status == "failed"
        assert refused_result.reason_code == "queue_full"

        gate.set()
        first_result, second_result = await asyncio.gather(
            running_task, waiting_task
        )
        assert first_result.status == "success"
        assert second_result.status == "success"
        assert len(harness.transport.requests) == 2


@pytest.mark.asyncio
async def test_queue_timeout_never_reaches_model_or_references(
    decision_database: str,
) -> None:
    """AC：排队过期后不补取引用、不调用 Jev。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder,
        config={"max_concurrent_decisions": 1, "queue_timeout_seconds": 0.2},
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"
        first_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m1"
        )
        running = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=first_identity,
                    target=make_message(identity=first_identity),
                )
            )
        )
        await _wait_for(lambda: bool(harness.transport.requests))

        second_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m2"
        )
        expired = await harness.decision.evaluate(
            make_request(
                identity=second_identity,
                target=make_message(
                    identity=second_identity, reference_ids=("ref-1",)
                ),
            )
        )
        assert expired.status == "failed"
        assert expired.reason_code == "queue_timeout"
        assert harness.reference_calls == []
        assert len(harness.transport.requests) == 1

        gate.set()
        assert (await running).status == "success"


@pytest.mark.asyncio
async def test_single_group_runs_one_task_at_a_time(decision_database: str) -> None:
    """AC：同群任何时刻最多一个执行任务（同一账号/同一群）。"""
    probe = Probe(hold_seconds=0.05)
    options = HarnessOptions(
        responder=probe.responder, config={"max_concurrent_decisions": 4}
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"
        requests = []
        for index in range(3):
            identity = make_identity(
                account_id=account_id, group_id=group_id, message_id=f"m{index}"
            )
            requests.append(
                make_request(
                    identity=identity,
                    target=make_message(identity=identity, text=f"同群消息{index}"),
                )
            )
        results = await asyncio.gather(
            *(harness.decision.evaluate(request) for request in requests)
        )
        assert [result.status for result in results] == ["success"] * 3
        assert probe.peak == 1


@pytest.mark.asyncio
async def test_new_group_joins_behind_already_waiting_work(
    decision_database: str,
) -> None:
    """AC：新群/空闲群排在已在等待的群之后，随后仍然轮转（不排空单群）。"""
    probe = Probe()
    gate = asyncio.Event()
    blocked = {"armed": True}

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if blocked["armed"]:
            await gate.wait()
        return await probe.responder(request, index)

    options = HarnessOptions(
        responder=responder, config={"max_concurrent_decisions": 1}
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_a = f"group-a-{uuid4().hex[:6]}"
        group_b = f"group-b-{uuid4().hex[:6]}"

        def identity_for(group_id: str, message_id: str) -> EventIdentity:
            return make_identity(
                account_id=account_id, group_id=group_id, message_id=message_id
            )

        def request_for(identity: EventIdentity, text: str) -> Any:
            return make_request(
                identity=identity,
                target=make_message(identity=identity, text=text),
            )

        a1 = identity_for(group_a, "a1")
        first_task = asyncio.create_task(
            harness.decision.evaluate(request_for(a1, "A1"))
        )
        await _wait_for(lambda: bool(harness.transport.requests))
        await _wait_for_row(harness.engine, a1, "running")

        # 用持久事实逐个定序入队：A2 已在等待后才让全新群 B1 加入
        a2 = identity_for(group_a, "a2")
        second_task = asyncio.create_task(
            harness.decision.evaluate(request_for(a2, "A2"))
        )
        await _wait_for_row(harness.engine, a2, "queued")

        b1 = identity_for(group_b, "b1")
        third_task = asyncio.create_task(
            harness.decision.evaluate(request_for(b1, "B1"))
        )
        await _wait_for_row(harness.engine, b1, "queued")

        a3 = identity_for(group_a, "a3")
        fourth_task = asyncio.create_task(
            harness.decision.evaluate(request_for(a3, "A3"))
        )
        await _wait_for_row(harness.engine, a3, "queued")

        b2 = identity_for(group_b, "b2")
        fifth_task = asyncio.create_task(
            harness.decision.evaluate(request_for(b2, "B2"))
        )
        await _wait_for_row(harness.engine, b2, "queued")

        gate.set()
        results = await asyncio.gather(
            first_task, second_task, third_task, fourth_task, fifth_task
        )
        assert [result.status for result in results] == ["success"] * 5
        # 尾部策略：A1,A2,B1；随后继续群间轮转（不是先排空 A）
        assert probe.order == ["A1", "A2", "B1", "A3", "B2"]


@pytest.mark.asyncio
async def test_idle_group_returns_at_the_rotation_tail(
    decision_database: str,
) -> None:
    """AC：长期空闲的群重新活跃时排到当前轮转尾部，而不是带着旧序号抢先。"""
    probe = Probe()
    gate = asyncio.Event()
    blocked = {"armed": False}

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if blocked["armed"]:
            await gate.wait()
        return await probe.responder(request, index)

    options = HarnessOptions(
        responder=responder, config={"max_concurrent_decisions": 1}
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_idle = f"group-i-{uuid4().hex[:6]}"
        group_other = f"group-o-{uuid4().hex[:6]}"

        def identity_for(group_id: str, message_id: str) -> EventIdentity:
            return make_identity(
                account_id=account_id, group_id=group_id, message_id=message_id
            )

        def request_for(identity: EventIdentity, text: str) -> Any:
            return make_request(
                identity=identity,
                target=make_message(identity=identity, text=text),
            )

        # 第一轮：空闲群先被服务一次（此后再无在途工作，成为空闲群）
        idle_first = identity_for(group_idle, "i1")
        first = await harness.decision.evaluate(request_for(idle_first, "I1"))
        assert first.status == "success"

        # 第二轮：另一群占住名额，两个群都被排入等待
        blocked["armed"] = True
        idle_second = identity_for(group_idle, "i2")
        runner = asyncio.create_task(
            harness.decision.evaluate(request_for(idle_second, "I2"))
        )
        await _wait_for_row(harness.engine, idle_second, "running")

        other = identity_for(group_other, "o1")
        other_task = asyncio.create_task(
            harness.decision.evaluate(request_for(other, "O1"))
        )
        await _wait_for_row(harness.engine, other, "queued")

        idle_third = identity_for(group_idle, "i3")
        later = asyncio.create_task(
            harness.decision.evaluate(request_for(idle_third, "I3"))
        )
        await _wait_for_row(harness.engine, idle_third, "queued")

        blocked["armed"] = False
        gate.set()
        results = await asyncio.gather(runner, other_task, later)
        assert [result.status for result in results] == ["success"] * 3
        # 空闲群 I 重新活跃时排到尾部：另一群先被服务
        assert probe.order == ["I1", "I2", "O1", "I3"]


@pytest.mark.asyncio
async def test_capacity_increase_is_read_live(decision_database: str) -> None:
    """AC：共享容量上调后，已排队任务按原顺序继续分配名额。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder, config={"max_concurrent_decisions": 1}
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_a = f"group-{uuid4().hex[:8]}"
        group_b = f"group-{uuid4().hex[:8]}"
        first_identity = make_identity(
            account_id=account_id, group_id=group_a, message_id="m1"
        )
        first_task = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=first_identity,
                    target=make_message(identity=first_identity),
                )
            )
        )
        await _wait_for(lambda: bool(harness.transport.requests))

        second_identity = make_identity(
            account_id=account_id, group_id=group_b, message_id="m2"
        )
        second_task = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=second_identity,
                    target=make_message(identity=second_identity),
                )
            )
        )
        await asyncio.sleep(0.2)
        assert len(harness.transport.requests) == 1
        assert await read_slot_rows(harness.engine) == 1

        await update_config_row(harness.engine, {"max_concurrent_decisions": 2})
        await _wait_for(lambda: len(harness.transport.requests) == 2, within_seconds=5)

        gate.set()
        results = await asyncio.gather(first_task, second_task)
        assert [result.status for result in results] == ["success"] * 2


@pytest.mark.asyncio
async def test_capacity_reduction_keeps_waiters_with_original_deadlines(
    decision_database: str,
) -> None:
    """AC：降低容量不挤掉已排队任务；空出的名额按新容量再分配。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder,
        config={
            "max_concurrent_decisions": 2,
            "max_queued_messages_per_group": 5,
        },
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        groups = [f"group-{uuid4().hex[:8]}" for _ in range(4)]
        tasks = []
        for index, group_id in enumerate(groups):
            identity = make_identity(
                account_id=account_id, group_id=group_id, message_id=f"m{index}"
            )
            tasks.append(
                asyncio.create_task(
                    harness.decision.evaluate(
                        make_request(
                            identity=identity,
                            target=make_message(identity=identity, text=f"消息{index}"),
                        )
                    )
                )
            )
            await asyncio.sleep(0.05)
        assert len(harness.transport.requests) == 2

        await update_config_row(harness.engine, {"max_concurrent_decisions": 1})
        gate.set()
        results = await asyncio.gather(*tasks)
        assert [result.status for result in results] == ["success"] * 4
        assert len(harness.transport.requests) == 4
        assert await count_requests_by_status(harness.engine) == {"success": 4}


# ------------------------------ 一次交接与撤销 ------------------------------


@pytest.mark.asyncio
async def test_handoff_qualification_is_consumed_once(decision_database: str) -> None:
    """AC：业务交接与失败通知各自一次性；结果本身不构成许可。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        request = make_request()
        result = await harness.decision.evaluate(request)
        assert result.route == "chat"

        assert await harness.decision.claim_handoff(request, result, "chat") is True
        assert await harness.decision.claim_handoff(request, result, "chat") is False
        assert await harness.decision.claim_handoff(request, result, "summary") is False
        assert (
            await harness.decision.claim_handoff(
                request, result, "failure_notification"
            )
            is False
        )


@pytest.mark.asyncio
async def test_forged_result_route_cannot_claim(decision_database: str) -> None:
    """AC：重建/伪造的结果不能把 silent 变成 chat 或 chat 变成 summary。"""

    def silent_responder(
        request: httpx2.Request, index: int
    ) -> dict[str, Any]:
        del index
        return build_answers(
            json.loads(request.content)["questions"],
            reply="silent",
            retention="noise",
        )

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=silent_responder)
    ) as harness:
        assert harness.decision is not None
        request = make_request()
        silent = await harness.decision.evaluate(request)
        assert silent.status == "success"
        assert silent.route == "none"
        forged_chat = DecisionResult(
            decision_id=silent.decision_id,
            status="success",
            reason_code=silent.reason_code,
            route="chat",
            force_reply=True,
        )
        assert (
            await harness.decision.claim_handoff(request, forged_chat, "chat")
            is False
        )

    async with DecisionHarness(
        decision_database,
        HarnessOptions(
            reset_storage=False, write_config_row=False, write_prompt_row=True
        ),
    ) as harness:
        assert harness.decision is not None
        chat_request = make_request()
        chat_result = await harness.decision.evaluate(chat_request)
        assert chat_result.route == "chat"
        forged_summary = DecisionResult(
            decision_id=chat_result.decision_id,
            status=chat_result.status,
            reason_code=chat_result.reason_code,
            route="summary",
        )
        assert (
            await harness.decision.claim_handoff(
                chat_request, forged_summary, "summary"
            )
            is False
        )
        assert (
            await harness.decision.claim_handoff(chat_request, chat_result, "chat")
            is True
        )


@pytest.mark.asyncio
async def test_disable_trigger_invalidates_unhanded_late_results(
    decision_database: str,
) -> None:
    """AC：停用撤销未交接资格；恢复启用不会复活迟到结果。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(identity=identity)
        result = await harness.decision.evaluate(request)
        assert result.status == "success"

        await update_config_row(harness.engine, {"plugin_enable": False})
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "disabled"
        assert await harness.decision.claim_handoff(request, result, "chat") is False

        await update_config_row(harness.engine, {"plugin_enable": True})
        assert await harness.decision.claim_handoff(request, result, "chat") is False


@pytest.mark.asyncio
async def test_handoff_rechecks_live_admission(decision_database: str) -> None:
    """AC：交接前重新裁决准入；撤销后结果失效且不可复活。"""
    admitted = {"value": True}

    def admission(_identity: EventIdentity) -> bool:
        return admitted["value"]

    options = HarnessOptions(admission=admission)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(identity=identity)
        result = await harness.decision.evaluate(request)
        assert result.status == "success"

        admitted["value"] = False
        assert await harness.decision.claim_handoff(request, result, "chat") is False
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"

        admitted["value"] = True
        assert await harness.decision.claim_handoff(request, result, "chat") is False


@pytest.mark.asyncio
async def test_disable_during_reference_read_blocks_the_model_call(
    decision_database: str,
) -> None:
    """AC：持有引用读取期间 false→true 停用后不得再发起 SDK 请求。"""
    reference_started = asyncio.Event()
    release = asyncio.Event()

    async def reference_reader(
        _identity: EventIdentity, message_id: str
    ) -> Any:
        reference_started.set()
        await release.wait()
        return make_message(
            group_id=_identity.group_id, message_id=message_id, text="引用原文"
        )

    options = HarnessOptions(reference_reader=reference_reader)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(
            identity=identity,
            target=make_message(identity=identity, reference_ids=("ref-1",)),
        )
        pending = asyncio.create_task(harness.decision.evaluate(request))
        await asyncio.wait_for(reference_started.wait(), timeout=5)
        # 停用再启用：config_source 之后仍会看到 true，但持久撤销已经生效
        await update_config_row(harness.engine, {"plugin_enable": False})
        await update_config_row(harness.engine, {"plugin_enable": True})
        release.set()
        result = await asyncio.wait_for(pending, timeout=15)

        assert result.status == "cancelled"
        assert result.reason_code in {"disabled", "interrupted", "admission_revoked"}
        assert harness.transport.requests == []


@pytest.mark.asyncio
async def test_admission_revocation_during_model_call_cancels_locally(
    decision_database: str,
) -> None:
    """AC：观察到的撤权取消本地任务、持久撤销，且迟到结果不能恢复资格。"""
    started = asyncio.Event()
    admitted = {"value": True}

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        started.set()
        await asyncio.sleep(5)
        return build_answers(json.loads(request.content)["questions"])

    def admission(_identity: EventIdentity) -> bool:
        return admitted["value"]

    options = HarnessOptions(responder=responder, admission=admission)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(identity=identity, target=make_message(identity=identity))
        pending = asyncio.create_task(harness.decision.evaluate(request))
        await asyncio.wait_for(started.wait(), timeout=5)
        # 只改编排层裁决（数据库没有被触发器改动）
        admitted["value"] = False
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=15)

        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert harness.transport.requests and len(harness.transport.requests) == 1

        admitted["value"] = True
        stale = DecisionResult(
            decision_id=str(uuid4()),
            status="success",
            reason_code="classified",
            route="chat",
        )
        assert await harness.decision.claim_handoff(request, stale, "chat") is False


# ------------------------------ 重启、崩溃与所有权 ------------------------------


@pytest.mark.asyncio
async def test_redelivery_after_restart_is_duplicate(decision_database: str) -> None:
    """AC：重启后同一事件不再判定、不再抽签、不再交接。"""
    identity = make_identity()
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=make_message(identity=identity))
        )
        assert result.status == "success"

    options = HarnessOptions(
        reset_storage=False, write_prompt_row=False, write_config_row=False
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        redelivered = await harness.decision.evaluate(
            make_request(identity=identity, target=make_message(identity=identity))
        )
        assert redelivered.status == "duplicate"
        assert harness.transport.requests == []
        assert harness.random_draws == 0


@pytest.mark.asyncio
async def test_live_owner_keeps_its_slot_while_another_engine_starts(
    decision_database: str,
) -> None:
    """AC：另一实例初始化/入队不得回收存活实例的工作与名额。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    admitted = {"value": True}

    def admission(_identity: EventIdentity) -> bool:
        return admitted["value"]

    options = HarnessOptions(
        responder=responder,
        admission=admission,
        config={"max_concurrent_decisions": 1},
    )
    identity = make_identity()
    async with DecisionHarness(decision_database, options) as alive:
        assert alive.decision is not None and alive.engine is not None
        request = make_request(identity=identity, target=make_message(identity=identity))
        pending = asyncio.create_task(alive.decision.evaluate(request))
        await _wait_for(lambda: bool(alive.transport.requests))

        second_options = HarnessOptions(
            reset_storage=False,
            write_prompt_row=False,
            write_config_row=False,
            initialize=True,
        )
        async with DecisionHarness(decision_database, second_options) as other:
            assert other.decision is not None and other.engine is not None
            admin = await admin_engine(decision_database)
            try:
                row = await read_dispatch(admin, identity)
                assert row is not None
                assert row["state"] == "running"
                assert row["slot_held"] is True
                assert await read_slot_rows(admin) == 1
            finally:
                await admin.dispose()

            # 另一实例也入队（容量 1 被他占用）：应保持等待，不越权执行
            other_group = f"group-{uuid4().hex[:8]}"
            other_identity = make_identity(
                account_id=identity.account_id,
                group_id=other_group,
                message_id="o1",
            )
            other_task = asyncio.create_task(
                other.decision.evaluate(
                    make_request(
                        identity=other_identity,
                        target=make_message(identity=other_identity),
                    )
                )
            )
            await asyncio.sleep(0.3)
            assert other.transport.requests == []

            gate.set()
            first_result = await asyncio.wait_for(pending, timeout=10)
            assert first_result.status == "success"
            # 存活实例保留并消费自己的交接资格
            assert (
                await alive.decision.claim_handoff(request, first_result, "chat")
                is True
            )
            second_result = await asyncio.wait_for(other_task, timeout=15)
            assert second_result.status == "success"


@pytest.mark.asyncio
async def test_crash_abandons_work_without_reexecution(decision_database: str) -> None:
    """AC：崩溃后的未完成事件被中断，不再调用/抽签/交接。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(responder=responder)
    identity = make_identity()
    async with DecisionHarness(decision_database, options) as crashed:
        assert crashed.decision is not None
        request = make_request(identity=identity, target=make_message(identity=identity))
        pending = asyncio.create_task(crashed.decision.evaluate(request))
        await _wait_for(lambda: bool(crashed.transport.requests))

        # 外部连接丢失故障：只终止本夹具应用名归属的连接
        admin = await admin_engine(decision_database)
        try:
            killed = await kill_connections(admin, crashed.application_name)
            assert killed > 0
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=15)
            row = await read_dispatch(admin, identity)
            assert row is not None
            assert row["state"] == "running"
            assert row["slot_held"] is True
            # 租约丢失后该实例不得再取得任何交接资格
            stale = DecisionResult(
                decision_id=str(uuid4()),
                status="success",
                reason_code="classified",
                route="chat",
            )
            assert (
                await crashed.decision.claim_handoff(request, stale, "chat") is False
            )

            # 新实例回收：中断该工作并释放名额
            recovery_options = HarnessOptions(
                reset_storage=False,
                write_prompt_row=False,
                write_config_row=False,
            )
            async with DecisionHarness(decision_database, recovery_options) as revival:
                assert revival.decision is not None
                row = await read_dispatch(admin, identity)
                assert row is not None
                assert row["state"] == "cancelled"
                assert row["reason_code"] == "interrupted"
                assert row["slot_held"] is False

                redelivered = await revival.decision.evaluate(
                    make_request(
                        identity=identity, target=make_message(identity=identity)
                    )
                )
                assert redelivered.status == "duplicate"
                assert revival.transport.requests == []
                assert revival.random_draws == 0
                assert (
                    await revival.decision.claim_handoff(
                        request, redelivered, "chat"
                    )
                    is False
                )
        finally:
            await admin.dispose()


# ------------------------------ 跨进程验收（真实效果区间） ------------------------------


def _configure_scratch(url: str, config: dict[str, Any]) -> None:
    async def _prepare() -> None:
        engine = await admin_engine(url)
        try:
            await reset_decision_storage(engine)
            await write_config_row(engine, overrides=config)
        finally:
            await engine.dispose()

    asyncio.run(_prepare())
    write_scratch_prompt_row(url)


def _run_workers(
    payloads: Sequence[dict[str, Any]], *, timeout: float
) -> list[dict[str, Any]]:
    context = multiprocessing.get_context("spawn")
    with context.Pool(processes=len(payloads)) as pool:
        try:
            pending = pool.map_async(spawn_worker, list(payloads), chunksize=1)
            outcomes: list[dict[str, Any]] = pending.get(timeout=timeout)
        finally:
            pool.terminate()
            pool.join()
    return outcomes


def test_same_event_competition_across_processes_has_single_winner(
    decision_database: str,
) -> None:
    """AC：真实多进程竞争同一事件只有一个胜者、只有一次模型请求。"""
    skip_without_services()
    config = {"max_concurrent_decisions": 4}
    _configure_scratch(decision_database, config)
    identity = make_identity()
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.dict()
        intervals = manager.list()
        events_log = manager.list()
        payloads = [
            worker_payload(
                decision_database,
                config=config,
                events=[(identity, str(uuid4()))],
                barrier=barrier,
                intervals=intervals,
                events_log=events_log,
                barrier_size=4,
                worker_id=str(index),
                hold_seconds=0.2,
            )
            for index in range(4)
        ]
        outcomes = _run_workers(payloads, timeout=180)
        logged = list(events_log)
        spans = [(item[1], item[2]) for item in list(intervals)]

    statuses = sorted(
        result["status"] for outcome in outcomes for result in outcome["results"]
    )
    assert statuses.count("success") == 1
    assert statuses.count("duplicate") == 3
    assert sum(1 for item in logged if item["kind"] == "sdk") == 1
    handoffs = [
        result["handoff"] for outcome in outcomes for result in outcome["results"]
    ]
    assert handoffs.count(True) == 1
    assert peak_overlap(spans) == 1


def test_deployment_capacity_is_shared_across_processes(
    decision_database: str,
) -> None:
    """AC：真实多进程同时执行（含引用补取）不超过 4 个，受控同载达到 4 个。"""
    skip_without_services()
    config = {
        "max_concurrent_decisions": 4,
        "max_queued_messages_per_group": 5,
    }
    _configure_scratch(decision_database, config)
    account_id = f"acct-{uuid4().hex[:8]}"
    groups = [f"group-{index}-{uuid4().hex[:6]}" for index in range(6)]
    events: list[tuple[EventIdentity, str]] = [
        (
            make_identity(
                account_id=account_id,
                group_id=group_id,
                message_id=f"m{index}-{slot}",
            ),
            str(uuid4()),
        )
        for index, group_id in enumerate(groups)
        for slot in range(2)
    ]
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.dict()
        intervals = manager.list()
        events_log = manager.list()
        payloads = [
            worker_payload(
                decision_database,
                config=config,
                events=events[index * 3 : (index + 1) * 3],
                barrier=barrier,
                intervals=intervals,
                events_log=events_log,
                barrier_size=4,
                worker_id=str(index),
                hold_seconds=0.15,
                reference_ids=(f"ref-{index}",),
            )
            for index in range(4)
        ]
        outcomes = _run_workers(payloads, timeout=240)
        logged = list(events_log)
        spans = [(item[1], item[2]) for item in list(intervals)]

    statuses = [
        result["status"] for outcome in outcomes for result in outcome["results"]
    ]
    assert statuses.count("success") == 12
    sdk_calls = sum(1 for item in logged if item["kind"] == "sdk")
    reference_calls = sum(1 for item in logged if item["kind"] == "reference")
    assert sdk_calls == 12
    assert reference_calls >= 12
    peak = peak_overlap(spans)
    assert peak <= 4
    assert peak == 4

    # 结束状态：全部完成且不再持有名额（补充观测，不是并发预言机）
    async def _final_state() -> tuple[dict[str, int], int]:
        engine = await admin_engine(decision_database)
        try:
            return (
                await count_requests_by_status(engine),
                await read_slot_rows(engine),
            )
        finally:
            await engine.dispose()

    states, slots = asyncio.run(_final_state())
    assert states == {"success": 12}
    assert slots == 0


def test_interval_overlap_oracle_counts_real_overlap() -> None:
    """AC：并发峰值由真实进入/退出区间计算，可区分重叠与串行。"""
    overlapping = [(0.0, 1.0), (0.5, 1.5), (2.0, 2.5)]
    serial = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
    assert peak_overlap(overlapping) == 2
    assert peak_overlap(serial) == 1
    assert peak_overlap([]) == 0
