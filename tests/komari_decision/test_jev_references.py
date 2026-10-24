"""TSK-182 判定引擎验收：显式引用链的补取、复用、缺失、循环与截止。

断言穿过 ``evaluate`` 与官方 SDK transport：只看模型实际收到的 state、返回
结果中的引用完整性事实、外部引用读取次数与效果是否发生。
"""

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
    create_scratch_database,
    drop_scratch_database,
    make_identity,
    make_message,
    make_request,
    read_dispatch,
    skip_without_services,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from komari_bot.decision.jev import DecisionMessage, EventIdentity

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def decision_database() -> Iterator[str]:
    skip_without_services()
    url = asyncio.run(create_scratch_database(f"_jevref{uuid4().hex[:8]}"))
    yield url
    asyncio.run(drop_scratch_database(url))


def _reference_reader(messages: dict[str, DecisionMessage]):
    async def reader(identity: EventIdentity, message_id: str) -> DecisionMessage | None:
        del identity
        return messages.get(message_id)

    return reader


def _conversation(state: dict[str, Any]) -> dict[str, Any]:
    return state["untrusted_group_conversation"]


async def test_multi_level_reference_chain_is_followed_and_serialized_once(
    decision_database: str,
) -> None:
    """AC：多层引用原文可见，重叠正文不重复序列化。"""
    group_id = f"group-{uuid4().hex[:8]}"
    account_id = f"acct-{uuid4().hex[:8]}"
    outer = make_message(group_id=group_id, message_id="ref-1", text="被引用的第一条原文")
    inner = make_message(group_id=group_id, message_id="ref-2", text="更深一层的原文")
    reader_messages = {
        "ref-1": make_message(
            group_id=group_id,
            message_id="ref-1",
            text="被引用的第一条原文",
            reference_ids=("ref-2",),
        ),
        "ref-2": inner,
    }
    identity = make_identity(account_id=account_id, group_id=group_id, message_id="m1")
    target = make_message(identity=identity, reference_ids=("ref-1", "ref-2"))
    options = HarnessOptions(reference_reader=_reference_reader(reader_messages))

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )
        assert result.status == "success"
        assert result.references.complete is True
        assert result.references.resolved_count == 2
        assert result.references.missing_count == 0

        state = harness.transport.state(0)
        rendered = json.dumps(state, ensure_ascii=False)
        assert rendered.count("被引用的第一条原文") == 1
        assert rendered.count("更深一层的原文") == 1
        assert len(harness.transport.requests) == 1
        del outer


async def test_overlapping_reference_is_reused_without_fetch(
    decision_database: str,
) -> None:
    """AC：引用节点与近期窗口重叠时复用身份与正文，不再补取。"""
    group_id = f"group-{uuid4().hex[:8]}"
    account_id = f"acct-{uuid4().hex[:8]}"
    recent_identity = make_identity(
        account_id=account_id, group_id=group_id, message_id="recent-1"
    )
    known = make_message(
        group_id=group_id, message_id="known-1", text="调用方自带引用原文"
    )
    identity = make_identity(account_id=account_id, group_id=group_id, message_id="m2")
    target = make_message(
        identity=identity, reference_ids=("recent-1", "known-1")
    )
    options = HarnessOptions(reference_reader=_reference_reader({}))

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        first = await harness.decision.evaluate(
            make_request(
                identity=recent_identity,
                target=make_message(identity=recent_identity, text="近期窗口里的原文"),
            )
        )
        assert first.status == "success"
        harness.reference_calls.clear()

        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target, known_references=(known,))
        )
        assert result.status == "success"
        assert harness.reference_calls == []
        assert result.references.missing_count == 0

        rendered = json.dumps(harness.transport.state(1), ensure_ascii=False)
        assert rendered.count("近期窗口里的原文") == 1
        assert rendered.count("调用方自带引用原文") == 1


