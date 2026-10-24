"""TSK-182 引擎修正验收 A/B/F：输入定案、早期失败身份与实测诊断。

全部断言穿过公开判定入口与外部效果观测（SDK transport、引用读取、持久行、
健康快照）。Redis 故障注入只替换**外部存储端点**，不改动引擎内部。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx2
import pytest

from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    FailingRedis,
    HarnessOptions,
    admin_engine,
    build_answers,
    create_scratch_database,
    delete_prompt,
    drop_scratch_database,
    kill_connections,
    make_identity,
    make_message,
    make_request,
    read_dispatch,
    skip_without_services,
    wait_until,
    write_prompt,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevset{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


async def _second_harness(database_url: str) -> DecisionHarness:
    """同一数据库上的新实例（不重置数据，自己写回 Prompt 行）。"""
    return DecisionHarness(
        database_url,
        HarnessOptions(
            reset_storage=False,
            write_prompt_row=True,
            write_config_row=False,
        ),
    )


# ------------------------------ A. 事实定案 ------------------------------


async def test_interrupted_eligible_message_is_preserved_by_next_ingress(
    decision_database: str,
) -> None:
    """AC：中断的合格原文由下一次入口按持久事实确认，不依赖时间推断。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
            await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    old_text = "被中断但仍合格的前文必须保留"
    identity = make_identity()
    # 崩溃实例不清理 Redis 作用域：下一步的新实例要在同一窗口上观察定案结果
    async with DecisionHarness(
        decision_database,
        HarnessOptions(responder=responder, clean_redis_scopes=False),
    ) as crashed:
        assert crashed.decision is not None
        request = make_request(
            identity=identity,
            target=make_message(identity=identity, text=old_text),
        )
        pending = asyncio.create_task(crashed.decision.evaluate(request))
        await wait_until(lambda: bool(crashed.transport.requests))

        admin = await admin_engine(decision_database)
        try:
            assert await kill_connections(admin, crashed.application_name) > 0
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=15)
            row = await read_dispatch(admin, identity)
            assert row is not None
            assert row["state"] == "running"
        finally:
            await admin.dispose()

    async with await _second_harness(decision_database) as revival:
        assert revival.decision is not None and revival.engine is not None
        row = await read_dispatch(revival.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "interrupted"
        assert row["slot_held"] is False

        follower = make_identity(
            account_id=identity.account_id, group_id=identity.group_id
        )
        result = await revival.decision.evaluate(make_request(identity=follower))
        assert result.status == "success"
        assert old_text in str(revival.transport.state())


async def test_successful_noise_settles_from_persisted_boolean_after_redis_failure(
    decision_database: str,
) -> None:
    """AC：SQL 已成功、Redis 定案失败时，下一次入口仅凭持久布尔值移除噪声。"""
    holder: dict[str, FailingRedis] = {}

    def wrap(client: Any) -> Any:
        holder["redis"] = FailingRedis(client)
        return holder["redis"]

    noise_text = "被判定为噪声且 Redis 定案曾失败的正文"

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
            holder["redis"].arms()  # 让紧随其后的 SQL 成功之后的 Redis 定案失败
        return build_answers(
            json.loads(request.content)["questions"],
            reply="silent",
            retention="noise",
        )

    options = HarnessOptions(responder=responder, redis_wrapper=wrap)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=make_message(identity=identity, text=noise_text),
            )
        )
        assert result.status == "success"
        assert result.retain_recent is False
        assert holder["redis"].failed_evals == 1

        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["recent_retained"] is False

        follower = make_identity(
            account_id=identity.account_id, group_id=identity.group_id
        )
        following = await harness.decision.evaluate(make_request(identity=follower))
        assert following.status == "success"
        assert noise_text not in str(harness.transport.state(1))


async def test_successful_candidate_settles_from_persisted_boolean_after_redis_failure(
    decision_database: str,
) -> None:
    """AC：SQL 已成功、Redis 定案失败时，下一次入口按持久布尔值确认合格原文。"""
    holder: dict[str, FailingRedis] = {}

    def wrap(client: Any) -> Any:
        holder["redis"] = FailingRedis(client)
        return holder["redis"]

    kept_text = "被判定为候选且 Redis 定案曾失败的正文"

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
            holder["redis"].arms()
        return build_answers(
            json.loads(request.content)["questions"],
            reply="required",
            retention="candidate",
        )

    options = HarnessOptions(responder=responder, redis_wrapper=wrap)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=make_message(identity=identity, text=kept_text),
            )
        )
        assert result.retain_recent is True
        assert holder["redis"].failed_evals == 1
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["recent_retained"] is True

        follower = make_identity(
            account_id=identity.account_id, group_id=identity.group_id
        )
        following = await harness.decision.evaluate(make_request(identity=follower))
        assert following.status == "success"
        assert kept_text in str(harness.transport.state(1))


