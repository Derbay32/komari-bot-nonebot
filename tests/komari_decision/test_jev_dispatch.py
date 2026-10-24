"""TSK-187 分发载体验收：一次判定、冻结候选、结果保留与单次交接。

断言只穿过 ``komari_bot.decision.dispatch.DecisionDispatch`` 的公开面；引擎替身
只模拟外部判定边界（不复制引擎结果组合、交接或抽签逻辑）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from komari_bot.decision.dispatch import (
    DecisionDispatch,
    DecisionDispatchUnavailableError,
)
from komari_bot.decision.jev import (
    DecisionMessage,
    DecisionRequest,
    DecisionResult,
    EventIdentity,
)

pytestmark = pytest.mark.asyncio


def _request(
    *,
    text: str = "今天谁值班？",
    dispatch_id: str = "dispatch-1",
) -> DecisionRequest:
    identity = EventIdentity(
        protocol="onebot_v11",
        account_id="12345",
        group_id="100",
        message_id="777",
    )
    return DecisionRequest(
        event=identity,
        target=DecisionMessage(
            message_id="777",
            group_id="100",
            sender_id="67890",
            text=text,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        received_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        dispatch_id=dispatch_id,
    )


def _result(*, route: str = "chat", decision_id: str = "decision-1") -> DecisionResult:
    return DecisionResult(
        decision_id=decision_id,
        status="success",
        reason_code="classified",
        route=route,  # type: ignore[arg-type]
        force_reply=True,
        effective_reply="required",
        retain_recent=True,
        memory_candidate=True,
    )


class _FakeEngine:
    """只执行外部判定边界：记录调用、可选阻塞、记录交接。"""

    def __init__(self, *, result: DecisionResult | None = None) -> None:
        self.result = result or _result()
        self.evaluate_calls = 0
        self.observed_requests: list[DecisionRequest] = []
        self.claims: list[tuple[DecisionRequest, DecisionResult, str]] = []
        self.claim_result = True
        self.release = asyncio.Event()
        self.block = False

    async def evaluate(self, request: DecisionRequest) -> DecisionResult:
        self.evaluate_calls += 1
        self.observed_requests.append(request)
        if self.block:
            await self.release.wait()
        return self.result

    async def claim_handoff(
        self,
        request: DecisionRequest,
        result: DecisionResult,
        route: str,
    ) -> bool:
        self.claims.append((request, result, route))
        return self.claim_result

    async def observe_delivered(
        self,
        message: DecisionMessage,
        *,
        protocol: str,
        account_id: str,
        received_at: datetime,
    ) -> None:
        del message, protocol, account_id, received_at


async def test_first_evaluation_freezes_candidate_and_dispatch_id() -> None:
    engine = _FakeEngine()
    dispatch = DecisionDispatch(_request())

    result = await dispatch.evaluate(engine, summary_candidate=True)
    assert result is engine.result
    assert engine.evaluate_calls == 1
    assert dispatch.request.summary_candidate is True
    assert dispatch.request.dispatch_id == "dispatch-1"

    # 后来者即使给出不同候选标记也只读取第一次判定。
    again = await dispatch.evaluate(engine, summary_candidate=False)
    assert again is result
    assert engine.evaluate_calls == 1
    assert dispatch.request.summary_candidate is True
    assert engine.observed_requests[0] is dispatch.request
    assert dispatch.result is result


async def test_retained_result_survives_engine_cache_eviction() -> None:
    engine = _FakeEngine()
    dispatch = DecisionDispatch(_request())
    first = await dispatch.evaluate(engine, summary_candidate=False)

    # 引擎侧有界结果缓存被淘汰（模拟引擎丢弃自己的 dict 条目）。
    engine.result = _result(decision_id="decision-evicted")

    assert await dispatch.evaluate(engine, summary_candidate=False) is first
    assert engine.evaluate_calls == 1


async def test_cancelled_waiter_does_not_cancel_shared_evaluation() -> None:
    engine = _FakeEngine()
    engine.block = True
    dispatch = DecisionDispatch(_request())

    first_waiter = asyncio.create_task(
        dispatch.evaluate(engine, summary_candidate=True)
    )
    await asyncio.sleep(0.01)
    second_waiter = asyncio.create_task(
        dispatch.evaluate(engine, summary_candidate=False)
    )
    await asyncio.sleep(0.01)
    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter

    engine.release.set()
    result = await second_waiter

    assert result is engine.result
    assert engine.evaluate_calls == 1
    assert dispatch.result is result
    # 共享任务已结束，没有遗留未受控任务。
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("decision-dispatch-")
    ]


async def test_claim_handoff_delegates_exact_frozen_request_and_result() -> None:
    engine = _FakeEngine()
    dispatch = DecisionDispatch(_request())
    result = await dispatch.evaluate(engine, summary_candidate=False)

    claimed = await dispatch.claim_handoff("chat")

    assert claimed is True
    assert len(engine.claims) == 1
    request, claimed_result, route = engine.claims[0]
    assert request is dispatch.request
    assert claimed_result is result
    assert route == "chat"


async def test_claim_handoff_before_evaluation_never_invents_eligibility() -> None:
    engine = _FakeEngine()
    dispatch = DecisionDispatch(_request())

    assert await dispatch.claim_handoff("chat") is False
    assert engine.claims == []
    assert engine.evaluate_calls == 0


async def test_claim_handoff_propagates_engine_refusal() -> None:
    engine = _FakeEngine()
    engine.claim_result = False
    dispatch = DecisionDispatch(_request())
    await dispatch.evaluate(engine, summary_candidate=True)

    assert await dispatch.claim_handoff("summary") is False
    assert [route for _, _, route in engine.claims] == ["summary"]


async def test_unavailable_error_is_a_runtime_error() -> None:
    assert issubclass(DecisionDispatchUnavailableError, RuntimeError)