async def test_cycle_terminates_and_is_reported(decision_database: str) -> None:
    """AC：循环引用终止受影响路径，并留下可观测的不完整标记。"""
    group_id = f"group-{uuid4().hex[:8]}"
    account_id = f"acct-{uuid4().hex[:8]}"
    identity = make_identity(account_id=account_id, group_id=group_id, message_id="m1")
    target = make_message(identity=identity, reference_ids=("cycle-a",))
    reader_messages = {
        "cycle-a": make_message(
            group_id=group_id,
            message_id="cycle-a",
            text="循环里的第一条",
            reference_ids=("m1",),
        ),
    }
    options = HarnessOptions(reference_reader=_reference_reader(reader_messages))

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )
        assert result.status == "success"
        assert result.references.complete is False
        assert result.references.cycle_count >= 1
        assert len(harness.transport.requests) == 1

        conversation = _conversation(harness.transport.state(0))
        status = conversation["reference_status"]
        assert status["complete"] is False
        assert status["cyclic"] >= 1
        assert status["timed_out"] is False


async def test_missing_reference_is_marked_without_inventing_text(
    decision_database: str,
) -> None:
    """AC：缺失引用明确标记，不虚构原文。"""
    group_id = f"group-{uuid4().hex[:8]}"
    identity = make_identity(group_id=group_id, message_id="m1")
    target = make_message(identity=identity, reference_ids=("gone-1",))
    options = HarnessOptions(reference_reader=_reference_reader({}))

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )
        assert result.references.complete is False
        assert result.references.missing_count == 1
        assert result.references.resolved_count == 0

        state = harness.transport.state(0)
        conversation = _conversation(state)
        assert conversation["reference_status"]["missing"]
        rendered = json.dumps(state, ensure_ascii=False)
        assert "gone-1" not in rendered
        assert len(harness.transport.requests) == 1


@pytest.mark.parametrize("invalidation", ["cross_group", "future", "mismatched"])
async def test_invalid_reference_nodes_are_rejected_without_body(
    decision_database: str, invalidation: str
) -> None:
    """AC：跨群、未来与身份不符的返回节点一律丢弃正文。"""
    group_id = f"group-{uuid4().hex[:8]}"
    identity = make_identity(group_id=group_id, message_id="m1")
    target = make_message(identity=identity, reference_ids=("bad-1",))
    now = datetime.now(UTC)
    returned = {
        "cross_group": make_message(
            group_id=f"group-{uuid4().hex[:8]}",
            message_id="bad-1",
            text="不该出现的跨群正文",
        ),
        "future": make_message(
            group_id=group_id,
            message_id="bad-1",
            text="不该出现的未来正文",
            timestamp=now + timedelta(minutes=5),
        ),
        "mismatched": make_message(
            group_id=group_id,
            message_id="other-9",
            text="不该出现的错配正文",
        ),
    }[invalidation]
    options = HarnessOptions(
        reference_reader=_reference_reader({"bad-1": returned})
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target, received_at=now)
        )
        assert result.references.complete is False
        assert result.references.missing_count == 1
        rendered = json.dumps(harness.transport.state(0), ensure_ascii=False)
        assert "不该出现的" not in rendered


async def test_whole_chain_shares_one_deadline_and_keeps_partial_state(
    decision_database: str,
) -> None:
    """AC：整链共享一个截止；超时用已取得内容与不完整标记继续一次判定。"""
    group_id = f"group-{uuid4().hex[:8]}"
    identity = make_identity(group_id=group_id, message_id="m1")
    target = make_message(identity=identity, reference_ids=("slow-a",))
    fast = make_message(
        group_id=group_id,
        message_id="slow-a",
        text="及时取得的引用原文",
        reference_ids=("slow-b",),
    )

    async def reader(
        _identity: EventIdentity, message_id: str
    ) -> DecisionMessage | None:
        if message_id == "slow-a":
            return fast
        await asyncio.sleep(0.3)  # 超过整链截止
        return make_message(
            group_id=group_id, message_id="slow-b", text="迟到的引用正文"
        )

    options = HarnessOptions(
        reference_reader=reader,
        config={"reference_timeout_seconds": 0.05},
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )

        assert result.status == "success"  # 引用超时不是 Jev 失败
        assert result.references.timed_out is True
        assert result.references.complete is False
        assert len(harness.transport.requests) == 1
        state = harness.transport.state(0)
        rendered = json.dumps(state, ensure_ascii=False)
        assert "及时取得的引用原文" in rendered
        assert "迟到的引用正文" not in rendered
        assert _conversation(state)["reference_status"]["timed_out"] is True