# ------------------------------ B. 早期失败保留资格 ------------------------------


async def test_queue_full_summary_candidate_gets_one_qualification_and_keeps_input(
    decision_database: str,
) -> None:
    """AC：总结候选队列满时保留合格原文，并且只给一次失败通知资格。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
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
        assert harness.decision is not None and harness.engine is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"

        def request_for(message_id: str, text: str) -> Any:
            identity = make_identity(
                account_id=account_id, group_id=group_id, message_id=message_id
            )
            return make_request(
                identity=identity,
                target=make_message(identity=identity, text=text),
                summary_candidate=True,
            )

        first = request_for("m1", "占用名额的消息")
        running = asyncio.create_task(harness.decision.evaluate(first))
        await wait_until(lambda: bool(harness.transport.requests))

        waiting = request_for("m2", "排队等待的总结候选")
        second = asyncio.create_task(harness.decision.evaluate(waiting))
        row = None
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            row = await read_dispatch(harness.engine, waiting.event)
            if row is not None and row["state"] == "queued":
                break
            await asyncio.sleep(0.01)
        assert row is not None

        refused_text = "队列满但通过过滤的总结候选正文"
        refused = request_for("m3", refused_text)
        refused_result = await harness.decision.evaluate(refused)
        assert refused_result.status == "failed"
        assert refused_result.reason_code == "queue_full"
        assert await harness.decision.claim_handoff(refused, refused_result, "chat") is False
        assert (
            await harness.decision.claim_handoff(refused, refused_result, "summary")
            is False
        )
        assert (
            await harness.decision.claim_handoff(
                refused, refused_result, "failure_notification"
            )
            is True
        )
        assert (
            await harness.decision.claim_handoff(
                refused, refused_result, "failure_notification"
            )
            is False
        )
        health = harness.decision.get_health()
        assert health.last_request_status == "unknown"
        assert health.consecutive_failures == 0

        gate.set()
        await asyncio.gather(running, second)

        follower = make_identity(
            account_id=account_id, group_id=group_id, message_id="m4"
        )
        following = await harness.decision.evaluate(make_request(identity=follower))
        assert following.status == "success"
        assert refused_text in str(harness.transport.state(-1))


async def test_queue_full_ordinary_message_keeps_input_without_notification(
    decision_database: str,
) -> None:
    """AC：普通消息队列满时保留合格原文，但不取得失败通知资格。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
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
        assert harness.decision is not None and harness.engine is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"

        def request_for(message_id: str, text: str) -> Any:
            identity = make_identity(
                account_id=account_id, group_id=group_id, message_id=message_id
            )
            return make_request(
                identity=identity,
                target=make_message(identity=identity, text=text),
            )

        running = asyncio.create_task(harness.decision.evaluate(request_for("m1", "占用")))
        await wait_until(lambda: bool(harness.transport.requests))
        second = asyncio.create_task(
            harness.decision.evaluate(request_for("m2", "等待中的普通消息"))
        )
        await asyncio.sleep(0.1)

        refused_text = "队列满但合格保留的普通正文"
        refused = request_for("m3", refused_text)
        refused_result = await harness.decision.evaluate(refused)
        assert refused_result.reason_code == "queue_full"
        assert (
            await harness.decision.claim_handoff(
                refused, refused_result, "failure_notification"
            )
            is False
        )

        gate.set()
        await asyncio.gather(running, second)
        follower = make_identity(
            account_id=account_id, group_id=group_id, message_id="m4"
        )
        following = await harness.decision.evaluate(make_request(identity=follower))
        assert following.status == "success"
        assert refused_text in str(harness.transport.state(-1))


