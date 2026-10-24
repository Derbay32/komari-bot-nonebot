"""通过公开判定入口验证撤权只影响对应事件。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from tests.komari_decision.jev_test_support import (
    DecisionHarness,
    HarnessOptions,
    build_answers,
    create_scratch_database,
    drop_scratch_database,
    make_identity,
    make_message,
    make_request,
    read_dispatch,
    skip_without_services,
    wait_until,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx2

    from komari_bot.decision.jev import DecisionMessage, EventIdentity

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevrev{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


async def test_revoking_one_event_does_not_block_new_admitted_handoff(
    decision_database: str,
) -> None:
    rejected_groups: set[str] = set()

    async def reader(identity: EventIdentity, message_id: str) -> DecisionMessage:
        rejected_groups.add(identity.group_id)
        return make_message(identity=identity, message_id=message_id)

    options = HarnessOptions(
        reference_reader=reader,
        admission=lambda identity: identity.group_id not in rejected_groups,
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity()
        old = make_request(
            identity=identity,
            target=make_message(identity=identity, reference_ids=("quoted",)),
        )
        outcomes = await asyncio.gather(
            harness.decision.evaluate(old), return_exceptions=True
        )
        outcome = outcomes[0]
        if not isinstance(outcome, BaseException):
            assert outcome.status == "cancelled"
        else:
            assert isinstance(outcome, asyncio.CancelledError)

        fresh = make_request()
        result = await harness.decision.evaluate(fresh)
        assert result.status == "success"
        assert result.route == "chat"
        assert await harness.decision.claim_handoff(fresh, result, "chat") is True


async def test_restricted_platform_summon_never_enters_recent_input(
    decision_database: str,
) -> None:
    admitted = False
    options = HarnessOptions(admission=lambda _identity: admitted)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity()
        request = make_request(
            identity=identity,
            text="这段受限原文不能成为前文",
            real_platform_at=True,
        )
        denied = await harness.decision.evaluate(request)
        assert denied.status == "cancelled"
        assert denied.reason_code == "admission_revoked"
        assert harness.transport.requests == []
        admitted = True
        next_identity = make_identity(
            account_id=identity.account_id, group_id=identity.group_id
        )
        result = await harness.decision.evaluate(make_request(identity=next_identity))
        assert result.status == "success"
        assert "这段受限原文不能成为前文" not in str(harness.transport.state())


async def test_missing_reference_after_revocation_cannot_start_sdk(
    decision_database: str,
) -> None:
    admitted = True

    async def reader(_identity: EventIdentity, _message_id: str) -> None:
        nonlocal admitted
        admitted = False

    options = HarnessOptions(
        reference_reader=reader, admission=lambda _identity: admitted
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        identity = make_identity()
        request = make_request(
            identity=identity,
            target=make_message(identity=identity, reference_ids=("missing",)),
        )
        outcomes = await asyncio.gather(
            harness.decision.evaluate(request), return_exceptions=True
        )
        assert harness.reference_calls
        assert harness.transport.requests == []
        outcome = outcomes[0]
        if not isinstance(outcome, BaseException):
            assert outcome.status == "cancelled"
        else:
            assert isinstance(outcome, asyncio.CancelledError)


async def test_live_pending_message_survives_other_delivery_and_late_noise(
    decision_database: str,
) -> None:
    gate = asyncio.Event()

    async def responder(request: httpx2.Request, index: int) -> dict[str, Any]:
        if index == 0:
            await gate.wait()
        return build_answers(
            json.loads(request.content)["questions"],
            reply="silent" if index == 0 else "required",
            retention="noise" if index == 0 else "candidate",
        )

    options = HarnessOptions(responder=responder)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        first_identity = make_identity()
        old_text = "仍在判定中的前文必须出现在随后冻结的输入里"
        first = make_request(
            identity=first_identity,
            target=make_message(
                identity=first_identity,
                text=old_text,
                timestamp=datetime.now(UTC) - timedelta(seconds=60),
            ),
            received_at=datetime.now(UTC),
        )
        running = asyncio.create_task(harness.decision.evaluate(first))
        following: asyncio.Task | None = None
        try:
            await wait_until(lambda: bool(harness.transport.requests))
            await harness.decision.observe_delivered(
                make_message(
                    identity=first_identity,
                    message_id="confirmed-bot-message",
                    text="另一条已经送达的回复",
                    is_bot=True,
                ),
                protocol=first_identity.protocol,
                account_id=first_identity.account_id,
                received_at=datetime.now(UTC),
            )
            next_identity = make_identity(
                account_id=first_identity.account_id,
                group_id=first_identity.group_id,
            )
            following = asyncio.create_task(
                harness.decision.evaluate(make_request(identity=next_identity))
            )
            async with asyncio.timeout(5):
                while True:
                    row = await read_dispatch(harness.engine, next_identity)
                    if row is not None and row["state"] == "queued":
                        break
                    await asyncio.sleep(0.01)
            gate.set()
            assert (await running).status == "success"
            assert (await following).status == "success"
            assert old_text in str(harness.transport.state(1))
        finally:
            gate.set()
            tasks = [running] if following is None else [running, following]
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_retained_completed_result_observes_admission_denial(
    decision_database: str,
) -> None:
    """AC：本地保留的已完成结果也要观察撤权；恢复准入后仍不可交接。"""
    admitted = {"value": True}

    def admission(identity: EventIdentity) -> bool:
        del identity
        return admitted["value"]

    options = HarnessOptions(admission=admission)
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None and harness.engine is not None
        identity = make_identity()
        request = make_request(identity=identity)
        result = await harness.decision.evaluate(request)
        assert result.status == "success"
        assert result.route == "chat"

        # 观察者（不是本用例）先看到撤权：持久撤销 + 本地墓碑
        admitted["value"] = False
        deadline = asyncio.get_running_loop().time() + 10
        row = None
        while asyncio.get_running_loop().time() < deadline:
            row = await read_dispatch(harness.engine, identity)
            if row is not None and row["state"] == "cancelled":
                break
            await asyncio.sleep(0.02)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"

        # 恢复准入后旧结果仍然不可交接
        admitted["value"] = True
        assert await harness.decision.claim_handoff(request, result, "chat") is False

        # 与之无关的新事件仍然正常
        fresh = make_request()
        fresh_result = await harness.decision.evaluate(fresh)
        assert fresh_result.status == "success"
        assert await harness.decision.claim_handoff(fresh, fresh_result, "chat") is True