async def test_admission_revocation_cancels_event_without_model_call(
    decision_database: str,
) -> None:
    """AC：撤权不降级为引用缺失，整事件取消且不发起 SDK 请求。"""
    group_id = f"group-{uuid4().hex[:8]}"
    identity = make_identity(group_id=group_id, message_id="m1")
    target = make_message(identity=identity, reference_ids=("ref-1",))
    admissions = {"count": 0}

    def admission(_identity: EventIdentity) -> bool:
        admissions["count"] += 1
        # 第 1 次：任务入口允许；第 2 次（远程读取前）仍允许；第 3 次（使用结果前）撤销
        return admissions["count"] < 3

    options = HarnessOptions(
        reference_reader=_reference_reader(
            {
                "ref-1": make_message(
                    group_id=group_id, message_id="ref-1", text="引用原文"
                )
            }
        ),
        admission=admission,
    )
    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )
        assert result.status == "cancelled"
        assert result.reason_code == "admission_revoked"
        assert harness.transport.requests == []
        assert harness.engine is not None
        row = await read_dispatch(harness.engine, identity)
        assert row is not None
        assert row["state"] == "cancelled"
        assert row["reason_code"] == "admission_revoked"


async def test_filtered_message_can_be_a_reference_without_being_judged_again(
    decision_database: str,
) -> None:
    """AC：已过滤 A 可作为 B 的引用原文，但不会重新成为判定目标。"""
    group_id = f"group-{uuid4().hex[:8]}"
    account_id = f"acct-{uuid4().hex[:8]}"
    long_text = "".join(chr(0x4E00 + index) for index in range(80))
    long_reference = make_message(
        group_id=group_id, message_id="filtered-a", text=long_text
    )
    filtered_identity = make_identity(
        account_id=account_id, group_id=group_id, message_id="a-1"
    )
    identity = make_identity(account_id=account_id, group_id=group_id, message_id="b-1")
    filtered_target = make_message(identity=filtered_identity, text=long_text)
    target = make_message(identity=identity, reference_ids=("filtered-a",))
    options = HarnessOptions(
        reference_reader=_reference_reader({"filtered-a": long_reference})
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        filtered = await harness.decision.evaluate(
            make_request(identity=filtered_identity, target=filtered_target)
        )
        assert filtered.status == "filtered"
        assert harness.transport.requests == []

        result = await harness.decision.evaluate(
            make_request(identity=identity, target=target)
        )
        assert result.status == "success"
        assert len(harness.transport.requests) == 1
        rendered = json.dumps(harness.transport.state(0), ensure_ascii=False)
        assert long_text in rendered
        # A 只被判定一次（首次即 filtered），其后不再成为目标
        assert harness.engine is not None
        assert await read_dispatch(harness.engine, filtered_identity) is not None
        assert len(harness.transport.requests) == 1


async def test_unreached_known_reference_never_reaches_the_model(
    decision_database: str,
) -> None:
    """AC：调用方自带的无关原文没有显式引用边触达时不得进入 state。"""
    group_id = f"group-{uuid4().hex[:8]}"
    account_id = f"acct-{uuid4().hex[:8]}"
    identity = make_identity(
        account_id=account_id, group_id=group_id, message_id="m1"
    )
    target = make_message(identity=identity, text="只引用 reached-1")
    reached = make_message(
        group_id=group_id, message_id="reached-1", text="被引用的正文"
    )
    unrelated = make_message(
        group_id=group_id, message_id="unrelated-9", text="无关机密标记-MARKER-42"
    )
    target = make_message(
        identity=identity,
        text="只引用 reached-1",
        reference_ids=("reached-1",),
    )
    options = HarnessOptions(
        reference_reader=_reference_reader({"reached-1": reached})
    )

    async with DecisionHarness(decision_database, options) as harness:
        assert harness.decision is not None
        result = await harness.decision.evaluate(
            make_request(
                identity=identity,
                target=target,
                known_references=(reached, unrelated),
            )
        )
        assert result.status == "success"
        state = harness.transport.state(0)
        rendered = json.dumps(state, ensure_ascii=False)
        assert "被引用的正文" in rendered
        assert "MARKER-42" not in rendered
        # 无关原文也不构成新的追踪起点
        assert harness.reference_calls == []
        # 已知引用不再以独立清单形式扩展选中输入
        conversation = _conversation(state)
        assert "known_references" not in conversation
        assert len(conversation["timeline"]) == 1