async def test_missing_prompt_keeps_established_input_eligibility(
    decision_database: str,
) -> None:
    """AC：Prompt 缺失只影响执行受理，已成立的输入资格仍保留。"""
    kept_text = "Prompt 缺失时仍需保留的合格正文"
    options = HarnessOptions(write_prompt_row=False)
    try:
        async with DecisionHarness(decision_database, options) as harness:
            assert harness.decision is not None and harness.engine is not None
            account_id = f"acct-{uuid4().hex[:8]}"
            group_id = f"group-{uuid4().hex[:8]}"
            identity = make_identity(
                account_id=account_id, group_id=group_id, message_id="m1"
            )
            result = await harness.decision.evaluate(
                make_request(
                    identity=identity,
                    target=make_message(identity=identity, text=kept_text),
                )
            )
            assert result.status == "failed"
            assert result.reason_code == "prompt_unavailable"
            assert harness.transport.requests == []

            await write_prompt()
            follower = make_identity(
                account_id=account_id, group_id=group_id, message_id="m2"
            )
            following = await harness.decision.evaluate(make_request(identity=follower))
            assert following.status == "success"
            assert kept_text in str(harness.transport.state())
    finally:
        await delete_prompt()


async def test_input_window_failure_commits_identity_and_blocks_redelivery(
    decision_database: str,
) -> None:
    """AC：输入快照失败也要留下身份，且不得回滚或发起 SDK 调用。"""
    holder: dict[str, FailingRedis] = {}

    def wrap(client: Any) -> Any:
        holder["redis"] = FailingRedis(client)
        return holder["redis"]

    async with DecisionHarness(
        decision_database, HarnessOptions(redis_wrapper=wrap)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        holder["redis"].arms()

        request = make_request()
        result = await harness.decision.evaluate(request)
        assert result.status == "failed"
        assert result.reason_code == "input_unavailable"
        assert harness.transport.requests == []
        assert holder["redis"].failed_evals == 1

        row = await read_dispatch(harness.engine, request.event)
        assert row is not None
        assert row["state"] == "failed"
        assert row["reason_code"] == "input_unavailable"
        assert row["slot_held"] is False
        assert row["business_route"] == "none"
        assert (
            await harness.decision.claim_handoff(
                request, result, "failure_notification"
            )
            is True
        )

        redelivered = await harness.decision.evaluate(
            make_request(
                identity=request.event,
                target=make_message(identity=request.event),
                dispatch_id=str(uuid4()),
            )
        )
        assert redelivered.status == "duplicate"
        assert harness.transport.requests == []


# ------------------------------ C. 锁内复检与粘性撤权 ------------------------------


async def test_admission_flip_after_entrance_check_commits_minimal_identity(
    decision_database: str,
) -> None:
    """AC：入口检查通过后锁内复检拒绝时，只提交最小 cancelled 身份。"""
    calls = {"n": 0}

    def admission(_identity: Any) -> bool:
        calls["n"] += 1
        return calls["n"] <= 1

    denied_text = "锁内复检拒绝时不得缓存正文"
    async with DecisionHarness(
        decision_database, HarnessOptions(admission=admission)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=make_message(identity=identity, text=denied_text),
            )
        )
        assert result.status == "cancelled"
        assert result.reason_code == "admission_revoked"
        assert harness.transport.requests == []
        assert harness.reference_calls == []

        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert row["slot_held"] is False
        assert row["business_route"] == "none"

        from komari_bot.plugins.komari_decision.services.jev_recent import (
            DecisionRecentStore,
        )

        assert harness.redis is not None
        assert (
            await DecisionRecentStore(harness.redis).window(
                protocol=identity.protocol,
                account_id=identity.account_id,
                group_id=identity.group_id,
            )
            == ()
        )


async def test_claim_side_admission_check_persists_revocation(
    decision_database: str,
) -> None:
    """AC：锁内交接复核拒绝时持久化撤销；恢复准入后重复领取仍然失败。"""
    mode = {"value": "allow"}

    def admission(_identity: Any) -> bool:
        if mode["value"] == "allow":
            return True
        if mode["value"] == "deny_after_precheck":
            mode["value"] = "deny"
            return True
        return False

    async with DecisionHarness(
        decision_database, HarnessOptions(admission=admission)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(identity=identity)
        result = await harness.decision.evaluate(request)
        assert result.status == "success"

        mode["value"] = "deny_after_precheck"
        assert await harness.decision.claim_handoff(request, result, "chat") is False
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert row["handed_off_at"] is None

        mode["value"] = "allow"
        assert await harness.decision.claim_handoff(request, result, "chat") is False


async def test_claim_side_revocation_also_covers_failure_notification(
    decision_database: str,
) -> None:
    """AC：失败通知的锁内复核同样持久化撤销且不可复活。"""
    mode = {"value": "allow"}

    def admission(_identity: Any) -> bool:
        if mode["value"] == "allow":
            return True
        if mode["value"] == "deny_after_precheck":
            mode["value"] = "deny"
            return True
        return False

    def responder(request: httpx2.Request, _index: int) -> httpx2.Response:
        return httpx2.Response(
            500, json={"error": "synthetic-upstream-failure"}, request=request
        )

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder, admission=admission)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        request = make_request()
        result = await harness.decision.evaluate(request)
        assert result.status == "failed"
        assert result.reason_code == "provider_error"

        mode["value"] = "deny_after_precheck"
        assert (
            await harness.decision.claim_handoff(
                request, result, "failure_notification"
            )
            is False
        )
        row = await read_dispatch(harness.engine, request.event)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"
        assert row["notified_at"] is None

        mode["value"] = "allow"
        assert (
            await harness.decision.claim_handoff(
                request, result, "failure_notification"
            )
            is False
        )


# ------------------------------ F. 实测诊断 ------------------------------


async def test_queue_wait_is_measured_from_persisted_timestamps(
    decision_database: str,
) -> None:
    """AC：排队时长来自持久 enqueued_at/granted_at，而不是调用前的占位值。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
            await gate.wait()
        return build_answers(json.loads(request.content)["questions"])

    options = HarnessOptions(
        responder=responder, config={"max_concurrent_decisions": 1}
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        running = asyncio.create_task(harness.decision.evaluate(make_request()))
        await wait_until(lambda: bool(harness.transport.requests))

        follower_identity = make_identity()
        follower = asyncio.create_task(
            harness.decision.evaluate(make_request(identity=follower_identity))
        )
        await asyncio.sleep(0.3)
        gate.set()
        await running
        result = await follower

        assert result.status == "success"
        assert result.durations.queue_ms is not None
        assert result.durations.queue_ms >= 250.0


async def test_request_failure_measures_request_phase_but_configuration_does_not(
    decision_database: str,
) -> None:
    """AC：真实开始的请求记录 request_ms；创建客户端前被拒的配置失败不记录。"""

    def responder(request: httpx2.Request, _index: int) -> httpx2.Response:
        return httpx2.Response(
            500, json={"error": "synthetic-upstream-failure"}, request=request
        )

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None
        failed = await harness.decision.evaluate(make_request())
        assert failed.status == "failed"
        assert failed.reason_code == "provider_error"
        assert failed.durations.request_ms is not None
        assert failed.durations.queue_ms is not None

    rejected_options = HarnessOptions(config={"jev_api_key": ""})
    async with DecisionHarness(decision_database, rejected_options) as harness:
        assert harness.decision is not None
        rejected = await harness.decision.evaluate(make_request())
        assert rejected.reason_code == "configuration_invalid"
        assert harness.transport.requests == []
        assert rejected.durations.request_ms is None


async def test_health_observed_at_only_moves_on_new_observation(
    decision_database: str,
) -> None:
    """AC：没有新的观测时，重复健康查询返回同一时间且不产生 I/O。"""
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None
        request = make_request()
        result = await harness.decision.evaluate(request)
        assert result.status == "success"

        first = harness.decision.get_health()
        requests_before = len(harness.transport.requests)
        await asyncio.sleep(0.05)
        second = harness.decision.get_health()
        assert second.observed_at == first.observed_at
        assert len(harness.transport.requests) == requests_before

        await harness.decision.evaluate(make_request())
        third = harness.decision.get_health()
        assert third.observed_at > first.observed_at


async def test_old_but_live_pending_message_is_not_settled(
    decision_database: str,
) -> None:
    """AC：仍在执行的旧消息无论平台时间多旧都保持未定案（同作用域入口定案）。"""
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        del index
        body = json.loads(request.content)
        conversation = body["state"]["untrusted_group_conversation"]
        target_text = conversation["messages"][conversation["target"]]["text"]
        if target_text.startswith("仍在执行"):
            await gate.wait()
        return build_answers(body["questions"])

    async with DecisionHarness(
        decision_database, HarnessOptions(responder=responder)
    ) as harness:
        assert harness.decision is not None and harness.engine is not None
        account_id = f"acct-{uuid4().hex[:8]}"
        group_id = f"group-{uuid4().hex[:8]}"
        old_text = "仍在执行的旧消息必须保留为未定案前文"
        old_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m1"
        )
        running = asyncio.create_task(
            harness.decision.evaluate(
                make_request(
                    identity=old_identity,
                    target=make_message(
                        identity=old_identity,
                        text=old_text,
                        timestamp=datetime.now(UTC) - timedelta(hours=2),
                    ),
                    received_at=datetime.now(UTC),
                )
            )
        )
        await wait_until(lambda: bool(harness.transport.requests))

        # 同群的确定性召唤不占名额：它的入口定案可以在 A 仍在执行时发生
        summon_identity = make_identity(
            account_id=account_id, group_id=group_id, message_id="m2"
        )
        summon = await harness.decision.evaluate(
            make_request(
                identity=summon_identity,
                target=make_message(identity=summon_identity, text="在吗"),
                real_platform_at=True,
            )
        )
        assert summon.status == "bypass"

        from komari_bot.plugins.komari_decision.services.jev_recent import (
            DecisionRecentStore,
        )

        assert harness.redis is not None
        store = DecisionRecentStore(harness.redis)
        pending = await store.provisional_ids(
            protocol=old_identity.protocol,
            account_id=account_id,
            group_id=group_id,
        )
        # 老而活的 pending 记录绝不被时间推断定案
        assert old_identity.message_id in pending
        texts = [
            message.text
            for message in await store.window(
                protocol=old_identity.protocol,
                account_id=account_id,
                group_id=group_id,
            )
        ]
        assert old_text in texts

        gate.set()
        running_result = await running
        assert running_result.status == "success", running_result.reason_code


async def test_success_without_settlement_boolean_fails_closed(
    decision_database: str,
) -> None:
    """AC：成功行缺少缓存处置布尔值时失败关闭：不猜分类、保持未定案。"""
    from sqlalchemy import text

    from komari_bot.plugins.komari_decision.services.jev_recent import (
        DecisionRecentStore,
    )

    account_id = f"acct-{uuid4().hex[:8]}"
    group_id = f"group-{uuid4().hex[:8]}"
    orphan_text = "缺少处置布尔值的成功行正文"
    async with DecisionHarness(decision_database) as harness:
        assert harness.decision is not None and harness.engine is not None
        store = DecisionRecentStore(harness.redis)  # type: ignore[arg-type]
        orphan = make_message(
            group_id=group_id, message_id="orphan-1", text=orphan_text
        )
        # 该条目以 provisional 存在（Redis 侧未定案）
        await store.freeze_for_target(
            protocol="onebot_v11",
            account_id=account_id,
            target=orphan,
            cutoff=orphan.timestamp + timedelta(seconds=5),
        )
        # 手工构造「成功但缺处置布尔值」的一致性问题行
        async with harness.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO komari_decision_dispatches ("
                    "decision_id, protocol, account_id, group_id, message_id,"
                    " owner_process_id, dispatch_id, generation, state, reason_code,"
                    " enqueued_at, queue_deadline, finished_at, slot_held,"
                    " business_route, notification_eligible, recent_retained)"
                    " SELECT gen_random_uuid(), :protocol, :account_id, :group_id,"
                    " :message_id, id, gen_random_uuid(), 0, 'success', 'silent',"
                    " now(), now(), now(), false, 'none', false, NULL"
                    " FROM komari_decision_processes"
                    " WHERE stopped_at IS NULL ORDER BY id DESC LIMIT 1"
                ),
                {
                    "protocol": "onebot_v11",
                    "account_id": account_id,
                    "group_id": group_id,
                    "message_id": orphan.message_id,
                },
            )
        inserted = await read_dispatch(
            harness.engine,
            make_identity(
                protocol="onebot_v11",
                account_id=account_id,
                group_id=group_id,
                message_id=orphan.message_id,
            ),
        )
        assert inserted is not None, "成功行未写入（一致性问题夹具失败）"
        assert inserted["state"] == "success"
        assert inserted["recent_retained"] is None

        follower = make_identity(
            account_id=account_id, group_id=group_id, message_id="m-next"
        )
        result = await harness.decision.evaluate(make_request(identity=follower))
        assert result.status == "success"

        pending = await store.provisional_ids(
            protocol="onebot_v11", account_id=account_id, group_id=group_id
        )
        # 一致性缺口不猜分类：该条目既不被确认也不被移除
        assert orphan.message_id in pending
